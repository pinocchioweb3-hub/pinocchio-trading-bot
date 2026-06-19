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

# ── Session B：跨年純價格層誠實橫幅（任何用 yearscale 來源的報告都必須帶）──
YEARSCALE_HONESTY_BANNER = (
    "⚠️ 跨年回測＝純價格層：只用 OHLC（價格/量/結構/ATR）。"
    "CoinGlass 綜合指標（OI/CVD/funding/多空比）每個 history 端點硬卡 500 根、"
    "present-anchored、無時間分頁 → 跨年「綜合指標」歷史物理上做不到，"
    "故年級回測的 funding/OI/positioning 一律中性化（不是真值、不可解讀為當時市況）。"
)


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


# ── Session B：跨年純價格層 BTC 200MA 閘門（不受 OKX 300 根上限）──────────
_BTC_GATE_SERIES_YS: dict[str, list] = {}


async def _btc_gate_series_yearscale(days: int, tf: str = "4h") -> list:
    """年級版 BTC 4h 200MA 閘門序列。走 data_loader.get_ohlc（Binance 年級、分頁），
    取代只能拉 300 根的 OKX 端點。回 [(ts_ms, above_200ma 或 None)] 升序。
    按 (days,tf) 快取，整批回測只算一次。"""
    from .data_loader import get_ohlc
    key = f"{tf}:{days}"
    cached = _BTC_GATE_SERIES_YS.get(key)
    if cached is not None:
        return cached
    try:
        candles = await get_ohlc("BTC", tf, days + 40)   # +40 暖機 200MA
        closes = [c["close"] for c in candles]
        out = []
        for i, c in enumerate(candles):
            out.append((c["ts"], (c["close"] > sum(closes[i - 199:i + 1]) / 200)
                        if i >= 199 else None))
        _BTC_GATE_SERIES_YS[key] = out
    except Exception:
        _BTC_GATE_SERIES_YS[key] = []
    return _BTC_GATE_SERIES_YS[key]


def _build_points_from_ohlc(symbol: str, candles: list[dict],
                            btc_gate_series: list) -> list[HistoryPoint]:
    """從純價格 OHLC（年級可行）組 HistoryPoint。

    與 fetch_real_history 的價格層因子（cvd_slope 量能代理 / cvd_div / ATR7d /
    higher_lows / oi_delta）邏輯完全一致；但綜合指標（OI/funding/positioning）
    跨年取不到 → 全部中性化（oi=0、funding=0、ratio=1.0、cvd=0），並依賴
    YEARSCALE_HONESTY_BANNER 說明這不是真值。

    ⚠️ tf 假設：cvd_div / ATR7d / higher_lows 的「24 根=1 天、168 根=7 天」是
    1h 慣例。年級來源建議用 4h（窗口會自然對應更長真實時長，純為波動度代理仍合理）。"""
    points: list[HistoryPoint] = []
    for i, c in enumerate(candles):
        ts = c["ts"]
        gate = _btc_gate_at(btc_gate_series, ts)
        # cvd_slope：用近 6 根 up/down 量能差代理（無 trade-level CVD），與既有一致
        if i >= 5:
            recent = candles[max(0, i - 5):i + 1]
            up_vol = sum(r.get("volume", 0) or 0 for r in recent if r["close"] > r["open"])
            down_vol = sum(r.get("volume", 0) or 0 for r in recent if r["close"] < r["open"])
            tot = up_vol + down_vol
            cvd_slope = (up_vol - down_vol) / tot if tot > 0 else 0
        else:
            cvd_slope = 0
        if i >= 5:
            price_chg = (c["close"] - candles[i - 5]["close"]) / candles[i - 5]["close"] * 100
            if price_chg <= 0.5 and cvd_slope > 0.2:
                cvd_div = "bull"
            elif price_chg >= -0.5 and cvd_slope < -0.2:
                cvd_div = "bear"
            else:
                cvd_div = "none"
        else:
            cvd_div = "none"
        if i >= 168:
            recent7d = candles[i - 168:i]
            highs = [r["high"] for r in recent7d]
            lows = [r["low"] for r in recent7d]
            atr_pct_7d = (max(highs) - min(lows)) / c["close"] * 100
            daily_lows = []
            for d_start in range(0, 168, 24):
                d_bars = recent7d[d_start:d_start + 24]
                if d_bars:
                    daily_lows.append(min(b["low"] for b in d_bars))
            ascending = sum(1 for j in range(1, len(daily_lows))
                            if daily_lows[j] > daily_lows[j - 1])
            higher_lows = ascending >= 4
        else:
            atr_pct_7d = 5.0
            higher_lows = False
        points.append(HistoryPoint(
            ts=ts, symbol=symbol, price=c["close"],
            high=c["high"], low=c["low"],
            # ── 綜合指標跨年取不到 → 中性化（見 YEARSCALE_HONESTY_BANNER）──
            oi=0, oi_delta_pct=0,
            funding=0, funding_predicted=0,
            cvd=0, cvd_slope=cvd_slope, cvd_price_divergence=cvd_div,
            top_trader_ratio=1.0, ls_ratio=1.0,
            liq_long=0, liq_short=0,
            btc_gate_open=gate,
            btc_regime="trend_up" if gate else "trend_down",
            above_4h_200ma=gate,
            is_hot=True, strength_score=70,
            atr_pct_7d=atr_pct_7d, vol_24h_vs_30d=1.0,
            cvd_slope_7d=cvd_slope * 5,
            top_trader_slope_7d=0.0, oi_delta_7d_pct=0.0,
            higher_lows_7d=higher_lows,
            event_tag="yearscale_priceonly",
        ))
    return points


