"""宏觀趨勢分析 + 每小時報告（你的關鍵需求）。

對指標層（BTC/ETH/SOL）算：
    7d / 30d / 90d 報酬
    距期內高 (drawdown from peak)
    50d / 90d MA 位置
    regime 分類（heuristic）

對現貨層（SUI/WLFI）：
    當前狀態 + 近期變化

對交易層：
    Top N 強勢分數
    哪些值得關注

合成市場判斷與操作建議。
"""
from __future__ import annotations

import asyncio
import datetime as dt
import time
from typing import Any


# === 趨勢計算 ===
async def fetch_macro_metrics(source, symbol: str, days: int = 90) -> dict:
    """拉 1d × N 根，算趨勢指標"""
    r = await source.get_price_series(symbol, "1d", days)
    if r.get("error"):
        return {"symbol": symbol, "error": r.get("message")}

    series = r.get("series", [])
    if not series:
        return {"symbol": symbol, "error": "empty series"}

    closes = [p["value"] for p in series]
    cur = closes[-1]
    out: dict[str, Any] = {"symbol": symbol, "current_price": cur, "data_points": len(closes)}

    for n_days in (7, 30, 90):
        if len(closes) >= n_days + 1:
            past = closes[-n_days - 1]
            out[f"return_{n_days}d_pct"] = round((cur - past) / past * 100, 2) if past else 0.0

    # Drawdown 從期內高
    peak = max(closes)
    peak_idx = closes.index(peak)
    out["window_high"] = peak
    out["drawdown_from_high_pct"] = round((cur - peak) / peak * 100, 2) if peak else 0.0
    out["days_since_peak"] = len(closes) - 1 - peak_idx

    # MA 位置
    if len(closes) >= 50:
        ma50 = sum(closes[-50:]) / 50
        out["ma50"] = round(ma50, 2)
        out["above_ma50"] = cur > ma50
        out["dist_from_ma50_pct"] = round((cur - ma50) / ma50 * 100, 2) if ma50 else 0.0

    if len(closes) >= 90:
        ma90 = sum(closes[-90:]) / 90
        out["ma90"] = round(ma90, 2)
        out["above_ma90"] = cur > ma90

    return out


def classify_regime(btc: dict) -> str:
    """啟發式分類 BTC 宏觀 regime。"""
    if btc.get("error"):
        return "unknown"

    dd = btc.get("drawdown_from_high_pct", 0) or 0
    ret_7d = btc.get("return_7d_pct", 0) or 0
    ret_30d = btc.get("return_30d_pct", 0) or 0
    above_50 = btc.get("above_ma50", False)
    above_90 = btc.get("above_ma90", False)

    # 由嚴重到溫和判斷
    if dd <= -30 and ret_30d <= -15:
        return "bear_capitulation"    # 熊市恐慌出清
    if dd <= -20 and not above_50:
        return "bear_deleveraging"    # 熊市去槓桿
    if not above_90 and ret_7d < -3:
        return "downtrend"            # 下跌趨勢
    if not above_90 and ret_7d > 0:
        return "potential_reversal"   # 潛在反轉
    if above_50 and above_90 and ret_7d > 3:
        return "strong_uptrend"
    if above_50 and above_90:
        return "uptrend"
    if not above_50 and above_90:
        return "ranging_in_uptrend"
    return "ranging"


def regime_recommendation(regime: str) -> dict:
    """每個 regime 對應的操作建議。"""
    return {
        "bear_capitulation": {
            "label": "熊市恐慌出清",
            "color": "🔴",
            "long_setups": "❌ 全部暫停",
            "short_setups": "⚠️ 已到深處，避免追空",
            "ambush": "✅ 適合分批左側埋伏（小倉、長時框）",
            "wait_for": "BTC 連 3 根 4h 收盤站回 200MA",
        },
        "bear_deleveraging": {
            "label": "熊市去槓桿",
            "color": "🔴",
            "long_setups": "❌ 全部暫停",
            "short_setups": "⚠️ 已在低點區，不建議",
            "ambush": "✅ 可分批埋伏優質標的",
            "wait_for": "BTC 收盤站回 50d MA 並維持 3-5 天",
        },
        "downtrend": {
            "label": "下跌趨勢",
            "color": "🟠",
            "long_setups": "❌ 暫停（除非極強反轉訊號）",
            "short_setups": "⚠️ 跟空風險高，等彈到阻力",
            "ambush": "🟡 可挑強勢標的小倉埋伏",
            "wait_for": "BTC 站回 4h 200MA",
        },
        "potential_reversal": {
            "label": "潛在反轉",
            "color": "🟡",
            "long_setups": "🟡 嚴選 setup A，半倉操作",
            "short_setups": "❌ 不建議",
            "ambush": "✅ 適合進場",
            "wait_for": "BTC 突破近 7d 高 + 站穩",
        },
        "ranging": {
            "label": "盤整",
            "color": "⚪️",
            "long_setups": "🟡 嚴選極強訊號",
            "short_setups": "🟡 嚴選極強訊號",
            "ambush": "⚠️ 等突破方向再說",
            "wait_for": "明確趨勢方向",
        },
        "ranging_in_uptrend": {
            "label": "上升中的盤整",
            "color": "🟢",
            "long_setups": "✅ Setup A 正常運作",
            "short_setups": "❌ 不建議",
            "ambush": "✅ 良好機會",
            "wait_for": "繼續監控",
        },
        "uptrend": {
            "label": "上升趨勢",
            "color": "🟢",
            "long_setups": "✅ Setup A 主要運作",
            "short_setups": "❌ 不建議",
            "ambush": "✅ 主要機會",
            "wait_for": "趨勢延續",
        },
        "strong_uptrend": {
            "label": "強勢上升",
            "color": "🟢",
            "long_setups": "✅ Setup A 全力運作（注意過熱風險）",
            "short_setups": "❌ 不建議",
            "ambush": "🟡 已過好時機，但小倉可試",
            "wait_for": "回調再進場",
        },
        "unknown": {
            "label": "資料不足",
            "color": "⚪️",
            "long_setups": "—", "short_setups": "—",
            "ambush": "—", "wait_for": "等更多資料",
        },
    }.get(regime, {"label": regime, "color": "⚪️"})


# === 完整宏觀掃描 ===
async def compute_macro_state(source, watchlist) -> dict:
    """並行拉指標+現貨+交易層的宏觀指標，組成完整狀態。"""
    indicator_syms = list(watchlist.indicator)
    spot_syms = list(watchlist.spot)

    # 並行拉所有 macro metrics
    all_syms = indicator_syms + spot_syms
    metrics_results = await asyncio.gather(
        *[fetch_macro_metrics(source, s) for s in all_syms],
        return_exceptions=True,
    )
    metrics: dict[str, dict] = {}
    for sym, r in zip(all_syms, metrics_results):
        if isinstance(r, Exception):
            metrics[sym] = {"symbol": sym, "error": str(r)}
        else:
            metrics[sym] = r

    # BTC regime
    btc = metrics.get("BTC", {})
    regime = classify_regime(btc)

    # ETH/BTC ratio
    eth_btc_ratio = None
    if not btc.get("error") and not metrics.get("ETH", {}).get("error"):
        eth_p = metrics["ETH"].get("current_price")
        btc_p = btc.get("current_price")
        if eth_p and btc_p:
            eth_btc_ratio = round(eth_p / btc_p, 6)

    # 額外即時欄位（funding 等）— 用 mi_get_snapshot 拉
    from market_intel_mcp.server import (
        mi_get_etf_flows, mi_get_liquidation_scan, mi_get_sentiment, mi_get_snapshot,
    )
    extras = {}
    for sym in indicator_syms + spot_syms:
        try:
            s = await mi_get_snapshot(sym, "1h", 24)
            extras[sym] = {
                "funding": s.get("funding"),
                "oi_delta_pct": s.get("oi_delta_pct"),
                "btc_gate_open": s.get("btc_gate_open"),
            }
        except Exception as e:
            extras[sym] = {"error": str(e)}

    # 並行拉所有 macro 用資料（12 項一次到位）
    from market_intel_mcp.server import (
        mi_get_funding_outliers, mi_get_funding_arbitrage,
        mi_get_hyperliquid_whales, mi_get_market_cycle_full,
        mi_get_news, mi_get_okx_news, mi_get_options_market,
        mi_get_pattern_analysis, mi_get_spot_futures_basis,
    )
    # 抓 watchlist 全部 symbols 用於新聞過濾
    all_watch_syms = list(watchlist.indicator) + list(watchlist.spot) + (watchlist.trading or [])
    (etf_btc, etf_eth, sentiment, liq_scan, whales, cycle, news,
     funding_outliers, funding_arb, options_btc, options_eth, basis_btc, basis_eth,
     okx_news, pattern_btc, pattern_eth, pattern_sol) = await asyncio.gather(
        mi_get_etf_flows("BTC", 7),
        mi_get_etf_flows("ETH", 7),
        mi_get_sentiment(),
        mi_get_liquidation_scan(15),
        mi_get_hyperliquid_whales(20),
        mi_get_market_cycle_full(),
        mi_get_news(filter_kind="important", page=1),
        mi_get_funding_outliers(10),
        mi_get_funding_arbitrage(5),
        mi_get_options_market("BTC"),
        mi_get_options_market("ETH"),
        mi_get_spot_futures_basis("BTC"),
        mi_get_spot_futures_basis("ETH"),
        mi_get_okx_news(hours_back=72, max_items=20, watchlist_symbols=all_watch_syms),
        mi_get_pattern_analysis("BTC", ["1h", "4h", "12h", "1d", "1w"]),
        mi_get_pattern_analysis("ETH", ["1h", "4h", "12h", "1d", "1w"]),
        mi_get_pattern_analysis("SOL", ["1h", "4h", "12h", "1d", "1w"]),
        return_exceptions=True,
    )

    def _safe(x): return x if isinstance(x, dict) else {"error": str(x)}

    return {
        "ts": dt.datetime.now(tz=dt.timezone.utc),
        "metrics": metrics, "extras": extras,
        "regime": regime, "regime_advice": regime_recommendation(regime),
        "eth_btc_ratio": eth_btc_ratio,
        "etf_btc": _safe(etf_btc), "etf_eth": _safe(etf_eth),
        "sentiment": _safe(sentiment), "liq_scan": _safe(liq_scan),
        "whales": _safe(whales), "cycle": _safe(cycle),
        "news": _safe(news),
        "funding_outliers": _safe(funding_outliers),
        "funding_arb": _safe(funding_arb),
        "options_btc": _safe(options_btc), "options_eth": _safe(options_eth),
        "basis_btc": _safe(basis_btc), "basis_eth": _safe(basis_eth),
        "okx_news": _safe(okx_news),
        "pattern_btc": _safe(pattern_btc),
        "pattern_eth": _safe(pattern_eth),
        "pattern_sol": _safe(pattern_sol),
    }


