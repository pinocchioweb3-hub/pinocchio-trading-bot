"""真實歷史回測：從 CoinGlass + OKX 拉真資料，跑 L2 引擎，產出真實 metrics。

替換 backtest/historical.py 的合成 mock 為實際歷史 OHLC + funding + OI + positioning。

策略：
1. 用 OKX 1h candles 拉 N 天歷史（OKX 4h × 200 = 33 天上限）
2. 用 CoinGlass funding-rate/history 拉同期間的 8h funding 序列
3. 用 CoinGlass OI aggregated-history 拉 OI 序列
4. 用 CoinGlass positioning history 拉大戶/散戶比序列
5. 對每個小時 timestamp 組 MarketSnapshot
6. 跑 L2 evaluate() → 收集所有 FIRE
7. 用同樣方式跑 simulator → 算真實 PnL
"""
from __future__ import annotations

import asyncio
import datetime as dt
import os
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")
os.environ["MARKET_INTEL_BACKEND"] = "coinglass"

sys.path.insert(0, str(ROOT))

from l2_trigger.configs.ambush import get_ambush_config
from l2_trigger.configs.intraday import get_intraday_config
from l2_trigger.engine import evaluate
from l2_trigger.types import MarketSnapshot, TriggerAction

from market_intel_mcp.sources.coinglass import CoinGlassSource
from market_intel_mcp.sources.okx_candles import get_okx_candles
from .historical import HistoryPoint
from .metrics import aggregate
from .replay import _to_snapshot, FireEvent
from .report import render_summary, render_trade_log
from .simulator import simulate


_BTC_GATE_SERIES: list | None = None


async def _btc_gate_series() -> list:
    """v33: 取 BTC 4h 200MA 序列當回測 btc_gate 真值（取代寫死 True，那會嚴重高估）。
    回 [(ts_ms, above_200ma 或 None)] 升序。快取：整批回測只抓一次。"""
    global _BTC_GATE_SERIES
    if _BTC_GATE_SERIES is not None:
        return _BTC_GATE_SERIES
    try:
        okx = get_okx_candles()
        d = await okx.get_candles("BTC", "4h", 300)
        candles = d.get("candles", []) if isinstance(d, dict) else []
        closes = [c["close"] for c in candles]
        out = []
        for i, c in enumerate(candles):
            out.append((c["ts"], (c["close"] > sum(closes[i - 199:i + 1]) / 200)
                        if i >= 199 else None))
        _BTC_GATE_SERIES = out
    except Exception:
        _BTC_GATE_SERIES = []
    return _BTC_GATE_SERIES


def _btc_gate_at(series: list, ts: int) -> bool:
    """找 ts 之前最近一根 BTC 4h bar 的 above_200ma；無資料保守回 False（不高估）。"""
    val = None
    for bts, above in series:
        if bts > ts:
            break
        if above is not None:
            val = above
    return bool(val)