async def fetch_real_history_yearscale(symbol: str, days: int = 365,
                                       tf: str = "4h") -> list[HistoryPoint]:
    """【純價格層年級來源】從 data_loader.get_ohlc（Binance 年級 K）組 HistoryPoint。

    與 fetch_real_history 的差異（誠實）：
        - 價格/量/結構/ATR：真值（年級可行）。
        - OI / funding / positioning / CVD：中性化（跨年物理上取不到，見橫幅）。
    顯式呼叫才走這條；既有 fetch_real_history 與週回測排程行為完全不變。"""
    candles = await get_ohlc_yearscale(symbol, tf, days)
    if len(candles) < 200:
        print(f"    [skip] {symbol} 年級 {tf} 只有 {len(candles)} 根，不足")
        return []
    btc_gate = await _btc_gate_series_yearscale(days, "4h")
    return _build_points_from_ohlc(symbol, candles, btc_gate)


async def get_ohlc_yearscale(symbol: str, tf: str, days: int) -> list[dict]:
    """薄包裝 data_loader.get_ohlc（讓本模組單一進入點便於測試/replace）。"""
    from .data_loader import get_ohlc
    return await get_ohlc(symbol, tf, days)


async def fetch_real_history(
    symbol: str,
    days: int = 30,
    cg: CoinGlassSource | None = None,
    *,
    price_only_yearscale: bool = False,
    yearscale_tf: str = "4h",
) -> list[HistoryPoint]:
    """從多源拉真實歷史，組成 HistoryPoint 序列（每小時 1 個）

    Session B 附加（向後相容、預設關閉）：
        price_only_yearscale=True → 改走純價格層年級來源（data_loader Binance 年級 K），
        綜合指標中性化。⚠️只有顯式開啟才改行為；預設 False 時下方多源邏輯完全不變，
        既有呼叫者（replay_real / backtest_session 週回測）零影響。
    """
    if price_only_yearscale:
        # 純價格層年級分支：不碰 CoinGlass（綜合指標跨年取不到，已中性化）。
        return await fetch_real_history_yearscale(symbol, days, yearscale_tf)

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


# ═══════════════════════════════════════════════════════════════════════════
# Session B 附加能力（只建骨架、不改線上行為）：歷史價格層 A/B 回測
#   D6 = 進場區寬度（entry-zone width）：限價掛在 close 的 X% 折讓，未觸不進場。
#   D7 = trailing vs 固定 R：對同一批進場點比較「固定 R 出場」vs「ATR/百分比追蹤」。
# 都走純價格層年級 OHLC（誠實＝跨年可行）；只用 simulator 既有 OHLC 邏輯，零下單。
# 這些是「能力骨架」：協調者可日後接到結構/類比訊號產生器上跑成對比較。
# ═══════════════════════════════════════════════════════════════════════════
from .metrics import aggregate as _aggregate          # noqa: E402
from .validation import assess as _assess              # noqa: E402