async def compute_pulse_state(source, watchlist) -> dict:
    """組 Hourly Pulse 用的 delta-focused 數據。短而精，不重複 daily macro 的長敘事。"""
    from market_intel_mcp.server import (
        mi_get_etf_flows, mi_get_funding, mi_get_hyperliquid_whales,
        mi_get_liquidation_scan, mi_get_sentiment,
    )
    from market_intel_mcp.sources.okx_candles import get_okx_candles

    # 並行拉
    indicator_syms = list(watchlist.indicator)
    candles_results, etf_btc, etf_eth, sentiment, liq, whales = await asyncio.gather(
        asyncio.gather(*[
            get_okx_candles().get_candles(s, "1h", 200) for s in indicator_syms
        ]),
        mi_get_etf_flows("BTC", 7),
        mi_get_etf_flows("ETH", 7),
        mi_get_sentiment(),
        mi_get_liquidation_scan(10),
        mi_get_hyperliquid_whales(15),
        return_exceptions=True,
    )

    # 從 1h candles 算 1h / 24h / 3d / 1w delta
    price_deltas = {}
    flow_recent = {}
    for sym, cr in zip(indicator_syms,
                       candles_results if isinstance(candles_results, list) else []):
        if not isinstance(cr, dict) or cr.get("error"):
            price_deltas[sym] = {"error": True}
            continue
        candles = cr.get("candles", [])
        if len(candles) < 168:  # need at least 7 days hourly
            price_deltas[sym] = {"error": True, "msg": "insufficient_data"}
            continue
        cur = candles[-1]["close"]
        c_1h = candles[-2]["close"] if len(candles) >= 2 else cur
        c_24h = candles[-25]["close"] if len(candles) >= 25 else cur
        c_3d = candles[-73]["close"] if len(candles) >= 73 else cur
        c_1w = candles[-169]["close"] if len(candles) >= 169 else cur
        hi24 = max(c["high"] for c in candles[-24:])
        lo24 = min(c["low"] for c in candles[-24:])
        price_deltas[sym] = {
            "current": round(cur, 4),
            "change_1h_pct": round((cur - c_1h) / c_1h * 100, 3) if c_1h else 0,
            "change_24h_pct": round((cur - c_24h) / c_24h * 100, 2) if c_24h else 0,
            "change_3d_pct": round((cur - c_3d) / c_3d * 100, 2) if c_3d else 0,
            "change_1w_pct": round((cur - c_1w) / c_1w * 100, 2) if c_1w else 0,
            "high_24h": round(hi24, 4), "low_24h": round(lo24, 4),
        }

    # Funding 24h 變化（從 history 對比）
    funding_results = await asyncio.gather(
        *[mi_get_funding(s) for s in indicator_syms],
        return_exceptions=True,
    )
    funding_changes = {}
    for sym, fr in zip(indicator_syms, funding_results):
        if isinstance(fr, dict) and not fr.get("error"):
            funding_changes[sym] = {
                "current": fr.get("funding", 0),
                "predicted": fr.get("funding_predicted", 0),
                # 24h change pts: simplified to predicted - current (rough trend proxy)
                "change_24h_pct_points": (fr.get("funding_predicted", 0) or 0) - (fr.get("funding", 0) or 0),
            }

    def _safe(x): return x if isinstance(x, dict) else {"error": str(x)}

    # ETF today (last datapoint vs prior)
    etf_btc_today = {}
    if isinstance(etf_btc, dict) and not etf_btc.get("error"):
        flows = etf_btc.get("series", [])
        today = flows[-1]["flow_usd"] if flows else 0
        cum_3d = sum(f.get("flow_usd", 0) for f in flows[-3:])
        etf_btc_today = {"today_flow_usd": today, "cumulative_3d_flow_usd": cum_3d}
    etf_eth_today = {}
    if isinstance(etf_eth, dict) and not etf_eth.get("error"):
        flows = etf_eth.get("series", [])
        today = flows[-1]["flow_usd"] if flows else 0
        cum_3d = sum(f.get("flow_usd", 0) for f in flows[-3:])
        etf_eth_today = {"today_flow_usd": today, "cumulative_3d_flow_usd": cum_3d}

    return {
        "ts": dt.datetime.now(tz=dt.timezone.utc),
        "price_deltas": price_deltas,
        "funding_changes": funding_changes,
        "flow_recent": flow_recent,
        "liq_today": _safe(liq),
        "etf_btc_today": etf_btc_today,
        "etf_eth_today": etf_eth_today,
        "whales_now": _safe(whales),
        "sentiment_now": _safe(sentiment),
    }


async def _fetch_binance_raw(symbol: str) -> dict:
    """v33：抓 Binance 永續 funding/大戶多空比，供與 OKX/CoinGlass 交叉驗證。失敗回 {}。"""
    try:
        from market_intel_mcp.sources.binance_perp import get_binance_perp
        src = get_binance_perp()

        async def _s(c):
            try:
                return await c
            except Exception:
                return None
        fund, pos = await asyncio.gather(
            _s(src.get_funding(symbol)),
            _s(src.get_positioning(symbol, "4h", 30)),
        )
        out = {}
        if isinstance(fund, dict) and not fund.get("error"):
            out["funding"] = fund.get("funding")
        if isinstance(pos, dict) and not pos.get("error"):
            out["ls_ratio"] = pos.get("latest")
        return out
    except Exception:
        return {}


def _binance_divergence(cg: dict, bn: dict) -> dict:
    """v33：比較 主源(OKX/CoinGlass) vs Binance 的 funding 與大戶多空比，回背離摘要。
    背離大→在分析註記（兩所對同一品種看法分歧＝資訊，不是錯誤）。"""
    out = {"binance": bn, "flags": []}
    cgf, bnf = cg.get("funding"), bn.get("funding")
    if cgf is not None and bnf is not None:
        if (cgf > 0) != (bnf > 0) and abs(cgf - bnf) > 0.0002:
            out["flags"].append(
                f"資金費率跨所背離：主源 {cgf*100:+.4f}% vs Binance {bnf*100:+.4f}%")
    cgl, bnl = cg.get("ls_ratio"), bn.get("ls_ratio")
    if cgl and bnl:
        hi, lo = max(cgl, bnl), max(min(cgl, bnl), 0.01)
        if hi / lo > 1.25:
            out["flags"].append(
                f"大戶多空比跨所背離：主源 {cgl:.2f} vs Binance {bnl:.2f}")
    return out


def _oi_break_note(direction: str, oi_delta_pct: float | None) -> str | None:
    """M4：用突破當下 OI 變化判 BOS/CHoCH 真偽（正交確認，業界共識會計關係）。"""
    if oi_delta_pct is None:
        return None
    if abs(oi_delta_pct) <= 1.0:
        return "OI 持平（突破動能中性）"
    up = oi_delta_pct > 0
    if direction == "bull":
        return "增倉推動（新多進場，真突破）" if up else "空頭回補/減倉（虛漲，留意假突破）"
    return "增倉推動（新空進場，真跌破）" if up else "多頭止損離場（減倉，跌破動能存疑）"


