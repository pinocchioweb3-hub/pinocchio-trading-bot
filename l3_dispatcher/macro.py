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


async def compute_per_symbol_state(source, symbol: str) -> dict:
    """組單一標的 deep dive 用的完整資料（含 SMC 量化指標）。"""
    from market_intel_mcp.server import (
        mi_get_etf_flows, mi_get_hyperliquid_whales, mi_get_pattern_analysis,
        mi_get_snapshot,
    )
    from market_intel_mcp.sources.okx_candles import get_okx_candles
    from market_intel_mcp.smc_levels import compute_smc_levels

    # 並行拉所有東西
    candles_src = get_okx_candles()
    pattern, snap, whales, c_4h, c_1d = await asyncio.gather(
        mi_get_pattern_analysis(symbol, ["15m", "1h", "4h", "12h", "1d", "1w"]),
        mi_get_snapshot(symbol, "1h", 96),
        mi_get_hyperliquid_whales(50),
        candles_src.get_candles(symbol, "4h", 200),
        candles_src.get_candles(symbol, "1d", 200),
        return_exceptions=True,
    )

    def _safe(x): return x if isinstance(x, dict) else {"error": str(x)}

    # 跑 SMC 量化指標於 4h（戰術層）與 1d（戰略層）
    smc_levels = {}
    if isinstance(c_4h, dict) and not c_4h.get("error"):
        smc_levels["4h"] = compute_smc_levels(c_4h.get("candles", []), swing_length=10)
    if isinstance(c_1d, dict) and not c_1d.get("error"):
        smc_levels["1d"] = compute_smc_levels(c_1d.get("candles", []), swing_length=5)

    result = {
        "symbol": symbol,
        "ts": dt.datetime.now(tz=dt.timezone.utc),
        "pattern": _safe(pattern),
        "snapshot": _safe(snap),
        "whales": _safe(whales),
        "smc_levels": smc_levels,
    }

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


async def run_per_symbol_loop(tg, source, watchlist, interval_seconds: int = 21600,
                             max_symbols_per_run: int = 3):
    """每 N 秒（預設 6h）對交易層 top N 個強勢幣做 deep dive，每幣一份計畫。

    v12: 已開單品種會被過濾掉，避免重複推同樣的做單機會。
    """
    from .synthesizer import synthesize_per_symbol
    from .trade_journal import get_open_trades

    # 啟動延後
    await asyncio.sleep(min(interval_seconds, 120))

    while True:
        try:
            # 已開單品種（同方向才算重複；不同方向（如 BTC 多 + BTC 空）罕見但理論可行）
            open_syms_set = {o["symbol"] for o in get_open_trades()}
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
                            await _send_to_telegram(
                                tg, text,
                                prefix=f"🎯 <b>{sym} 交易計畫深度分析</b>\n"
                            )
                            # v18-F: 附 SMC 標記圖（失敗不阻塞）
                            try:
                                from .chart_render import render_symbol_chart
                                chart = await render_symbol_chart(sym, "4h", 120)
                                if chart:
                                    await tg.send_photo(
                                        chart, caption=f"📐 {sym} 4H SMC 結構圖")
                            except Exception as e:
                                print(f"[deepdive] chart error: {e}")
                            print(f"[deepdive] {sym} sent ({meta.get('output_chars')} chars)")
                        else:
                            print(f"[deepdive] {sym} synth failed: {meta.get('error')}")
                        # 避免 Telegram 連續發送被限速
                        await asyncio.sleep(2)
                    except Exception as e:
                        print(f"[deepdive] {sym} error: {type(e).__name__}: {e}")
        except Exception as e:
            print(f"[deepdive] loop error: {type(e).__name__}: {e}")
        await asyncio.sleep(interval_seconds)


async def _send_to_telegram(tg, text: str, prefix: str = "") -> int:
    """共用 send + auto-split + plain text fallback。回傳成功發送 parts 數。"""
    import re as _re
    full = f"{prefix}{text}"

    async def _try_send(part: str) -> bool:
        resp = await tg.send_message(part, parse_mode="HTML")
        if resp.get("ok"): return True
        # HTML 失敗 → 剝標籤改純文字（不傳 parse_mode）
        plain = _re.sub(r"<[^>]+>", "", part)
        resp2 = await tg.send_message(plain, parse_mode=None)
        return resp2.get("ok", False)

    sent = 0
    if len(full) > 4096:
        parts, cur = [], ""
        for line in full.split("\n"):
            if len(cur) + len(line) + 1 > 3900:
                parts.append(cur); cur = line
            else:
                cur += ("\n" if cur else "") + line
        if cur: parts.append(cur)
        for i, p in enumerate(parts, 1):
            ok = await _try_send(f"<b>[{i}/{len(parts)}]</b>\n{p}")
            if ok: sent += 1
            await asyncio.sleep(0.5)
    else:
        if await _try_send(full): sent = 1
    return sent