def _entry_signals_from_candles(candles: list[dict], *, brk_lookback: int = 20,
                                vol_window: int = 20, vol_z_min: float = 1.8,
                                warmup: int = 60) -> list[dict]:
    """純價格層的簡易進場訊號產生器（突破 + 爆量），供 A/B 骨架共用同一批進場點。
    回 [{idx, direction}]。⚠️無前視：每根 i 只看 candles[:i+1]。"""
    n = len(candles)
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]
    closes = [c["close"] for c in candles]
    vols = [c.get("volume", 0) or 0 for c in candles]
    sigs = []
    for i in range(max(warmup, brk_lookback, vol_window), n - 1):
        vma = (sum(vols[i - vol_window:i]) / vol_window) or 1e-9
        if vols[i] / vma < vol_z_min:
            continue
        prior_high = max(highs[i - brk_lookback:i])
        prior_low = min(lows[i - brk_lookback:i])
        if closes[i] > prior_high:
            sigs.append({"idx": i, "direction": "bull"})
        elif closes[i] < prior_low:
            sigs.append({"idx": i, "direction": "bear"})
    return sigs


def _summary(trades: list, n_trials: int = 1) -> dict:
    m = _aggregate(trades)
    va = _assess([t.realized_r for t in trades], n_trials=n_trials)
    return {"n": m.n_trades, "win_rate": round(m.win_rate * 100, 1),
            "expectancy_r": round(m.expectancy_r, 4),
            "profit_factor": round(m.profit_factor, 2),
            "max_consec_losses": m.max_consecutive_losses,
            "psr": va.get("psr"), "min_trl": va.get("min_trl"),
            "verdict": va.get("verdict")}


async def ab_entry_zone_width(symbol: str, days: int = 365, tf: str = "4h",
                              widths_pct=(0.0, 0.5, 1.0, 1.5),
                              sl_pct: float = 4.0, tp_r=(1.0, 1.5, 2.0),
                              hold_max: int = 48, fill_window: int = 6) -> dict:
    """D6 骨架：比較不同「進場區折讓寬度」對期望值的影響（純價格層、年級）。

    對同一批突破訊號：width=0 → 市價進（close）；width=X% → 限價掛在 close 的
    X% 折讓（多單往下、空單往上），未來 fill_window 根內觸及才算進場，否則放棄。
    回 {width_pct: summary}。誠實：未模擬部分成交/排隊，僅觸價即視為成交。"""
    candles = await get_ohlc_yearscale(symbol, tf, days)
    if len(candles) < 200:
        return {"error": f"insufficient ({len(candles)} bars)", "honesty": YEARSCALE_HONESTY_BANNER}
    sigs = _entry_signals_from_candles(candles)
    n = len(candles)
    out: dict = {"_meta": {"symbol": symbol, "tf": tf, "days": days,
                           "n_signals": len(sigs)},
                 "_honesty": YEARSCALE_HONESTY_BANNER}
    for w in widths_pct:
        trades = []
        for s in sigs:
            i, direction = s["idx"], s["direction"]
            ref = candles[i]["close"]
            if w <= 0:
                entry_idx, entry = i, ref
            else:
                want = ref * (1 - w / 100) if direction == "bull" else ref * (1 + w / 100)
                entry_idx = None
                for j in range(i + 1, min(i + 1 + fill_window, n)):
                    if (direction == "bull" and candles[j]["low"] <= want) or \
                       (direction == "bear" and candles[j]["high"] >= want):
                        entry_idx, entry = j, want
                        break
                if entry_idx is None:
                    continue   # 未成交 → 不進場（這正是 entry-zone 的取捨）
            if direction == "bull":
                stop = entry * (1 - sl_pct / 100); sld = entry - stop
                tps = tuple(entry + sld * r for r in tp_r)
            else:
                stop = entry * (1 + sl_pct / 100); sld = stop - entry
                tps = tuple(entry - sld * r for r in tp_r)
            future = [(candles[k]["ts"], candles[k]["high"], candles[k]["low"],
                       candles[k]["close"])
                      for k in range(entry_idx + 1, min(entry_idx + 1 + hold_max, n))]
            if not future:
                continue
            trades.append(simulate(symbol=symbol, setup_name=f"d6_w{w}",
                                   direction=direction, entry_ts=candles[entry_idx]["ts"],
                                   entry_price=entry, stop=stop, tps=tps,
                                   future_prices=future, hold_max_hours=hold_max))
        out[f"width_{w}pct"] = _summary(trades)
    return out