def _oi_sweep_note(oi_delta_pct: float | None) -> str | None:
    """M4：掃單當下 OI 驟降＝清算離場/良性反轉；OI 增＝新倉（可能延續或陷阱）。"""
    if oi_delta_pct is None:
        return None
    if oi_delta_pct < -1.5:
        return "OI 驟降＝清算離場／良性反轉（掃單後反轉機率較高）"
    if oi_delta_pct > 1.5:
        return "OI 增＝新倉進場（掃單後可能延續或陷阱，須配合 CVD）"
    return "OI 持平"


def _most_recent(bc_list: list | None) -> dict | None:
    """取最近一個結構事件（依 ago_bars 最小），避免 4h/1d 兩種排序不一致的坑。"""
    items = [b for b in (bc_list or []) if b.get("direction")]
    if not items:
        return None
    return min(items, key=lambda b: b.get("ago_bars", 1e9))


def _compute_htf_alignment(s4: dict, s1d: dict) -> dict:
    """M2：HTF(1d)→LTF(4h) 對齊驗證。產『已對齊事實』餵 deepdive，
    不在程式層硬否決（disclaimer #5：閘鬆緊是自由參數，須 OOS 回測校準才可收緊）。"""
    out = {"ltf_signal": None, "htf_trend": None, "price_1d_zone": None,
           "direction_aligned": None, "location_favorable": None,
           "verdict": "unknown", "note": ""}
    ltf = _most_recent(s4.get("bos_choch"))
    htf = _most_recent(s1d.get("bos_choch"))
    out["ltf_signal"] = ltf.get("direction") if ltf else None
    out["htf_trend"] = htf.get("direction") if htf else None
    pd1d = s1d.get("premium_discount") or {}
    out["price_1d_zone"] = pd1d.get("zone")

    if out["ltf_signal"] and out["htf_trend"]:
        out["direction_aligned"] = (out["ltf_signal"] == out["htf_trend"])
    if out["ltf_signal"] and out["price_1d_zone"] in ("premium", "discount"):
        out["location_favorable"] = (
            (out["ltf_signal"] == "bull" and out["price_1d_zone"] == "discount")
            or (out["ltf_signal"] == "bear" and out["price_1d_zone"] == "premium"))

    da, lf = out["direction_aligned"], out["location_favorable"]
    # 只有「方向一致 AND 位置有利」兩者皆 True 才算完全對齊（aligned）。
    # 修正：原本把『單邊 True、另一邊 None(資料不足/均衡區)』也升級成 aligned，
    # 會讓 note 謊稱兩者皆有利、在最常見的均衡區系統性高估信心 → 一律降級 partial。
    if da is None and lf is None:
        out["verdict"] = "unknown"
    elif da is True and lf is True:
        out["verdict"] = "aligned"
    elif da is False and lf is False:
        out["verdict"] = "conflict"
    else:
        out["verdict"] = "partial"   # 含一個 False，或單邊 True 另一邊 None

    if out["verdict"] == "partial":
        # 精準文案：區分「其一不利」與「其一資料不足」，不誇大成兩者皆有利
        if da is True and lf is None:
            out["note"] = ("⚠️ HTF 部分對齊：方向順勢一致，但 1d 位於均衡區/區位資料不足，"
                           "未確認折價-溢價有利位置 → 順勢偏置成立但勿放大倉位，等更佳位置")
        elif lf is True and da is None:
            out["note"] = ("⚠️ HTF 部分對齊：1d 區位有利，但 1d 趨勢方向未確立（無 1d 結構）"
                           " → 需 4h 自身結構與獨立數據佐證，勿單據區位進場")
        else:
            out["note"] = ("⚠️ HTF 部分對齊：方向或進場位置其一不利，"
                           "需謹慎、縮小倉位、等更佳位置")
    else:
        _v = {"aligned": "✅ HTF 對齊：1d 趨勢與 4h 訊號一致且位置有利，順勢進場勝率較高",
              "conflict": "⛔ HTF 衝突：4h 訊號與 1d 趨勢/位置相悖（接刀風險），除非有強力獨立確認否則應降權或觀望",
              "unknown": "ℹ️ HTF 對齊未知：1d 結構或區位資料不足，無法判定"}
        out["note"] = _v.get(out["verdict"], "")
    return out


async def compute_per_symbol_state(source, symbol: str) -> dict:
    """組單一標的 deep dive 用的完整資料（含 SMC 量化指標）。"""
    from market_intel_mcp.server import (
        mi_get_etf_flows, mi_get_hyperliquid_whales, mi_get_pattern_analysis,
        mi_get_snapshot,
    )
    from market_intel_mcp.sources.okx_candles import get_okx_candles
    from market_intel_mcp.smc_levels import compute_smc_levels

    # 並行拉所有東西
    from .chart_render import _fetch_coinglass_overlays
    candles_src = get_okx_candles()
    pattern, snap, whales, c_4h, c_1d, cg_ov, bn_raw = await asyncio.gather(
        mi_get_pattern_analysis(symbol, ["15m", "1h", "4h", "12h", "1d", "1w"]),
        mi_get_snapshot(symbol, "1h", 96),
        mi_get_hyperliquid_whales(50),
        candles_src.get_candles(symbol, "4h", 200),
        candles_src.get_candles(symbol, "1d", 200),
        _fetch_coinglass_overlays(symbol, "4h", 120),   # v32: CVD/OI/資金費率/多空比佐證
        _fetch_binance_raw(symbol),                     # v33: Binance 第二來源交叉驗證
        return_exceptions=True,
    )

    def _safe(x): return x if isinstance(x, dict) else {"error": str(x)}

    # 跑 SMC 量化指標於 4h（戰術層）與 1d（戰略層）
    smc_levels = {}
    regime = {"label": "資料不足"}
    wyckoff = {"phase": None}
    _cg = cg_ov if isinstance(cg_ov, dict) else {}
    if isinstance(c_4h, dict) and not c_4h.get("error"):
        c4 = c_4h.get("candles", [])
        smc_levels["4h"] = compute_smc_levels(c4, swing_length=10)
        _n4 = len(c4)
        _oi_list = _cg.get("oi") or []
        _oi_ts = _cg.get("oi_ts") or []   # M4: 跨來源 ts 對齊（OI vs K 線不同源，防錯位）
        try:
            from .chart_render import (
                detect_structure_breaks, _detect_sweeps,
                _oi_delta_around, _liq_clusters)
            _sp4 = smc_levels["4h"].get("swing_points") or []

            def _ev_ts(idx):   # 事件 K 的時間戳（給 _oi_delta_around 做 ts 容差比對）
                return c4[idx]["ts"] if 0 <= idx < _n4 else None

            # v33: 自寫 BOS/CHoCH 取代套件只給 BOS（讓文章也標得出轉勢 CHoCH）
            #      + M4: 用突破當下 OI 變化確認真偽（ts 對齊，對不上回 None 不誤標）
            _bc = []
            for b in detect_structure_breaks(c4, _sp4):
                ago = _n4 - 1 - b["idx"]
                oid = _oi_delta_around(_oi_list, ago, oi_ts=_oi_ts,
                                       event_ts=_ev_ts(b["idx"]))
                _bc.append({"type": b["type"], "direction": b["direction"],
                            "level": b["level"], "ago_bars": ago,
                            "oi_delta_pct": oid,
                            "oi_confirm": _oi_break_note(b["direction"], oid)})
            smc_levels["4h"]["bos_choch"] = _bc
            # H2: 流動性掃單餵進 deepdive（最有 alpha 的 Spring/UTAD，過去只在圖上）
            #      + M4: 掃單當下 OI 確認反轉/陷阱（同樣 ts 對齊）
            _sweeps = []
            for s in _detect_sweeps(c4, _sp4, _n4):
                ago = _n4 - 1 - s["x"]
                oid = _oi_delta_around(_oi_list, ago, oi_ts=_oi_ts,
                                       event_ts=_ev_ts(s["x"]))
                _sweeps.append({"dir": s["dir"], "level": s["level"], "ago_bars": ago,
                                "oi_delta_pct": oid, "oi_confirm": _oi_sweep_note(oid)})
            smc_levels["4h"]["liquidity_sweeps"] = _sweeps
            # M1: 清算密集價帶（事後估計分佈，非真實掛單；標『估計』）
            _lc = _liq_clusters(c4, _cg.get("liq_long_series"),
                                _cg.get("liq_short_series"))
            if _lc:
                _cg["liq_clusters"] = _lc
        except Exception:
            pass
        try:
            from market_intel_mcp.regime import classify_regime
            regime = classify_regime(c4)   # v33: 市場狀態標籤
        except Exception:
            pass
        try:
            from market_intel_mcp.wyckoff import classify_wyckoff
            wyckoff = classify_wyckoff(c4, cvd_slope=_cg.get("cvd_slope"),
                                       oi_delta_pct=_cg.get("oi_delta_24h"))
        except Exception:
            pass
    if isinstance(c_1d, dict) and not c_1d.get("error"):
        smc_levels["1d"] = compute_smc_levels(c_1d.get("candles", []), swing_length=5)

    # M2: HTF(1d)→LTF(4h) 對齊驗證（餵 deepdive 當已對齊事實，不在程式硬否決）
    htf_alignment = _compute_htf_alignment(
        smc_levels.get("4h", {}), smc_levels.get("1d", {}))

    result = {
        "symbol": symbol,
        "ts": dt.datetime.now(tz=dt.timezone.utc),
        "pattern": _safe(pattern),
        "snapshot": _safe(snap),
        "whales": _safe(whales),
        "smc_levels": smc_levels,
        "htf_alignment": htf_alignment,                         # M2: HTF→LTF 對齊驗證
        "regime": regime,                                       # v33: 市場狀態標籤
        "wyckoff": wyckoff,                                      # v33: Wyckoff 階段
        "coinglass": cg_ov if isinstance(cg_ov, dict) else {},   # v32: CoinGlass 佐證序列
        "binance_xcheck": _binance_divergence(                   # v33: Binance 交叉驗證
            cg_ov if isinstance(cg_ov, dict) else {},
            bn_raw if isinstance(bn_raw, dict) else {}),
    }

    # v54-3: #34 多時框趨勢嵌套（影子，純顯示層；只用 OKX 原生時框，
    #        絕不合成 8H/5D。失敗 → 空 dict，永不中斷 deepdive，零訊號數學變更）
    try:
        from market_intel_mcp.timeframe_nesting import build_nesting
        _extra = await candles_src.get_multi_tf(
            symbol, ["1M", "1w", "3d", "2d", "12h"], limit_per_tf=200)
        _by_tf = {
            "4h": c_4h if isinstance(c_4h, dict) else None,   # 上方已取得，重用不重抓
            "1d": c_1d if isinstance(c_1d, dict) else None,
        }
        for _tf, _r in (_extra.get("by_timeframe") or {}).items():
            _by_tf[_tf] = _r
        result["tf_nesting"] = build_nesting(_by_tf)
    except Exception:
        result["tf_nesting"] = {}

    if symbol in ("BTC", "ETH"):
        etf = await mi_get_etf_flows(symbol, 7)
        result[f"etf_{symbol.lower()}"] = _safe(etf)

    return result