def _next_daily_run_seconds(target_hour_utc: int = 0) -> float:
    """算到下個指定 UTC 小時的秒數（用於 daily macro 排程到 08:00 台北 = 00:00 UTC）"""
    now = dt.datetime.now(tz=dt.timezone.utc)
    target = now.replace(hour=target_hour_utc, minute=0, second=0, microsecond=0)
    if target <= now:
        target += dt.timedelta(days=1)
    return (target - now).total_seconds()


async def run_position_tracker_loop(tg, source, interval_seconds: int = 3600):
    """每小時推一份「持倉追蹤快照」。

    內容：
    - 每筆 open trade：標的 / 方向 / 進場時間 / 進場價 / 當前價 / 距 TP1 / 距 SL / 當前 R
    - 若無 open trades 則不推（避免雜訊）
    """
    from .trade_journal import get_open_trades

    async def _push_snapshot():
        opens = get_open_trades()
        if not opens:
            return  # 無持倉不推

        # 抓所有 symbol 即時價
        symbols = list({o["symbol"] for o in opens})
        prices: dict[str, float] = {}
        for sym in symbols:
            try:
                from market_intel_mcp.sources.okx_candles import OkxCandlesSource
                okx = OkxCandlesSource()
                try:
                    d = await okx.get_candles(sym, "5m", 1)
                finally:
                    await okx.close()
                if isinstance(d, dict) and d.get("candles"):
                    prices[sym] = d["candles"][-1]["close"]
            except Exception as e:
                print(f"[position_tracker] price fetch {sym} error: {e}")

        if not prices:
            return  # 全失敗 → 不推假快照

        # 渲染
        now_ms = int(time.time() * 1000)
        lines = [f"📊 <b>持倉追蹤快照 ({len(opens)} 筆)</b>",
                 f"━━━━━━━━━━━━━━━━"]
        for o in opens:
            sym = o["symbol"]
            cur = prices.get(sym)
            if cur is None:
                lines.append(f"⚪ <b>{sym} {o['direction']}</b> (價格抓取失敗)")
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
                f"{icon} <b>{sym} {o['direction']}</b> (進場 {age_h:.1f}h 前){legs_str}\n"
                f"   進場 <code>${entry:.4f}</code> → 現價 <code>${cur:.4f}</code> "
                f"(<code>{cur_r:+.2f}R</code>)\n"
                f"   距 TP1 <code>{to_tp1_pct:+.2f}%</code>  距 SL <code>{to_sl_pct:+.2f}%</code>"
                if tp1 else
                f"{icon} <b>{sym} {o['direction']}</b> (進場 {age_h:.1f}h 前)\n"
                f"   進場 <code>${entry:.4f}</code> → 現價 <code>${cur:.4f}</code> (<code>{cur_r:+.2f}R</code>)"
            )

        text = "\n\n".join(lines) if False else "\n".join(lines)
        await _send_to_telegram(tg, text)
        print(f"[position_tracker] sent snapshot ({len(opens)} trades)")

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
            from .paper_journal import get_paper_stats, render_paper_summary
            paper_line = render_paper_summary(
                get_paper_stats(30, setup_not="us_breakout"))
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
            state = await compute_macro_state(source, watchlist)
            tradfi = None
            try:
                from market_intel_mcp.sources.tradfi import get_tradfi
                tradfi = await get_tradfi().get_full_snapshot()
            except Exception:
                pass
            text, meta = await synthesize_via_claude_code(state, tradfi, watchlist)
            if text:
                await _send_to_telegram(tg, text, prefix="📅 <b>Daily Macro 啟動版</b>\n")
                print(f"[daily-macro] startup sent ({meta.get('output_chars')} chars)")
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
            state = await compute_macro_state(source, watchlist)
            tradfi = None
            try:
                from market_intel_mcp.sources.tradfi import get_tradfi
                tradfi = await get_tradfi().get_full_snapshot()
            except Exception:
                pass
            text, meta = await synthesize_via_claude_code(state, tradfi, watchlist)
            if text:
                await _send_to_telegram(tg, text, prefix="📅 <b>每日宏觀分析 (08:00 台北)</b>\n")
                print(f"[daily-macro] sent ({meta.get('output_chars')} chars)")
                _mark_daily_macro_sent()
            else:
                print(f"[daily-macro] synth failed: {meta.get('error')}")
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