def _simulate_trailing(symbol: str, direction: str, entry_ts: int, entry: float,
                       stop: float, future: list[tuple], hold_max: int,
                       trail_pct: float):
    """D7 骨架：百分比追蹤停損（取代固定 TP）。從高/低水位回撤 trail_pct% 出場。
    純價格層；保守＝同根先判 stop（與 simulator 一致精神）。回 TradeOutcome-like dict。"""
    sl_dist = abs(entry - stop)
    if sl_dist == 0:
        return {"realized_r": 0.0, "exit_reason": "invalid_stop", "bars_held": 0}
    cost_r = 2 * (0.0005 + 0.0005) * entry / sl_dist   # 對齊 simulator 成本口徑
    eff_stop = stop
    peak = entry
    for k, b in enumerate(future, start=1):
        ts, hi, lo, cl = (b[0], b[1], b[2], b[3]) if len(b) >= 4 else (b[0], b[1], b[1], b[1])
        if k > hold_max:
            break
        # 先判停損（保守）
        if (direction == "bull" and lo <= eff_stop) or (direction == "bear" and hi >= eff_stop):
            r = (eff_stop - entry) / sl_dist if direction == "bull" else (entry - eff_stop) / sl_dist
            return {"realized_r": round(r - cost_r, 4), "exit_reason": "trail_stop", "bars_held": k}
        # 更新水位 + 收緊追蹤停損
        if direction == "bull":
            peak = max(peak, hi)
            eff_stop = max(eff_stop, peak * (1 - trail_pct / 100))
        else:
            peak = min(peak, lo)
            eff_stop = min(eff_stop, peak * (1 + trail_pct / 100))
    # timeout：末根收盤 mark
    last_cl = (future[-1][3] if len(future[-1]) >= 4 else future[-1][1]) if future else entry
    r = (last_cl - entry) / sl_dist if direction == "bull" else (entry - last_cl) / sl_dist
    return {"realized_r": round(r - cost_r, 4), "exit_reason": "trail_timeout",
            "bars_held": len(future)}


async def ab_trailing_vs_fixed(symbol: str, days: int = 365, tf: str = "4h",
                               sl_pct: float = 4.0, tp_r=(1.0, 1.5, 2.0),
                               trail_pct: float = 3.0, hold_max: int = 48) -> dict:
    """D7 骨架：同一批進場點比較「固定 R 出場」vs「百分比追蹤停損」（純價格層、年級）。
    回 {fixed_r: summary, trailing: summary, delta_expectancy_r}。"""
    candles = await get_ohlc_yearscale(symbol, tf, days)
    if len(candles) < 200:
        return {"error": f"insufficient ({len(candles)} bars)", "honesty": YEARSCALE_HONESTY_BANNER}
    sigs = _entry_signals_from_candles(candles)
    n = len(candles)
    fixed_trades = []
    trail_rs: list[float] = []
    for s in sigs:
        i, direction = s["idx"], s["direction"]
        entry = candles[i]["close"]
        if direction == "bull":
            stop = entry * (1 - sl_pct / 100); sld = entry - stop
            tps = tuple(entry + sld * r for r in tp_r)
        else:
            stop = entry * (1 + sl_pct / 100); sld = stop - entry
            tps = tuple(entry - sld * r for r in tp_r)
        future = [(candles[k]["ts"], candles[k]["high"], candles[k]["low"], candles[k]["close"])
                  for k in range(i + 1, min(i + 1 + hold_max, n))]
        if not future:
            continue
        fixed_trades.append(simulate(symbol=symbol, setup_name="d7_fixed",
                                     direction=direction, entry_ts=candles[i]["ts"],
                                     entry_price=entry, stop=stop, tps=tps,
                                     future_prices=future, hold_max_hours=hold_max))
        tr = _simulate_trailing(symbol, direction, candles[i]["ts"], entry, stop,
                                future, hold_max, trail_pct)
        trail_rs.append(tr["realized_r"])

    fixed_sum = _summary(fixed_trades)
    trail_va = _assess(trail_rs) if len(trail_rs) >= 3 else {}
    trail_sum = {"n": len(trail_rs),
                 "expectancy_r": round(sum(trail_rs) / len(trail_rs), 4) if trail_rs else 0.0,
                 "psr": trail_va.get("psr"), "min_trl": trail_va.get("min_trl"),
                 "verdict": trail_va.get("verdict")}
    return {"_meta": {"symbol": symbol, "tf": tf, "days": days, "trail_pct": trail_pct},
            "_honesty": YEARSCALE_HONESTY_BANNER,
            "fixed_r": fixed_sum, "trailing": trail_sum,
            "delta_expectancy_r": round(trail_sum["expectancy_r"] - fixed_sum["expectancy_r"], 4)}


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