def _mark_daily_macro_sent() -> None:
    """v23-2: 記錄 daily macro 發送時間（重啟去重用）"""
    import json as _json
    import time as _t
    from botpaths import data_dir
    try:
        (data_dir() / "daily_macro_state.json").write_text(
            _json.dumps({"last_sent_ts": _t.time()}), encoding="utf-8")
    except Exception:
        pass


async def run_hourly_pulse_loop(tg, source, watchlist, interval_seconds: int = 3600):
    """每小時推送即時動態 pulse（v23-2 差分式：只報與上次相比的變化）"""
    import json as _json
    from botpaths import data_dir
    from telegram_bot.message_format import render_macro_report
    from .synthesizer import synthesize_hourly_pulse

    # 上次報告持久化（重啟不失憶，差分基準連續）
    state_file = data_dir() / "pulse_state.json"
    last_text: str | None = None
    last_ts: str | None = None
    try:
        if state_file.exists():
            st = _json.loads(state_file.read_text(encoding="utf-8"))
            last_text, last_ts = st.get("text"), st.get("ts")
    except Exception:
        pass

    # 啟動延後（讓 daily macro 先跑，避免衝突）
    await asyncio.sleep(min(interval_seconds, 60))

    while True:
        try:
            pulse_state = await compute_pulse_state(source, watchlist)
            text, meta = await synthesize_hourly_pulse(
                pulse_state, last_pulse_text=last_text, last_pulse_ts=last_ts)
            if text:
                # v18-E: pulse 頂部固定一行市場廣度（全市場 356 檔即時統計）
                breadth_prefix = ""
                try:
                    from .market_scanner import get_latest_breadth, render_breadth_line
                    breadth_prefix = render_breadth_line(get_latest_breadth()) + "\n"
                except Exception:
                    pass
                await _send_to_telegram(
                    tg, text, prefix=f"⚡ <b>每小時即時動態</b>\n{breadth_prefix}")
                print(f"[pulse] sent ({meta.get('output_chars')} chars)")
                # v23-2: 存為下次的差分基準
                last_text = text
                last_ts = dt.datetime.now(tz=dt.timezone.utc).strftime("%m-%d %H:%M UTC")
                try:
                    state_file.write_text(
                        _json.dumps({"text": last_text, "ts": last_ts},
                                    ensure_ascii=False),
                        encoding="utf-8")
                except Exception:
                    pass
            else:
                print(f"[pulse] synth failed: {meta.get('error')}")
        except Exception as e:
            print(f"[pulse] error: {type(e).__name__}: {e}")
        await asyncio.sleep(interval_seconds)


def _deepdive_extra_context(sym: str, sym_state: dict | None) -> dict:
    """v56-2 治本：從 deepdive 早已抓好的 sym_state 萃取 plan_snapshot 影子 context，
    純資料重用、零新網路請求（這些值 chart/synthesizer 早已在用，過去卻沒餵進快照）：

      • wyckoff_phase          ← sym_state.wyckoff.phase（classify_wyckoff 4h 階段）
      • whale_net              ← 該標的 HL 鯨魚淨多倉百分比（per_symbol_aggregate 配對 symbol）
      • macro_confluence_score ← 市場層宏觀共振分數（讀末行，進場時凍結的宏觀背景）

    缺料 → 該鍵省略（assemble 只覆蓋非 None，未列鍵維持骨架 None＝誠實留空，紅線③）。
    全程 exception-safe：壞掉只是少記一格 context，絕不影響任何 FIRE/下單/建單路徑。
    抽成獨立純函式以利單元測試（不必跑整個 deepdive 迴圈）。"""
    out: dict = {}
    try:
        _wyk = ((sym_state or {}).get("wyckoff") or {}).get("phase")
        if _wyk is not None:
            out["wyckoff_phase"] = _wyk
        _wh = (sym_state or {}).get("whales") or {}
        if isinstance(_wh, dict) and not _wh.get("error"):
            _sw = next((w for w in _wh.get("per_symbol_aggregate", [])
                        if isinstance(w, dict) and w.get("symbol") == sym), None)
            if _sw is not None and _sw.get("net_long_pct") is not None:
                out["whale_net"] = _sw.get("net_long_pct")
        _mcs = _read_macro_confluence_score()
        if _mcs is not None:
            out["macro_confluence_score"] = _mcs
    except Exception:
        return {}
    return out


