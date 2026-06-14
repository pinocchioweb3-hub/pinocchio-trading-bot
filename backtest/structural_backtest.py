"""結構型長週期回測（v33，路線 D #2 的主體）。

目的：誠實回答「我們這套的勝率到底多少」——但只用「長期可取得」的因子：
    OHLC（量能/ATR/突破）+ BTC 4h 200MA 大盤閘門 + 結構（N 根高低突破）。
**不含** CoinGlass 因子（資金費率/OI/多空比，免費僅回溯 ~83 天），故這是「結構層」
回測，非完整多因子策略。資料源：backtest/data_loader（Binance 年級歷史）。
模擬器：已修的 simulator（OHLC 盤中判 stop/tp）。

策略（結構型動能突破代理，貼近 intraday 精神）：
    多單：BTC 閘門開(BTC 4h>200MA) + 量縮後爆量(vol z) + 突破近 N 根高 → FIRE 多
    空單：BTC 閘門關(BTC 4h<200MA) + 爆量 + 跌破近 N 根低 → FIRE 空
    停損 sl_pct%，TP 1R/1.5R/2R，hold_max 小時，冷卻避免連發。
"""
from __future__ import annotations

import asyncio
from statistics import mean, pstdev

from .data_loader import get_ohlc
from .simulator import simulate
from .metrics import aggregate
from .validation import assess


def _gate_from_4h(btc4h: list[dict], period: int = 200) -> list[tuple]:
    """BTC 4h 收盤 vs 200MA → [(ts, above_200ma 或 None)] 升序。"""
    closes = [c["close"] for c in btc4h]
    out = []
    for i, c in enumerate(btc4h):
        out.append((c["ts"], (c["close"] > sum(closes[i - period + 1:i + 1]) / period)
                    if i >= period - 1 else None))
    return out


def _gate_at(series: list[tuple], ts: int) -> bool:
    val = None
    for bts, above in series:
        if bts > ts:
            break
        if above is not None:
            val = above
    return bool(val)


def _replay_structural(symbol: str, candles: list[dict], gate_series: list[tuple],
                       *, brk_lookback: int = 20, vol_window: int = 20,
                       vol_z_min: float = 1.8, atr_period: int = 14,
                       sl_pct: float = 4.0, tp_r=(1.0, 1.5, 2.0),
                       hold_max_hours: int = 48, cooldown_bars: int = 12,
                       future_window: int = 48) -> list:
    n = len(candles)
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]
    closes = [c["close"] for c in candles]
    vols = [c.get("volume", 0) or 0 for c in candles]
    # ATR(period)
    trs = [0.0]
    for i in range(1, n):
        trs.append(max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]),
                       abs(lows[i] - closes[i - 1])))
    trades = []
    last_fire = -10_000
    warmup = max(brk_lookback, vol_window, atr_period) + 2
    for i in range(warmup, n - 1):
        if i - last_fire < cooldown_bars:
            continue
        vma = mean(vols[i - vol_window:i]) or 1e-9
        vol_z = vols[i] / vma
        if vol_z < vol_z_min:
            continue
        gate_open = _gate_at(gate_series, candles[i]["ts"])
        prior_high = max(highs[i - brk_lookback:i])
        prior_low = min(lows[i - brk_lookback:i])
        direction = None
        if gate_open and closes[i] > prior_high:
            direction = "bull"
        elif (not gate_open) and closes[i] < prior_low:
            direction = "bear"
        if direction is None:
            continue
        entry = closes[i]
        if direction == "bull":
            stop = entry * (1 - sl_pct / 100)
            sld = entry - stop
            tps = tuple(entry + sld * r for r in tp_r)
        else:
            stop = entry * (1 + sl_pct / 100)
            sld = stop - entry
            tps = tuple(entry - sld * r for r in tp_r)
        future = [(candles[j]["ts"], candles[j]["high"], candles[j]["low"],
                   candles[j]["close"]) for j in range(i + 1, min(i + 1 + future_window, n))]
        if not future:
            break
        trades.append(simulate(symbol=symbol, setup_name="structural",
                               direction=direction, entry_ts=candles[i]["ts"],
                               entry_price=entry, stop=stop, tps=tps,
                               future_prices=future, hold_max_hours=hold_max_hours))
        last_fire = i
    return trades


def _sharpe(trades: list) -> float:
    rs = [t.realized_r for t in trades]
    if len(rs) < 2:
        return 0.0
    sd = pstdev(rs)
    return (mean(rs) / sd) if sd > 0 else 0.0


async def run_structural_backtest(symbols: list[str] | None = None,
                                  tf: str = "1h", days: int = 365) -> dict:
    """跑結構型長回測。回 {symbol: {metrics..}} + _overall 彙總。"""
    symbols = symbols or ["BTC", "ETH", "SOL"]
    btc4h = await get_ohlc("BTC", "4h", days + 40)
    gate_series = _gate_from_4h(btc4h)
    gate_open_pct = (sum(1 for _, a in gate_series if a) /
                     max(1, sum(1 for _, a in gate_series if a is not None)) * 100)
    out = {"_meta": {"days": days, "tf": tf,
                     "btc_gate_open_pct": round(gate_open_pct, 1)}}
    all_trades = []
    for sym in symbols:
        candles = await get_ohlc(sym, tf, days)
        if len(candles) < 250:
            out[sym] = {"n": 0, "note": f"insufficient ({len(candles)} bars)"}
            continue
        trades = _replay_structural(sym, candles, gate_series)
        all_trades.extend(trades)
        m = aggregate(trades)
        va = assess([t.realized_r for t in trades])
        out[sym] = {"n": m.n_trades, "win_rate": round(m.win_rate * 100, 1),
                    "expectancy_r": round(m.expectancy_r, 3),
                    "profit_factor": round(m.profit_factor, 2),
                    "max_consec_losses": m.max_consecutive_losses,
                    "psr": va.get("psr"), "min_trl": va.get("min_trl"),
                    "bars": len(candles)}
    om = aggregate(all_trades)
    ova = assess([t.realized_r for t in all_trades])
    out["_overall"] = {"n": om.n_trades, "win_rate": round(om.win_rate * 100, 1),
                       "expectancy_r": round(om.expectancy_r, 3),
                       "profit_factor": round(om.profit_factor, 2),
                       "max_consec_losses": om.max_consecutive_losses,
                       "psr": ova.get("psr"), "dsr": ova.get("dsr"),
                       "min_trl": ova.get("min_trl"), "verdict": ova.get("verdict")}
    return out


if __name__ == "__main__":
    import sys
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 365
    syms = sys.argv[2:] if len(sys.argv) > 2 else None

    async def t():
        r = await run_structural_backtest(syms, "1h", days)
        meta = r.pop("_meta")
        print(f"=== 結構型長回測 {meta['days']}d 1h｜BTC 閘門開 {meta['btc_gate_open_pct']}% 時間 ===")
        for k, v in r.items():
            print(f"  {k}: {v}")
    asyncio.run(t())
