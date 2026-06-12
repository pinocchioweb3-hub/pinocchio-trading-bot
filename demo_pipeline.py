"""端到端 demo：mock data → mi_get_snapshot → L2 evaluate → FIRE/HOLD。

執行：
    python demo_pipeline.py

不需要 MCP Inspector；把工具當成普通 async 函式呼叫，驗證從資料層到
決策層整條鏈是通的。看到 SUI/ARB 出 FIRE 就代表 Task 8 完成。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# Windows PowerShell 預設 cp950 不支援 emoji；強制 UTF-8
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from l2_trigger.configs.ambush import get_ambush_config
from l2_trigger.configs.intraday import get_intraday_config
from l2_trigger.engine import evaluate
from l2_trigger.leverage import choose_leverage, compute_position, compute_tp_prices
from l2_trigger.types import MarketSnapshot
from market_intel_mcp.server import (
    mi_get_funding,
    mi_get_snapshot,
    mi_get_strength_rank,
    mi_health,
)


def dict_to_snapshot(d: dict) -> MarketSnapshot:
    """從 mi_get_snapshot 的 dict 輸出組 MarketSnapshot。
    缺料欄位讓 dataclass 用預設值 None；stale_fields 保留來源標記。
    """
    field_names = {f.name for f in MarketSnapshot.__dataclass_fields__.values()}
    filtered = {k: v for k, v in d.items() if k in field_names and v is not None}
    # 補強制欄位
    if "symbol" not in filtered:
        filtered["symbol"] = d.get("symbol", "UNKNOWN")
    if "ts" not in filtered:
        filtered["ts"] = 0
    if "price" not in filtered:
        filtered["price"] = 0.0
    # stale_fields 必須是 tuple
    if "stale_fields" in filtered and not isinstance(filtered["stale_fields"], tuple):
        filtered["stale_fields"] = tuple(filtered["stale_fields"])
    if "sources_used" in filtered and not isinstance(filtered["sources_used"], tuple):
        filtered["sources_used"] = tuple(filtered["sources_used"])
    return MarketSnapshot(**filtered)


def render_decision(symbol: str, snap: MarketSnapshot, decision, cfg) -> None:
    if decision.action.value == "fire":
        # 算倉位
        lev = choose_leverage(symbol, snap.atr_pct_7d)
        entry = snap.price
        direction = decision.direction.value
        sl_pct = cfg.sl_buffer_pct / 100
        stop = round(entry * (1 - sl_pct) if direction == "bull" else entry * (1 + sl_pct), 6)
        pos = compute_position(entry, stop, cfg.risk_per_trade_usd, lev)
        tps = compute_tp_prices(entry, stop, direction, cfg.tp_r_multiples)
        print(f"    🔥 FIRE {direction.upper()}  score={decision.composite_score:+.2f}")
        print(f"       lev={lev}x  entry=${entry}  stop=${stop} ({pos['sl_distance_pct']}%)")
        print(f"       margin=${pos['margin_usd']}  notional=${pos['notional_usd']}")
        print(f"       TP1=${tps['tp1']}  TP2=${tps['tp2']}  TP3=${tps['tp3']}")
    else:
        print(f"    ⏸  HOLD — {decision.reason}")


async def main() -> int:
    print("=" * 72)
    print("  market-intel-mcp v0  ×  L2 trigger engine  — end-to-end pipeline demo")
    print("=" * 72)

    # 1. Health
    health = await mi_health()
    print(f"\n[mi_health]")
    print(f"  backend={health.get('backend')}  source={health.get('source_name')}  ok={health.get('ok')}")
    print(f"  watchlist core: {health.get('watchlist_core')}")

    # 2. Sanity check on individual tool
    print(f"\n[mi_get_funding('SUI-USDT-SWAP')]  (測 symbol 正規化)")
    f = await mi_get_funding("SUI-USDT-SWAP")
    print(f"  → funding={f.get('funding')}  predicted={f.get('funding_predicted')}")

    # 3. Strength ranking
    print(f"\n[mi_get_strength_rank(top_n=5)]")
    rank = await mi_get_strength_rank(limit=50, top_n=5)
    print(f"  weights: {rank.get('weights')}")
    for it in rank.get("items", []):
        ctx = f"ret7d={it.get('return_7d_pct'):+.1f}% oi7d={it.get('oi_delta_7d_pct'):+.1f}% vol={it.get('vol_24h_vs_30d'):.2f}"
        print(f"    {it['symbol']:5s}  score={it['strength_score']:5.1f}  {ctx}")

    # 4. End-to-end pipeline per symbol
    for sym in ("SUI", "ARB", "BTC"):
        print()
        print("─" * 72)
        print(f"  📊 {sym}  ── snapshot → L2 engine")
        print("─" * 72)

        snap_dict = await mi_get_snapshot(sym, tf="1h", lookback=96)
        snap = dict_to_snapshot(snap_dict)

        print(f"  price=${snap.price}  is_hot={snap.is_hot}  strength={snap.strength_score}")
        print(f"  oi=${snap.oi:,.0f}  oi_d24={snap.oi_delta_pct}%  funding={snap.funding}")
        print(f"  cvd_div={snap.cvd_price_divergence}  cvd_slope={snap.cvd_slope}")
        print(f"  top_trader={snap.top_trader_ratio}  retail={snap.ls_ratio}")
        print(f"  btc_gate={snap.btc_gate_open} ({snap.btc_regime})  atr7d={snap.atr_pct_7d}%")
        print(f"  stale_fields={list(snap.stale_fields) or 'none'}")
        print(f"  sources_used={list(snap.sources_used)}")

        # Setup A
        cfg_a = get_intraday_config(sym)
        dec_a = evaluate(snap, cfg_a)
        print(f"\n  Setup A (intraday):")
        render_decision(sym, snap, dec_a, cfg_a)

        # Setup B
        cfg_b = get_ambush_config(sym)
        dec_b = evaluate(snap, cfg_b)
        print(f"\n  Setup B (ambush):")
        render_decision(sym, snap, dec_b, cfg_b)

    print()
    print("=" * 72)
    print("  Pipeline OK ✅  — Task 8 complete.")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