def _record_deepdive_plan(sym: str, plan: dict | None,
                          signal_msg_id: int | None = None,
                          sym_state: dict | None = None) -> dict | None:
    """v33：把 deepdive 可執行計畫存進紙上帳（等待觸發），回給圖表用的 plan dict。
    不可做單 / 缺關鍵價位 / 已有同標的 open 倉 → 不重複建單。任何錯誤回 None，不阻塞。

    v56：sym_state（compute_per_symbol_state 的完整資料）若有提供，會把其中『已抓好的』
    per-symbol 觀測（snapshot 的 OI/資金費/CVD/大戶比/btc_regime/200MA + 4h regime 趨勢
    方向）餵進進場快照的 regime/context 影子向量——治本 deepdive oi_price_quadrant 恆 None。"""
    if not plan or not plan.get("actionable"):
        return None
    direction = plan.get("direction")
    entry = plan.get("entry")
    stop = plan.get("stop")
    tp1, tp2, tp3 = plan.get("tp1"), plan.get("tp2"), plan.get("tp3")
    if direction not in ("bull", "bear") or stop is None or tp1 is None:
        return None
    # 限價分批：用區間中點當名目進場價
    lo, hi = plan.get("entry_lo"), plan.get("entry_hi")
    is_limit = (plan.get("entry_type") == "limit") and lo is not None and hi is not None
    if entry is None:
        entry = (lo + hi) / 2 if is_limit else None
    if entry is None:
        return None
    chart_plan = {"entry": entry, "stop": stop, "tp1": tp1, "tp2": tp2,
                  "tp3": tp3, "direction": direction}
    try:
        from .paper_journal import record_paper_entry, open_paper_symbols
        if sym in open_paper_symbols("deepdive"):
            return chart_plan   # 已在追蹤：畫線但不重複建單
        _tp2 = tp2 if tp2 is not None else tp1
        _tp3 = tp3 if tp3 is not None else tp1
        # v56 step1：進場那刻凍結計畫快照（純觀測，失敗回 None 不阻塞建單）
        try:
            from .plan_snapshot import build_plan_snapshot
            from .regime_vector import assemble as _asm_rg
            # v56 治本：過去這裡傳 _asm_rg(None)，deepdive 永遠沒有 per-symbol 觀測 →
            #   oi_price_quadrant 恆 None，優化器 (symbol,quadrant) 分桶退化成 per-symbol-only，
            #   架空「per-symbol×per-regime」目標。改重用 deepdive 早已抓好的同一份資料：
            #     • snapshot（mi_get_snapshot：oi_delta_pct/funding/cvd_slope/cvd 背離/大戶比/
            #       btc_regime/above_4h_200ma，欄名已對齊 assemble，零新網路請求）
            #     • per-symbol 4h regime 的 trend_dir（上/下）→ 推『市場已觀測價格方向』給象限分類
            #   純資料重用，零訊號/下單數學變更；任何缺料安全降級為 None（與舊行為等價）。
            _snap_for_rv = None
            try:
                _ss = (sym_state or {}).get("snapshot")
                if isinstance(_ss, dict) and not _ss.get("error"):
                    _snap_for_rv = dict(_ss)   # 淺拷貝，不污染 sym_state 下游用途（圖表等）
                    _td = ((sym_state or {}).get("regime") or {}).get("trend_dir")
                    if _td is not None:
                        _snap_for_rv["regime_trend_dir"] = _td
            except Exception:
                _snap_for_rv = None
            # v56-2 治本：deepdive 早已算好的 extra_context 過去全丟棄 → wyckoff_phase/
            #   whale_net/macro_confluence_score 恆 None。同樣純資料重用、零新請求、缺料→None。
            _extra_ctx = _deepdive_extra_context(sym, sym_state)
            _rv, _ctx = _asm_rg(_snap_for_rv, direction=direction,
                                extra_context=_extra_ctx or None)
            # v56-2：vol_trend 過去誤填來源標籤 "deepdive"（來源已存於 source 欄）。改與
            #   direct_fire/waiting_trigger 同口徑——用共用的 vol_regime_from_atr() 把 deepdive
            #   早已抓好的 snapshot.atr_pct_7d 分桶成 ATR 波動度（趨勢方向另由 oi_price_quadrant
            #   承載）。三條加密進場路徑同一語意同一門檻，lessons_store 讀 vol_trend 不再混義；
            #   缺 atr → "unknown"（誠實留空，與舊路徑等價）。
            from .plan_snapshot import vol_regime_from_atr
            _vol_regime = vol_regime_from_atr((_snap_for_rv or {}).get("atr_pct_7d"))
            plan_snap = build_plan_snapshot(
                source="macro_deepdive", direction=direction,
                entry_price=entry, planned_stop=stop,
                tp1=tp1, tp2=_tp2, tp3=_tp3,
                signal_msg_id=signal_msg_id, regime=_vol_regime,
                regime_vector=_rv, context=_ctx)
        except Exception:
            plan_snap = None
        record_paper_entry(
            symbol=sym, setup="deepdive", direction=direction,
            entry_price=entry, stop_price=stop,
            tp1=tp1, tp2=_tp2, tp3=_tp3,
            regime="deepdive",
            zone_lo=lo if is_limit else None, zone_hi=hi if is_limit else None,
            split_mode=is_limit, signal_msg_id=signal_msg_id,
            plan_snapshot=plan_snap,
        )
        print(f"[deepdive] {sym} paper entry recorded ({direction}, "
              f"{'limit' if is_limit else 'market'})")
    except Exception as e:
        print(f"[deepdive] {sym} paper record error: {type(e).__name__}: {e}")
    return chart_plan


async def run_per_symbol_loop(tg, source, watchlist, interval_seconds: int = 21600,
                             max_symbols_per_run: int = 3):
    """每 N 秒（預設 6h）對交易層 top N 個強勢幣做 deep dive，每幣一份計畫。

    v12: 已開單品種會被過濾掉，避免重複推同樣的做單機會。
    """
    from . import symbol_gate
    from .paper_journal import open_paper_symbols
    from .synthesizer import synthesize_per_symbol
    from .trade_journal import get_open_trades

    # 啟動延後
    await asyncio.sleep(min(interval_seconds, 120))

    while True:
        try:
            # 已開單品種：實倉(get_open_trades，紙上模式恆空)∪ 紙上 deepdive 持倉。
            # v47-2: 過去只看實倉表（紙上模式恆空）＝形同 no-op，於是 deepdive 每 6h 會對
            #        仍持倉的同幣「重發」🎯 深度分析（symbol_gate 1h 窗早過），使用者看起來
            #        就像重複單。改為也排除紙上 deepdive 持倉 → 持倉期間不再重發同幣 🎯。
            open_syms_set = ({o["symbol"] for o in get_open_trades()}
                             | open_paper_symbols("deepdive"))
            # 只跑 trading tier 排除已開單後的 top N
            candidates = [s for s in (watchlist.trading or []) if s not in open_syms_set]
            symbols = candidates[:max_symbols_per_run]
            if open_syms_set:
                print(f"[deepdive] excluding open trades: {sorted(open_syms_set)} "
                      f"(candidates: {candidates[:8]})")
            if not symbols:
                print(f"[deepdive] no trading tier symbols (all open or empty), skipping")
            else:
                print(f"[deepdive] analyzing {len(symbols)} symbols: {symbols}")
                for sym in symbols:
                    try:
                        sym_state = await compute_per_symbol_state(source, sym)
                        text, meta = await synthesize_per_symbol(sym, sym_state)
                        if text:
                            # v47/v48: 跨來源收斂閘——改用 claim() 原子搶槽，消除「should_send
                            #      唯讀檢查 → await 送 TG → mark_sent」之間的 TOCTOU：deepdive 與
                            #      scheduler 常同輪喚醒，原本可雙雙通過唯讀檢查各送一單 → 重複。
                            #      claim 在送出前一刻原子搶槽，只有一個來源搶得到；送出失敗才 release。
                            _dir = (meta.get("plan") or {}).get("direction")
                            if _dir in ("bull", "bear") and not symbol_gate.claim(sym, _dir):
                                print(f"[deepdive] {sym} {_dir} 跳過：symbol_gate 跨來源冷卻中"
                                      f"（已被搶槽/近期已推同幣同向，避免重複單）")
                                await asyncio.sleep(2)
                                continue
                            sig_mid = await _send_to_telegram(
                                tg, text,
                                prefix=(f"🎯 <b>{sym} 交易計畫深度分析</b>\n"
                                        + await _shadow_observe_prefix(sym_state))
                            )
                            if _dir in ("bull", "bear") and not sig_mid:
                                # v48: 送出失敗 → 歸還搶到的槽，下輪可重試（不靜默漏單）
                                symbol_gate.release(sym, _dir)
                            # v33: 把可執行計畫接進紙上帳（看得到的報單→可追蹤可算勝率），
                            #      存原始訊號 message_id 供持倉回連。失敗不阻塞。
                            plan = meta.get("plan")
                            chart_plan = _record_deepdive_plan(sym, plan, sig_mid,
                                                               sym_state=sym_state)
                            # v18-F: 附 SMC 標記圖（v33 帶計畫線；失敗不阻塞）
                            try:
                                from .chart_render import render_symbol_chart
                                # v33: 傳 deepdive 已抓的同一份 CoinGlass+Wyckoff，圖文同源不打架
                                _ov = dict(sym_state.get("coinglass") or {})
                                _ov["wyckoff"] = sym_state.get("wyckoff")
                                chart = await render_symbol_chart(sym, "4h", 120,
                                                                  plan=chart_plan,
                                                                  overlays=_ov)
                                if chart:
                                    cap = f"📐 {sym} 4H SMC＋全指標結構圖"
                                    if chart_plan:
                                        cap += "（已建紙上追蹤）"
                                    await tg.send_photo(chart, caption=cap)
                            except Exception as e:
                                print(f"[deepdive] chart error: {e}")
                            print(f"[deepdive] {sym} sent ({meta.get('output_chars')} chars)"
                                  f"{' +paper' if chart_plan else ''}")
                        else:
                            print(f"[deepdive] {sym} synth failed: {meta.get('error')}")
                        # 避免 Telegram 連續發送被限速
                        await asyncio.sleep(2)
                    except Exception as e:
                        print(f"[deepdive] {sym} error: {type(e).__name__}: {e}")
        except Exception as e:
            print(f"[deepdive] loop error: {type(e).__name__}: {e}")
        await asyncio.sleep(interval_seconds)