async def fetch_real_history(
    symbol: str,
    days: int = 30,
    cg: CoinGlassSource | None = None,
) -> list[HistoryPoint]:
    """從多源拉真實歷史，組成 HistoryPoint 序列（每小時 1 個）"""
    if cg is None:
        cg = CoinGlassSource()

    print(f"  [{symbol}] 拉 {days} 天歷史...")
    # 1h candles
    okx_cd = get_okx_candles()
    candles_resp = await okx_cd.get_candles(symbol, "1h", min(days * 24, 300))
    if candles_resp.get("error"):
        print(f"    [skip] {symbol} candles error: {candles_resp.get('message')}")
        return []
    candles = candles_resp.get("candles", [])
    if len(candles) < 168:
        print(f"    [skip] {symbol} 只有 {len(candles)} 根，不足 7 天")
        return []
    print(f"    OK candles: {len(candles)} 根")

    # CoinGlass funding (8h, 拉同等小時長)
    funding_resp = await cg.get_positioning(symbol, "top_trader_position", "1h",
                                            min(days * 24, 500))
    top_trader_series = []
    if not funding_resp.get("error"):
        top_trader_series = funding_resp.get("series", [])
    print(f"    OK top_trader: {len(top_trader_series)} 點")

    retail_resp = await cg.get_positioning(symbol, "account", "1h",
                                          min(days * 24, 500))
    retail_series = []
    if not retail_resp.get("error"):
        retail_series = retail_resp.get("series", [])
    print(f"    OK retail: {len(retail_series)} 點")

    oi_resp = await cg.get_oi(symbol, "1h", min(days * 24, 500))
    oi_series = []
    if not oi_resp.get("error"):
        oi_series = oi_resp.get("series", [])
    print(f"    OK oi: {len(oi_series)} 點")

    fund_resp_h = await cg.client.get(
        "/api/futures/funding-rate/history",
        params={"exchange": "Binance", "symbol": f"{symbol}USDT",
                "interval": "1h", "limit": min(days * 24, 500)},
    )
    funding_series = []
    try:
        fb = fund_resp_h.json()
        if fb.get("code") in ("0", 0):
            for d in fb.get("data") or []:
                funding_series.append({"ts": int(d.get("time", 0)),
                                       "value": float(d.get("close", 0))})
        print(f"    OK funding: {len(funding_series)} 點")
    except Exception as e:
        print(f"    [warn] funding parse: {e}")

    # 用時間戳對齊資料：每根 candle 找最接近的 funding/oi/positioning
    def _lookup(series: list[dict], ts: int):
        if not series: return None
        # binary search-ish: 找 ts 之前最近的
        last = None
        for p in series:
            if p["ts"] <= ts:
                last = p["value"]
            else:
                break
        return last

    # 組 HistoryPoint
    btc_gate_series = await _btc_gate_series()   # v33: 真實 BTC 4h 200MA 閘門
    points: list[HistoryPoint] = []
    for i, c in enumerate(candles):
        ts = c["ts"]
        gate = _btc_gate_at(btc_gate_series, ts)   # v33: 取代寫死 True
        # 從前一根算 oi_delta_pct（24h 變化）
        oi_now = _lookup(oi_series, ts)
        oi_24h_ago = _lookup(oi_series, ts - 24 * 3600 * 1000)
        oi_delta = ((oi_now - oi_24h_ago) / oi_24h_ago * 100
                    if oi_now and oi_24h_ago else 0)
        # cvd_slope (從近期 candles 用 high-low 推估，因為沒有 trade-level CVD)
        if i >= 5:
            recent = candles[max(0, i-5):i+1]
            up_vol = sum(r["volume"] for r in recent if r["close"] > r["open"])
            down_vol = sum(r["volume"] for r in recent if r["close"] < r["open"])
            tot = up_vol + down_vol
            cvd_slope = (up_vol - down_vol) / tot if tot > 0 else 0
        else:
            cvd_slope = 0
        # cvd_price_divergence (簡化：price 5h 持平/跌 + cvd_slope 正 = bull)
        if i >= 5:
            price_chg = (c["close"] - candles[i-5]["close"]) / candles[i-5]["close"] * 100
            if price_chg <= 0.5 and cvd_slope > 0.2:
                cvd_div = "bull"
            elif price_chg >= -0.5 and cvd_slope < -0.2:
                cvd_div = "bear"
            else:
                cvd_div = "none"
        else:
            cvd_div = "none"

        # ATR 7d (從近 7d candles)
        if i >= 168:
            recent7d = candles[i-168:i]
            highs = [r["high"] for r in recent7d]
            lows = [r["low"] for r in recent7d]
            atr_pct_7d = (max(highs) - min(lows)) / c["close"] * 100
            vol_24h = sum(r["volume"] for r in candles[max(0, i-24):i])
            vol_30d_avg = vol_24h  # 簡化，沒有 30d 數據
            vol_ratio = 1.0
            # higher lows: 7d 內每天的低點是否上升
            daily_lows = []
            for d_start in range(0, 168, 24):
                d_bars = recent7d[d_start:d_start+24]
                if d_bars: daily_lows.append(min(b["low"] for b in d_bars))
            ascending = sum(1 for j in range(1, len(daily_lows))
                           if daily_lows[j] > daily_lows[j-1])
            higher_lows = ascending >= 4
        else:
            atr_pct_7d = 5.0
            vol_ratio = 1.0
            higher_lows = False

        points.append(HistoryPoint(
            ts=ts, symbol=symbol, price=c["close"],
            high=c["high"], low=c["low"],   # v33: 給 simulator 判盤中觸及
            oi=oi_now or 0, oi_delta_pct=oi_delta,
            funding=_lookup(funding_series, ts) or 0,
            funding_predicted=_lookup(funding_series, ts) or 0,
            cvd=0, cvd_slope=cvd_slope, cvd_price_divergence=cvd_div,
            top_trader_ratio=_lookup(top_trader_series, ts) or 1.0,
            ls_ratio=_lookup(retail_series, ts) or 1.0,
            liq_long=0, liq_short=0,
            btc_gate_open=gate,   # v33: 真實 BTC 4h 200MA 閘門（非寫死）
            btc_regime="trend_up" if gate else "trend_down",
            above_4h_200ma=gate,
            is_hot=True, strength_score=70,
            atr_pct_7d=atr_pct_7d, vol_24h_vs_30d=vol_ratio,
            cvd_slope_7d=cvd_slope * 5,  # 粗估
            top_trader_slope_7d=0.005, oi_delta_7d_pct=oi_delta * 0.3,
            higher_lows_7d=higher_lows,
            event_tag="real",
        ))

    return points


