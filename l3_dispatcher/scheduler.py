"""Worker 1: 排程掃描器。

每 N 秒掃 watchlist：
    1. mi_get_snapshot(sym)
    2. 兩套 config (intraday/ambush) evaluate
    3. FIRE → fire_queue.enqueue (內建 cooldown 檢查)
    4. 把 scan summary 回傳給 main，給 heartbeat 用
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from l2_trigger.configs.ambush import get_ambush_config
from l2_trigger.configs.intraday import get_intraday_config
from l2_trigger.engine import evaluate
from l2_trigger.types import MarketSnapshot, TriggerAction

from .fire_queue import enqueue


# v34：核心行情欄位（CoinGlass + Binance 雙源皆可補）。只有這些 stale 才代表
# 真實資料源故障；進階衍生欄（cvd/清算/structure 等）stale 屬可接受降級，
# 不納入 supervisor 的 data_quality_low 告警口徑（避免每 ~30 分誤報誣指主流幣）。
CORE_FIELDS = frozenset({
    "price", "ts", "oi", "oi_delta_pct",
    "funding", "funding_predicted", "top_trader_ratio", "ls_ratio",
})


@dataclass
class ScanSummary:
    scanned: int = 0
    fires_enqueued: int = 0
    fires_in_cooldown: int = 0
    holds: int = 0
    errors: int = 0
    snapshots: list[dict] = field(default_factory=list)


def _dict_to_snapshot(d: dict, fallback_symbol: str) -> MarketSnapshot:
    """從 mi_get_snapshot dict → MarketSnapshot；缺料欄位用 None。"""
    field_names = {f.name for f in MarketSnapshot.__dataclass_fields__.values()}
    kept = {k: v for k, v in d.items() if k in field_names and v is not None}
    if "stale_fields" in kept and not isinstance(kept["stale_fields"], tuple):
        kept["stale_fields"] = tuple(kept["stale_fields"])
    if "sources_used" in kept and not isinstance(kept["sources_used"], tuple):
        kept["sources_used"] = tuple(kept["sources_used"])
    if "symbol" not in kept:
        kept["symbol"] = fallback_symbol
    if "ts" not in kept:
        kept["ts"] = 0
    if "price" not in kept:
        kept["price"] = 0.0
    return MarketSnapshot(**kept)


async def _gather_check_context(sym: str) -> dict:
    """蒐集 cross-check 需要的補充資料（ETF/sentiment/liq）"""
    from market_intel_mcp.server import (
        mi_get_etf_flows, mi_get_liquidation_scan, mi_get_sentiment,
    )
    ctx: dict = {}
    try:
        liq = await mi_get_liquidation_scan(20)
        ctx["liq_scan"] = liq if isinstance(liq, dict) else None
    except Exception:
        ctx["liq_scan"] = None
    try:
        sent = await mi_get_sentiment()
        ctx["sentiment"] = sent if isinstance(sent, dict) else None
    except Exception:
        ctx["sentiment"] = None
    if sym in ("BTC", "ETH"):
        try:
            etf = await mi_get_etf_flows(sym, 7)
            ctx["etf_flows"] = etf if isinstance(etf, dict) else None
        except Exception:
            ctx["etf_flows"] = None
    else:
        ctx["etf_flows"] = None
    return ctx


async def scan_once(watchlist_or_list, cooldown_seconds: int = 3600,
                    cross_check: bool = True) -> ScanSummary:
    """One scan cycle. 接受 list[str] 或 WatchlistManager（會用 fire_tier()）。"""
    from market_intel_mcp.server import mi_get_snapshot
    from .checks import cross_check_fire

    # 接受兩種輸入
    if hasattr(watchlist_or_list, "fire_tier"):
        fire_syms = watchlist_or_list.fire_tier()
        monitor_only = [s for s in watchlist_or_list.all_symbols if s not in fire_syms]
    else:
        fire_syms = list(watchlist_or_list)
        monitor_only = []

    summary = ScanSummary()

    # FIRE-eligible：跑 evaluate + cross-check
    for sym in fire_syms:
        try:
            d = await mi_get_snapshot(sym, "1h", 96)
            snap = _dict_to_snapshot(d, sym)
            summary.scanned += 1
            summary.snapshots.append({
                "symbol": sym, "tier": "trading",
                "price": snap.price, "funding": snap.funding,
                "oi_delta_pct": snap.oi_delta_pct,
                "btc_gate_open": snap.btc_gate_open, "btc_regime": snap.btc_regime,
                "is_hot": snap.is_hot, "strength_score": snap.strength_score,
                "stale_count": len(snap.stale_fields),
                "core_stale_count": len(set(snap.stale_fields) & CORE_FIELDS),
            })
            # v23-5: 策略由註冊表驅動（取代寫死的 intraday）— 用戶可在 .env
            # STRATEGIES_ENABLED 自選；預設只有 intraday 為 live（行為不變）
            from l2_trigger.registry import scheduler_strategies
            for _meta in scheduler_strategies():
                cfg = _meta.config_factory(sym)
                decision = evaluate(snap, cfg)
                if decision.action == TriggerAction.FIRE:
                    # === Cross-check 跨來源一致性檢查 ===
                    if cross_check:
                        ctx = await _gather_check_context(sym)
                        chk = await cross_check_fire(
                            decision,
                            etf_flows=ctx.get("etf_flows"),
                            sentiment=ctx.get("sentiment"),
                            liq_scan=ctx.get("liq_scan"),
                        )
                        if not chk.pass_:
                            print(f"[scheduler] {sym}/{cfg.setup_name}/{decision.direction.value} "
                                  f"FIRE blocked by cross-check (conf={chk.confidence}): {chk.reason}")
                            summary.fires_in_cooldown += 1   # 視同被擋
                            continue
                        # 把 confidence 注入 enqueue 用的 decision metadata
                        if enqueue(decision, cooldown_seconds=cooldown_seconds,
                                   cross_check_payload={
                                       "confidence": chk.confidence,
                                       "checks": chk.checks,
                                       "reason": chk.reason,
                                   }):
                            summary.fires_enqueued += 1
                        else:
                            summary.fires_in_cooldown += 1
                    else:
                        if enqueue(decision, cooldown_seconds=cooldown_seconds):
                            summary.fires_enqueued += 1
                        else:
                            summary.fires_in_cooldown += 1
                else:
                    summary.holds += 1
        except Exception as e:
            summary.errors += 1
            print(f"[scheduler] error scanning {sym}: {type(e).__name__}: {e}")

    # 指標 + 現貨層：只更新 snapshot 資訊，不跑 evaluate
    for sym in monitor_only:
        try:
            d = await mi_get_snapshot(sym, "1h", 96)
            snap = _dict_to_snapshot(d, sym)
            summary.snapshots.append({
                "symbol": sym, "tier": "monitor",
                "price": snap.price, "funding": snap.funding,
                "oi_delta_pct": snap.oi_delta_pct,
                "btc_gate_open": snap.btc_gate_open,
                "stale_count": len(snap.stale_fields),
                "core_stale_count": len(set(snap.stale_fields) & CORE_FIELDS),
            })
        except Exception as e:
            summary.errors += 1
            print(f"[scheduler] monitor {sym}: {type(e).__name__}: {e}")

    return summary


async def run_scheduler(
    watchlist,                       # WatchlistManager 或 list[str]
    interval_seconds: int,
    cooldown_seconds: int,
    summary_callback=None,
):
    """Worker 主迴圈。"""
    while True:
        summary = await scan_once(watchlist, cooldown_seconds=cooldown_seconds)
        n_fire = summary.fires_enqueued
        n_cd = summary.fires_in_cooldown
        n_hold = summary.holds
        n_err = summary.errors
        n_mon = sum(1 for s in summary.snapshots if s.get("tier") == "monitor")
        print(f"[scheduler] fire_scan={summary.scanned}({n_fire} fires, {n_cd} cooldown, {n_hold} holds)  monitor={n_mon}  err={n_err}")
        if summary_callback:
            summary_callback(summary)
        await asyncio.sleep(interval_seconds)