def _shadow_tf_nesting_line(sym_state: dict) -> str:
    """#34 影子顯示：把多時框嵌套階段組成「一行」確定性文字（不過 LLM）。

    純讀 sym_state['tf_nesting']（compute_per_symbol_state 已算好），任何缺料/錯誤回 ""。
    嚴守紅線③：只敘述「結構階段／層對齊」客觀事實，無勝率/報酬%/年化等績效字眼；
    明標「僅 OKX 原生時框、無 8H/5D 合成」「觀察中」，避免被當已驗證進場訊號。
    """
    try:
        nest = sym_state.get("tf_nesting") or {}
        n = int(nest.get("layer_count") or 0)
        stage = nest.get("stage_label")
        if n <= 0 or not stage:
            return ""
        align_pct = int(round(float(nest.get("alignment_score") or 0.0) * 100))
        side = (nest.get("trade_side") or {}).get("side")
        side_txt = {"right": "・右側順勢", "left": "・左側佈局"}.get(side, "")
        fb = nest.get("false_break") or {}
        fb_txt = "・⚠️疑似假突破" if fb.get("is_false_break") else ""
        return (f"🪜 多時框階段：{stage}（{n}層・對齊{align_pct}%{side_txt}{fb_txt}"
                f"；僅OKX原生時框1M/1w/1d/3d/2d/12h/4h，觀察中）\n")
    except Exception:
        return ""


async def _shadow_crossyear_line(symbol: str) -> str:
    """(A) 影子顯示：跨年類比『今年最像 20XX 年 X 月』一行（純讀、不過 LLM）。

    只對加密；任何失敗回 ''，絕不中斷 deepdive。crossyear_analogue 走
    backtest.data_loader（Binance 年級、免 key、純讀，自帶 15s timeout＋回 None）；
    render_crossyear_line 對 None/insufficient 都回安全字串，且已內含
    『歷史相似≠未來重演』誠實橫幅，故此處不重複加。
    嚴守影子鐵則：結果僅進顯示字串，永不進 strength_score/fire_queue/symbol_gate/下單。
    """
    try:
        from .analogue import crossyear_analogue, render_crossyear_line
        stats = await crossyear_analogue(symbol)
        return render_crossyear_line(stats)
    except Exception:
        return ""


def _shadow_convergence_focus_line() -> str:
    """#33 影子顯示：讀跨源匯流 JSONL 最後一輪，列「三方共現焦點幣」橫幅（不過 LLM）。

    純讀 data_dir()/convergence_shadow.jsonl 最後一行；不存在/空/壞 JSON 回 ""。
    嚴守紅線③：只敘述「OKX∧Binance∧CoinGlass 資金費率方向一致」此一事實，非「買進訊號」；
    標「觀察中／參考」；此為全市場焦點榜（非當前幣專屬），故用橫幅式文案以免被誤當該幣訊號。
    不顯示 strength_multiplier_SHADOW（避免被當已生效權重）。
    """
    try:
        import json as _json
        from botpaths import data_dir
        path = data_dir() / "convergence_shadow.jsonl"
        if not path.exists():
            return ""
        with open(path, "rb") as f:  # tail 讀，避免載入整個 5MB sink
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 65536))
            chunk = f.read()
        lines = [ln for ln in chunk.decode("utf-8", errors="ignore").splitlines()
                 if ln.strip()]
        if not lines:
            return ""
        rec = _json.loads(lines[-1])  # 取最後一行（可能因 tail 切斷較舊行，但末行完整）
        focus = [it for it in (rec.get("focus") or []) if it.get("triple_present")]
        focus.sort(key=lambda it: it.get("convergence_score") or 0.0, reverse=True)
        syms = [it.get("symbol") for it in focus[:5] if it.get("symbol")]
        if not syms:
            return ""
        return ("📊 本輪三方共現焦點幣（OKX∧Binance∧CoinGlass 資金費率方向一致，"
                f"觀察中／參考）：{'、'.join(syms)}\n")
    except Exception:
        return ""


async def _shadow_observe_prefix(sym_state: dict) -> str:
    """組合 #34（當前幣階段）+ #33（全市場焦點橫幅）+ (A) 跨年類比三段影子觀測，
    附在 deepdive 標題下。

    全程只讀、不過 LLM、任何錯誤回 ""，絕不中斷 deepdive 發送。
    (A) 跨年類比僅對加密（emoji=='🪙'）顯示——deepdive 引擎也會選到美股如 MU，
    用 _asset_kind 資產別守門擋掉美股，避免污染美股卡。跨年行加在最後
    （render_crossyear_line 開頭已含 '\\n'）。
    """
    try:
        sym = sym_state.get("symbol", "")
        emoji, _ = _asset_kind(sym, "")   # deepdive 引擎會選到美股如 MU
        cross = await _shadow_crossyear_line(sym) if emoji == "🪙" else ""
        return (_shadow_tf_nesting_line(sym_state)
                + _shadow_convergence_focus_line()
                + cross)
    except Exception:
        return ""


async def _send_to_telegram(tg, text: str, prefix: str = "") -> int:
    """共用 send + auto-split + plain text fallback。
    v33：回傳第一則訊息的 message_id（>0 代表成功、可當連結錨點；0=失敗）。"""
    import re as _re
    full = f"{prefix}{text}"

    async def _try_send(part: str) -> int:
        resp = await tg.send_message(part, parse_mode="HTML")
        if resp.get("ok"):
            return resp.get("result", {}).get("message_id", 0) or 0
        # HTML 失敗 → 剝標籤改純文字（不傳 parse_mode）
        plain = _re.sub(r"<[^>]+>", "", part)
        resp2 = await tg.send_message(plain, parse_mode=None)
        return resp2.get("result", {}).get("message_id", 0) or 0 if resp2.get("ok") else 0

    first_id = 0
    if len(full) > 4096:
        parts, cur = [], ""
        for line in full.split("\n"):
            if len(cur) + len(line) + 1 > 3900:
                parts.append(cur); cur = line
            else:
                cur += ("\n" if cur else "") + line
        if cur: parts.append(cur)
        for i, p in enumerate(parts, 1):
            mid = await _try_send(f"<b>[{i}/{len(parts)}]</b>\n{p}")
            if i == 1:
                first_id = mid
            await asyncio.sleep(0.5)
    else:
        first_id = await _try_send(full)
    return first_id


def _next_daily_run_seconds(target_hour_utc: int = 0) -> float:
    """算到下個指定 UTC 小時的秒數（用於 daily macro 排程到 08:00 台北 = 00:00 UTC）"""
    now = dt.datetime.now(tz=dt.timezone.utc)
    target = now.replace(hour=target_hour_utc, minute=0, second=0, microsecond=0)
    if target <= now:
        target += dt.timedelta(days=1)
    return (target - now).total_seconds()


def _macro_confluence_dashboard_line() -> str:
    """(B) 影子顯示：讀 macro_confluence.jsonl 末行 → render_dashboard 儀表板字串。

    純讀、不過 LLM、不二次打 API；檔缺/空/壞 JSON 回 ''。來源＝
    run_macro_confluence_loop 每小時已寫的整份 summary（含 macro_confluence_score/
    bias/components/risk_off/n_present/ts），讀末行＝零 API、零重算、與影子層解耦。
    render_dashboard 已內含『綜合宏觀儀表板（影子觀測，非進場訊號）』橫幅＋
    『永不影響訊號/下單』註腳，故此處不重複加。
    嚴守影子鐵則：macro_confluence_score 僅進顯示字串，未乘進/加進任何訊號數學。
    沿用 _shadow_convergence_focus_line 的 tail-read 樣式（seek 檔尾 65536 bytes）。
    """
    try:
        import json as _json
        from botpaths import data_dir
        from .macro_confluence import render_dashboard
        path = data_dir() / "macro_confluence.jsonl"
        if not path.exists():
            return ""
        with open(path, "rb") as f:  # tail 讀（仿 _shadow_convergence_focus_line）
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 65536))
            chunk = f.read()
        lines = [ln for ln in chunk.decode("utf-8", errors="ignore").splitlines()
                 if ln.strip()]
        if not lines:
            return ""
        rec = _json.loads(lines[-1])  # 取最後一行（tail 可能切斷較舊行，但末行完整）
        return render_dashboard(rec)
    except Exception:
        return ""


def _read_macro_confluence_score():
    """純讀 macro_confluence.jsonl 末行的 macro_confluence_score（市場層宏觀背景，
    進場時凍結用）。檔缺/空/壞 JSON/無此鍵/非數值 → None（誠實留空，紅線③）。
    零 API、零重算，與 _macro_confluence_dashboard_line 同源同 tail-read 樣式，
    只取原始分數而非渲染字串。供 _deepdive_extra_context 萃取進場快照用。"""
    try:
        import json as _json
        from botpaths import data_dir
        path = data_dir() / "macro_confluence.jsonl"
        if not path.exists():
            return None
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 65536))
            chunk = f.read()
        lines = [ln for ln in chunk.decode("utf-8", errors="ignore").splitlines()
                 if ln.strip()]
        if not lines:
            return None
        rec = _json.loads(lines[-1])
        v = rec.get("macro_confluence_score")
        return v if isinstance(v, (int, float)) else None
    except Exception:
        return None