async def replay_real(history: list[HistoryPoint], config, future_window: int = 36):
    """簡化 replay（呼叫 simulator）"""
    from l2_trigger.cooldown import CooldownStore
    cooldown = CooldownStore(cooldown_seconds=4 * 3600)
    trades = []
    fires = []
    for idx, point in enumerate(history):
        snap = _to_snapshot(point)
        decision = evaluate(snap, config)
        if decision.action != TriggerAction.FIRE:
            continue
        fires.append(FireEvent(
            ts=point.ts, setup_name=decision.setup_name,
            direction=decision.direction.value,
            snapshot_tag="real", reason=decision.reason,
        ))
        if not cooldown.should_emit(decision, now=point.ts / 1000.0):
            continue
        cooldown.mark_fired(decision, now=point.ts / 1000.0)
        # 算進場/SL/TP
        direction = decision.direction.value
        entry = point.price
        sl_pct = config.sl_buffer_pct / 100
        stop = entry * (1 - sl_pct) if direction == "bull" else entry * (1 + sl_pct)
        sl_dist = abs(entry - stop)
        tp_r = config.tp_r_multiples
        if direction == "bull":
            tps = tuple(entry + sl_dist * r for r in tp_r)
        else:
            tps = tuple(entry - sl_dist * r for r in tp_r)
        # 未來價格
        # v33: 傳 OHLC（ts, high, low, close）讓 simulator 用盤中高低判 stop/tp
        future = [(history[j].ts, history[j].high or history[j].price,
                   history[j].low or history[j].price, history[j].price)
                  for j in range(idx + 1, min(idx + 1 + future_window, len(history)))]
        outcome = simulate(
            symbol=point.symbol, setup_name=config.setup_name,
            direction=direction, entry_ts=point.ts,
            entry_price=entry, stop=stop, tps=tps,
            future_prices=future, hold_max_hours=config.hold_max_hours,
        )
        trades.append(outcome)
    return trades, fires


async def main(symbols: list[str] | None = None, days: int = 30):
    print("=" * 70)
    print(f"  真實歷史回測 ({days} 天)")
    print("=" * 70)

    if not symbols:
        symbols = ["BTC", "ETH", "SOL", "SUI", "ARB", "INJ"]

    cg = CoinGlassSource()
    all_results = {}

    for sym in symbols:
        try:
            history = await fetch_real_history(sym, days, cg)
            if len(history) < 168:
                print(f"  [skip] {sym} 資料不足")
                continue
            print(f"  [{sym}] 歷史: {len(history)} 點 ({history[0].ts} → {history[-1].ts})")

            # 跑 intraday + ambush 兩種
            for setup_fn, label in [(get_intraday_config, "intraday"),
                                     (get_ambush_config, "ambush")]:
                cfg = setup_fn(sym)
                trades, fires = await replay_real(history, cfg)
                m = aggregate(trades)
                all_results[f"{sym}_{label}"] = m
                print(f"\n=== {sym} / {label} ===")
                print(render_summary(
                    symbol=sym, setup_name=label,
                    risk_per_trade_usd=cfg.risk_per_trade_usd,
                    metrics=m, fires=fires,
                ))
                if trades:
                    print(render_trade_log(trades[:10], limit=10))
        except Exception as e:
            print(f"  [error] {sym}: {type(e).__name__}: {e}")

    await cg.close()

    # 總結
    print()
    print("=" * 70)
    print("  總結（所有 symbol × setup 組合）")
    print("=" * 70)
    for key, m in all_results.items():
        if m.n_trades > 0:
            print(f"  {key:20} trades={m.n_trades:3d}  勝率={m.win_rate*100:.1f}%  "
                  f"期望={m.expectancy_r:+.3f}R  PF={m.profit_factor:.2f}  "
                  f"連虧={m.max_consecutive_losses}")


if __name__ == "__main__":
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    syms = sys.argv[2:] if len(sys.argv) > 2 else None
    asyncio.run(main(symbols=syms, days=days))