# v36：持倉資產分類（決定 🪙加密 / 🇺🇸美股 / 🥇商品 標記）
_COMMODITY_SYMBOLS = {"XAU", "XAG", "XPT", "XPD"}


def _asset_kind(symbol: str, setup: str = "") -> tuple[str, str]:
    """回 (emoji, 中文標籤)。

    美股：白名單命中或 setup=='us_breakout'（deepdive 引擎也會選到美股如 MU，
    故不能只看 setup）。XAU 等貴金屬歸商品。其餘視為加密。
    """
    from .us_stocks import US_STOCK_WATCHLIST
    sym = (symbol or "").upper()
    if sym in _COMMODITY_SYMBOLS:
        return "🥇", "商品"
    if setup == "us_breakout" or sym in US_STOCK_WATCHLIST:
        return "🇺🇸", "美股"
    return "🪙", "加密"


async def run_position_tracker_loop(tg, source, interval_seconds: int = 3600):
    """每小時推一份「持倉追蹤快照」。

    內容：
    - 每筆持倉：資產別(🪙/🇺🇸/🥇) / 標的 / 方向 / 進場時間 / 進場價 / 當前價 /
      距 TP1 / 距 SL / 當前 R，並附「🔗原始訊號」回連到當初發單的那一分鐘貼文
    - 來源：實盤 trades（紅線不下實彈→恆空）+ 紙上驗證 paper_trades（真實追蹤）
    - 若無持倉則不推（避免雜訊）
    """
    from .trade_journal import get_open_trades

    async def _push_snapshot():
        from .paper_journal import get_open_paper
        from .trade_monitor import _signal_link

        live = get_open_trades()      # 實盤（紅線：不下實彈 → 目前恆為空）
        paper = get_open_paper()      # 紙上驗證持倉（真實追蹤標的，已濾掉未成交掛單）

        # 統一形狀：附資產別連結用 msg_id（實盤用 tg_message_id、紙上用 signal_msg_id）
        positions = []
        for o in live:
            positions.append({**o, "_kind": "live",
                              "_link_id": o.get("tg_message_id")})
        for o in paper:
            positions.append({**o, "_kind": "paper",
                              "_link_id": o.get("signal_msg_id")})

        if not positions:
            return  # 無持倉不推

        # 抓所有 symbol 即時價（單一 OKX client 重用，避免每檔開關連線）
        symbols = list({o["symbol"] for o in positions})
        prices: dict[str, float] = {}
        from market_intel_mcp.sources.okx_candles import OkxCandlesSource
        okx = OkxCandlesSource()
        try:
            for sym in symbols:
                try:
                    d = await okx.get_candles(sym, "5m", 1)
                    if isinstance(d, dict) and d.get("candles"):
                        prices[sym] = d["candles"][-1]["close"]
                except Exception as e:
                    print(f"[position_tracker] price fetch {sym} error: {e}")
        finally:
            await okx.close()

        if not prices:
            return  # 全失敗 → 不推假快照

        # 渲染
        now_ms = int(time.time() * 1000)
        n_paper = sum(1 for o in positions if o["_kind"] == "paper")
        n_live = len(positions) - n_paper
        if n_live:
            head = f"📊 <b>持倉追蹤快照（紙上 {n_paper}　實盤 {n_live}）</b>"
        else:
            head = f"📊 <b>持倉追蹤快照（紙上驗證 {n_paper} 筆）</b>"
        lines = [head, "━━━━━━━━━━━━━━━━"]
        for o in positions:
            sym = o["symbol"]
            a_emoji, _ = _asset_kind(sym, o.get("setup", ""))
            link = _signal_link(tg, o.get("_link_id"))
            cur = prices.get(sym)
            if cur is None:
                lines.append(f"⚪ {a_emoji}<b>{sym} {o['direction']}</b> (價格抓取失敗){link}")
                continue

            entry = o["entry_price"]; stop = o["stop_price"]; tp1 = o.get("tp1")
            age_h = (now_ms - o["entry_at"]) / 3600000
            sl_distance = abs(entry - stop)

            # 當前 R
            if o["direction"] == "bull":
                cur_r = (cur - entry) / sl_distance
                to_tp1_pct = (tp1 - cur) / cur * 100 if tp1 else None
                to_sl_pct = (cur - stop) / cur * 100
            else:
                cur_r = (entry - cur) / sl_distance
                to_tp1_pct = (cur - tp1) / cur * 100 if tp1 else None
                to_sl_pct = (stop - cur) / cur * 100

            # 狀態 icon
            if cur_r >= 1.0:
                icon = "🎯"  # 已過 TP1（理論上 monitor 已平 50%）
            elif cur_r >= 0.5:
                icon = "🟢"  # 半路
            elif cur_r >= 0:
                icon = "🟡"
            elif cur_r >= -0.5:
                icon = "🟠"
            else:
                icon = "🔴"  # 接近停損

            legs_str = ""
            if o["legs_hit"]:
                legs_str = f"  已過：<code>{','.join(sorted(o['legs_hit']))}</code>"
            lines.append(
                f"{icon} {a_emoji}<b>{sym} {o['direction']}</b> (進場 {age_h:.1f}h 前){legs_str}{link}\n"
                f"   進場 <code>${entry:.4f}</code> → 現價 <code>${cur:.4f}</code> "
                f"(<code>{cur_r:+.2f}R</code>)\n"
                f"   距 TP1 <code>{to_tp1_pct:+.2f}%</code>  距 SL <code>{to_sl_pct:+.2f}%</code>"
                if tp1 else
                f"{icon} {a_emoji}<b>{sym} {o['direction']}</b> (進場 {age_h:.1f}h 前){link}\n"
                f"   進場 <code>${entry:.4f}</code> → 現價 <code>${cur:.4f}</code> (<code>{cur_r:+.2f}R</code>)"
            )

        text = "\n".join(lines)
        await _send_to_telegram(tg, text)
        print(f"[position_tracker] sent snapshot "
              f"({len(positions)} positions: {n_paper} paper / {n_live} live)")

    # 啟動延後
    await asyncio.sleep(30)

    while True:
        try:
            await _push_snapshot()
        except Exception as e:
            print(f"[position_tracker] error: {type(e).__name__}: {e}")
        await asyncio.sleep(interval_seconds)


async def run_performance_loop(tg, target_hour_utc: int = 0,
                              run_on_startup: bool = False):
    """每天 target_hour_utc 推一份「過去 7 天 / 30 天績效總結」+ 當前風控狀態。

    給 user 看：bot 是否真有賺、勝率分佈、各 setup 表現。
    """
    from .risk_manager import get_risk_status, render_risk_status
    from .trade_journal import get_stats, render_stats_summary

    async def _push():
        try:
            stats7 = get_stats(7)
            stats30 = get_stats(30)
            risk = get_risk_status()

            # v16: 紙上驗證進度（引擎期望值）
            # v23-2: Stage 0 門檻只算加密引擎；美股實驗引擎獨立一行
            # v26: 訂單漏斗（提出/進場/無效/部分/止盈止損）
            from .paper_journal import (get_paper_stats, render_paper_summary,
                                        render_paper_funnel)
            paper_line = (render_paper_funnel(30, setup_not="us_breakout") + "\n\n" +
                          render_paper_summary(get_paper_stats(30, setup_not="us_breakout")))
            us = get_paper_stats(30, setup="us_breakout")
            if us["n_closed"] or us["n_open"]:
                paper_line += (f"\n🧪 美股紙上（實驗）30d：已平 <code>{us['n_closed']}</code> 筆 "
                               f"勝率 <code>{us['win_rate_pct']}%</code> "
                               f"期望值 <code>{us['avg_r']:+.2f}R</code>/筆")

            text = (
                render_stats_summary(stats7, label="📈 過去 7 天（實倉）") + "\n\n" +
                render_stats_summary(stats30, label="📊 過去 30 天（實倉）") + "\n\n" +
                paper_line + "\n\n" +
                render_risk_status(risk)
            )
            await _send_to_telegram(tg, text, prefix="📅 <b>每日績效總結</b>\n\n")
            print(f"[performance] sent: 7d={stats7['n_trades_closed']} closed, "
                  f"30d={stats30['n_trades_closed']} closed")

            # v21-B: 成績卡圖片（最近 6 筆已平倉，輸贏都上卡）
            try:
                from .report_card import render_report_cards
                card = render_report_cards(6)
                if card:
                    await tg.send_photo(card, caption="🗂 最近平倉成績卡（紙上驗證帳，不挑單）")
            except Exception as e:
                print(f"[performance] report card error: {type(e).__name__}: {e}")

            # v66: 累積 R 走勢圖（朋友回饋 Q1：像交易所跟單系統的績效走勢圖）
            #      純讀 paper_trades，各引擎分線，誠實標註「紙上模擬・非實盤」。
            try:
                from .equity_curve import render_equity_curve
                curve = render_equity_curve()
                if curve:
                    await tg.send_photo(curve, caption=(
                        "📈 紙上驗證帳累積 R 走勢（非實盤績效・兩引擎分線・"
                        "美股樣本不足不可作績效宣稱）"))
            except Exception as e:
                print(f"[performance] equity curve error: {type(e).__name__}: {e}")
        except Exception as e:
            print(f"[performance] error: {type(e).__name__}: {e}")

    if run_on_startup:
        await asyncio.sleep(90)
        await _push()

    while True:
        wait = _next_daily_run_seconds(target_hour_utc)
        next_run_at = dt.datetime.now(tz=dt.timezone.utc) + dt.timedelta(seconds=wait)
        print(f"[performance] next at {next_run_at.strftime('%Y-%m-%d %H:%M UTC')}")
        await asyncio.sleep(wait)
        await _push()


async def run_daily_macro_loop(tg, source, watchlist, target_hour_utc: int = 0,
                               run_on_startup: bool = True):
    """每天 target_hour_utc 點推送完整 daily macro（預設 00:00 UTC = 08:00 台北）"""
    from telegram_bot.message_format import render_macro_report
    from .synthesizer import synthesize_via_claude_code

    async def _compute_and_send(prefix: str) -> bool:
        """算宏觀狀態 → Claude 敘事化推送；回傳是否真的送出了東西。

        v49 修復：舊版若 Claude 敘事引擎（CLI）離線 → text=None → 整篇 daily macro
        **靜默消失**，使用者那天什麼都收不到、也不知道為何。現在比照 run_macro_loop
        的既有設計，敘事失敗時降級為 render_macro_report 模板版，使用者仍收得到當日
        宏觀數據，並誠實標示「非 AI 解讀」（紅線③：不假裝模板是 AI 寫的）。
        """
        state = await compute_macro_state(source, watchlist)
        tradfi = None
        try:
            from market_intel_mcp.sources.tradfi import get_tradfi
            tradfi = await get_tradfi().get_full_snapshot()
        except Exception:
            pass
        text, meta = await synthesize_via_claude_code(state, tradfi, watchlist)
        if text:
            # (B) 影子顯示：每日宏觀卡附「綜合宏觀儀表板（影子觀測，非進場訊號）」。
            #     純讀本地 jsonl 末行；dash 為空時 prefix 原樣、daily macro 照常送。
            dash = _macro_confluence_dashboard_line()
            await _send_to_telegram(
                tg, text,
                prefix=(prefix + dash + "\n") if dash else prefix)
            print(f"[daily-macro] sent ({meta.get('output_chars')} chars)")
            return True
        # 敘事引擎離線/失敗 → 降級模板版（不再靜默消失）
        err = (meta or {}).get("error", "unknown")
        print(f"[daily-macro] LLM 敘事失敗（{err}）→ 降級模板版")
        try:
            tmpl = render_macro_report(state, watchlist)
        except Exception as e:
            print(f"[daily-macro] 模板版也失敗，今日無法推送：{type(e).__name__}: {e}")
            return False
        if not tmpl:
            print("[daily-macro] 模板版為空，今日無法推送")
            return False
        await _send_to_telegram(
            tg, tmpl,
            prefix="📅 <b>每日宏觀分析（精簡模板版）</b>\n"
                   "<i>⚠️ AI 敘事引擎暫時離線，以下為數據模板版（非 AI 解讀）。</i>\n")
        print(f"[daily-macro] 模板降級版已送出（{len(tmpl)} chars）")
        return True

    # 啟動時可選跑一次（避免等到隔天）
    # v23-2: 6 小時內已發過就跳過 — 重啟頻繁時不再轟炸完整 Daily Macro
    if run_on_startup:
        import json as _json
        from botpaths import data_dir
        dm_state = data_dir() / "daily_macro_state.json"
        try:
            if dm_state.exists():
                st = _json.loads(dm_state.read_text(encoding="utf-8"))
                age_h = (dt.datetime.now(tz=dt.timezone.utc).timestamp()
                         - st.get("last_sent_ts", 0)) / 3600
                if age_h < 6:
                    print(f"[daily-macro] startup skip（{age_h:.1f}h 前已發過）")
                    run_on_startup = False
        except Exception:
            pass
    if run_on_startup:
        await asyncio.sleep(60)
        try:
            if await _compute_and_send("📅 <b>Daily Macro 啟動版</b>\n"):
                _mark_daily_macro_sent()
        except Exception as e:
            print(f"[daily-macro] startup error: {e}")

    # 主迴圈：每天 00:00 UTC 跑一次
    while True:
        wait = _next_daily_run_seconds(target_hour_utc)
        next_run_at = dt.datetime.now(tz=dt.timezone.utc) + dt.timedelta(seconds=wait)
        print(f"[daily-macro] next run at {next_run_at.strftime('%Y-%m-%d %H:%M UTC')} (in {wait/3600:.1f}h)")
        await asyncio.sleep(wait)
        try:
            if await _compute_and_send("📅 <b>每日宏觀分析 (08:00 台北)</b>\n"):
                _mark_daily_macro_sent()
        except Exception as e:
            print(f"[daily-macro] error: {type(e).__name__}: {e}")


async def run_macro_loop(tg, source, watchlist, interval_seconds: int = 3600,
                          use_llm: bool = True):
    """每 N 秒推一則完整宏觀分析到 Telegram。
    use_llm=True 用 Claude Code Headless 敘事化（Max 訂閱免費）；
    失敗時自動降級到 template renderer。
    """
    from telegram_bot.message_format import render_macro_report
    from .synthesizer import synthesize_via_claude_code

    # 啟動先等一個間隔，避免和 startup 衝
    await asyncio.sleep(min(interval_seconds, 60))
    while True:
        try:
            state = await compute_macro_state(source, watchlist)

            # 同時拉 TradFi（Yahoo Finance 跨資產）
            tradfi = None
            try:
                from market_intel_mcp.sources.tradfi import get_tradfi
                tradfi = await get_tradfi().get_full_snapshot()
            except Exception as e:
                print(f"[macro] tradfi fetch failed: {e}")

            # LLM 敘事化（預設）
            text = None
            mode_used = "template"
            if use_llm:
                text, meta = await synthesize_via_claude_code(
                    state, tradfi, watchlist
                )
                if text:
                    mode_used = "llm"
                    print(f"[macro] LLM synth OK ({meta.get('input_chars')} → {meta.get('output_chars')} chars)")
                else:
                    print(f"[macro] LLM failed: {meta.get('error')} → fallback template")
                    text = render_macro_report(state, watchlist)
            else:
                text = render_macro_report(state, watchlist)

            # Telegram 4096 字限制 → 分段送 + 驗證接收 + 失敗降級純文字
            import re as _re
            sent_ok = 0
            sent_fail = 0

            async def _send_with_fallback(part_text: str, prefix: str = "") -> bool:
                """送 HTML，失敗則剝掉標籤改純文字。回傳是否成功。"""
                full = f"{prefix}{part_text}" if prefix else part_text
                resp = await tg.send_message(full, parse_mode="HTML")
                if resp.get("ok"):
                    return True
                err_desc = resp.get("description", "unknown")
                print(f"[macro] HTML rejected: {err_desc[:120]}")
                # 剝掉所有 HTML 標籤改純文字
                plain = _re.sub(r"<[^>]+>", "", full)
                resp2 = await tg.send_message(plain, parse_mode=None)
                if resp2.get("ok"):
                    print(f"[macro] plain text fallback OK")
                    return True
                print(f"[macro] plain text also failed: {resp2.get('description', '?')[:120]}")
                return False

            if len(text) > 4096:
                parts = []
                cur = ""
                for line in text.split("\n"):
                    if len(cur) + len(line) + 1 > 3900:
                        parts.append(cur); cur = line
                    else:
                        cur += ("\n" if cur else "") + line
                if cur: parts.append(cur)
                for i, p in enumerate(parts, 1):
                    ok = await _send_with_fallback(p, f"<b>[{i}/{len(parts)}]</b>\n")
                    if ok: sent_ok += 1
                    else: sent_fail += 1
                    await asyncio.sleep(0.5)
            else:
                ok = await _send_with_fallback(text)
                if ok: sent_ok += 1
                else: sent_fail += 1

            if sent_fail == 0:
                print(f"[macro] sent OK (regime={state['regime']}, mode={mode_used}, parts={sent_ok})")
            else:
                print(f"[macro] PARTIAL FAIL (sent={sent_ok}, failed={sent_fail})")
        except Exception as e:
            print(f"[macro] error: {type(e).__name__}: {e}")
        await asyncio.sleep(interval_seconds)
