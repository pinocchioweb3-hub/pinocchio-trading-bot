"""相同條件歷史類比（v18-B）— 每筆訊號附「歷史上類似條件的實證結果」。

抄自競品 SM 浪潮的殺手功能，但口徑更誠實：
    - bracket = 先觸 +1R 或 -1R（與我們實盤 TP1 口徑一致，不是膨脹的 2.5R）
    - 同一根 K 高低同時觸及 → 保守計為輸（v15 對抗驗證的教訓）
    - 樣本 <8 直接說「歷史樣本不足」，不硬擠數字

條件特徵（當下 vs 歷史每根 1h K）：
    1. 24h 動能方向（收盤 vs 24 根前）與當下相同
    2. 該根 K 方向與訊號方向相同
    3. 量能倍數桶（≥2× 或 <2×）與當下相同（樣本不足時放寬）
"""
from __future__ import annotations

import asyncio
import datetime as dt
from statistics import mean, median, pstdev

SL_PCT = 4.0          # 與實盤 intraday 止損一致
FORWARD_BARS = 12     # 向前模擬 12 根 1h
MIN_SAMPLES = 8

# ── 跨年類比（Session B）參數 ──────────────────────────────────────────
CY_TF = "1d"              # 跨年類比用日線（年級可行；綜合指標跨年取不到→只用價格）
CY_WINDOW_BARS = 30       # 「當下情境」與「歷史月窗」各取 30 根日線（≈1 個月）
CY_LOOKBACK_DAYS = 1500   # 回看年數（BTC/ETH/SOL 有 ~3 年；其餘較短自動截斷）
CY_MIN_HISTORY = 120      # 至少要這麼多根日線才做跨年類比，否則「資料不足」


def _vol_mult_at(bars: list[dict], i: int, window: int = 24) -> float | None:
    if i < window:
        return None
    vols = [b.get("volume_usd") or 0 for b in bars[i - window:i]]
    avg = mean(vols) if vols else 0
    cur = bars[i].get("volume_usd") or 0
    return cur / avg if avg > 0 else None


def _momentum_sign(bars: list[dict], i: int, lookback: int = 24) -> int:
    if i < lookback:
        return 0
    prev = bars[i - lookback]["close"]
    return 1 if bars[i]["close"] > prev else (-1 if bars[i]["close"] < prev else 0)


def _bar_dir(b: dict) -> int:
    return 1 if b["close"] > b["open"] else (-1 if b["close"] < b["open"] else 0)


def _simulate_forward(bars: list[dict], i: int, direction: str) -> tuple[float, int] | None:
    """從 bars[i] 收盤進場，向前最多 FORWARD_BARS 根。
    回 (realized_r, bars_held)；資料不足回 None。
    win=+1R 先觸；loss=-1R 先觸；同根雙觸=保守算輸；都沒觸=末根收盤 mark。"""
    if i + 1 >= len(bars):
        return None
    entry = bars[i]["close"]
    sl_dist = entry * SL_PCT / 100
    if direction == "bull":
        tp, sl = entry + sl_dist, entry - sl_dist
    else:
        tp, sl = entry - sl_dist, entry + sl_dist

    end = min(i + 1 + FORWARD_BARS, len(bars))
    for j in range(i + 1, end):
        b = bars[j]
        if direction == "bull":
            hit_tp, hit_sl = b["high"] >= tp, b["low"] <= sl
        else:
            hit_tp, hit_sl = b["low"] <= tp, b["high"] >= sl
        if hit_tp and hit_sl:
            return -1.0, j - i          # 同根雙觸 → 保守算輸
        if hit_sl:
            return -1.0, j - i
        if hit_tp:
            return 1.0, j - i
    # 都沒觸 → mark-to-market
    last = bars[end - 1]["close"]
    r = ((last - entry) if direction == "bull" else (entry - last)) / sl_dist
    return round(r, 3), end - 1 - i


def compute_analogue(bars: list[dict], direction: str,
                     vol_mult_now: float | None) -> dict | None:
    """核心純函式：給 300 根 1h bars + 訊號方向 → 歷史類比統計。"""
    if len(bars) < 60:
        return None
    sig_dir = 1 if direction == "bull" else -1
    mom_now = _momentum_sign(bars, len(bars) - 1)
    vol_bucket_now = (vol_mult_now or 0) >= 2.0

    def _match(i: int, require_vol: bool) -> bool:
        if _momentum_sign(bars, i) != mom_now:
            return False
        if _bar_dir(bars[i]) != sig_dir:
            return False
        if require_vol:
            vm = _vol_mult_at(bars, i)
            if vm is None or ((vm >= 2.0) != vol_bucket_now):
                return False
        return True

    # 先用嚴格條件（含量能桶），樣本不足放寬
    for require_vol in (True, False):
        results = []
        i = 24
        while i < len(bars) - FORWARD_BARS - 1:
            if _match(i, require_vol):
                sim = _simulate_forward(bars, i, direction)
                if sim:
                    results.append(sim)
                    i += 3  # 匹配點間隔 ≥3 根，降低重疊樣本
                    continue
            i += 1
        if len(results) >= MIN_SAMPLES:
            rs = [r for r, _ in results]
            wins = sum(1 for r in rs if r >= 1.0)
            return {
                "n": len(results),
                "win_rate_pct": round(wins / len(results) * 100, 0),
                "avg_r": round(mean(rs), 2),
                "median_hold_h": round(median(h for _, h in results), 0),
                "relaxed": not require_vol,
            }
    # 兩輪都不足
    n_loose = len(results)
    return {"n": n_loose, "insufficient": True}


async def analogue_stats(symbol: str, direction: str,
                          vol_mult_now: float | None = None,
                          timeout_sec: float = 10.0) -> dict | None:
    """抓 300 根 1h K → 類比統計。任何失敗回 None（絕不阻塞訊號推送）。"""
    try:
        async def _run():
            from market_intel_mcp.sources.okx_candles import OkxCandlesSource
            okx = OkxCandlesSource()
            try:
                d = await okx.get_candles(symbol, "1h", 300)
            finally:
                await okx.close()
            bars = d.get("candles") if isinstance(d, dict) else None
            if not bars:
                return None
            return compute_analogue(bars, direction, vol_mult_now)
        return await asyncio.wait_for(_run(), timeout=timeout_sec)
    except Exception:
        return None


def render_analogue_line(stats: dict | None) -> str:
    """渲染成訊息附註行。None/不足 → 誠實標示。"""
    if stats is None:
        return "\n📜 <i>相同條件歷史：數據暫不可用</i>"
    if stats.get("insufficient"):
        return (f"\n📜 <i>相同條件歷史：樣本不足（僅 {stats['n']} 次，"
                f"&lt;{MIN_SAMPLES} 不具統計意義）</i>")
    relax = "（已放寬量能條件）" if stats.get("relaxed") else ""
    icon = "🟢" if stats["avg_r"] > 0 else "🔴"
    return (f"\n📜 <b>相同條件歷史</b>{relax}：近 300 根 1h 出現 "
            f"<code>{stats['n']}</code> 次｜先觸 +1R 機率 "
            f"<code>{stats['win_rate_pct']:.0f}%</code>｜"
            f"平均 <code>{stats['avg_r']:+.2f}R</code> {icon}｜"
            f"中位 <code>{stats['median_hold_h']:.0f}h</code>")


# ═══════════════════════════════════════════════════════════════════════════
# 跨年歷史類比（Session B）：「今年最像 20XX 年 X 月」
#   只用純價格層（年級可行；CoinGlass 綜合指標跨年取不到，不碰、不假裝有）。
#   ⚠️嚴禁前視（沿用 smc_walkforward 三鐵律精神）：
#     1) 歷史候選月窗必須「結束於當下窗起點之前」(end_idx < cur_start)，
#        絕不納入任何 ts ≥ 當下窗的根。
#     2) 特徵向量只由窗內各根算出，不看窗後任何資料。
#     3) 不在比對迴圈內呼叫即時 fetcher（一次預抓整條年級序列，用索引切片）。
# ═══════════════════════════════════════════════════════════════════════════

def _window_features(bars: list[dict], start: int, end: int) -> list[float] | None:
    """算 bars[start:end] 這段的標準化情境特徵向量（純價格）。
    特徵：累積報酬、平均日波幅、波動度(報酬std)、最大回撤、單調趨勢比例。
    任何根缺價回 None。⚠️只用 [start:end) 窗內資料，無前視。"""
    seg = bars[start:end]
    if len(seg) < 5:
        return None
    closes = [b["close"] for b in seg]
    if any(c <= 0 for c in closes):
        return None
    rets = [(closes[k] / closes[k - 1] - 1) for k in range(1, len(closes))]
    cum_ret = closes[-1] / closes[0] - 1
    rng = [((b["high"] - b["low"]) / b["close"]) for b in seg if b["close"] > 0]
    avg_range = mean(rng) if rng else 0.0
    vol = pstdev(rets) if len(rets) >= 2 else 0.0
    # 最大回撤（窗內）
    peak = closes[0]
    max_dd = 0.0
    for c in closes:
        peak = max(peak, c)
        max_dd = max(max_dd, (peak - c) / peak if peak > 0 else 0.0)
    up = sum(1 for r in rets if r > 0)
    trend = (up / len(rets)) if rets else 0.5     # 0.5=無方向；>0.5 偏多
    return [cum_ret, avg_range, vol, max_dd, trend]


def _similarity(a: list[float], b: list[float]) -> float:
    """兩特徵向量的相似度 0..1（用尺度歸一後的歐氏距離轉換）。越接近 1 越像。"""
    # 各維尺度差異大 → 用粗略尺度權重歸一（經驗值，純比較相對遠近）
    scale = [0.30, 0.03, 0.03, 0.20, 0.50]   # cum_ret/avg_range/vol/max_dd/trend
    d2 = 0.0
    for ai, bi, s in zip(a, b, scale):
        d2 += ((ai - bi) / s) ** 2
    dist = d2 ** 0.5
    return 1.0 / (1.0 + dist)


def compute_crossyear_analogue(bars: list[dict],
                               window: int = CY_WINDOW_BARS) -> dict | None:
    """核心純函式：給年級日線 bars（升序）→ 找「當下窗最像歷史哪個月窗」。

    當下窗 = 最後 window 根；候選歷史月窗 = 任何「結束於當下窗起點之前」的 window 根。
    回 {best_month, best_year, similarity_pct, n_candidates, forward_after_best, ...}
    forward_after_best = 該歷史最像月窗「之後」window 根的純價格報酬（誠實：那是
    歷史接下來真的發生了什麼，僅供參考、非預測）。資料不足回 None 或 insufficient。"""
    n = len(bars)
    if n < CY_MIN_HISTORY or n < window * 2 + 2:
        return {"insufficient": True, "n": n}
    cur_start = n - window
    cur_feat = _window_features(bars, cur_start, n)
    if cur_feat is None:
        return {"insufficient": True, "n": n}

    best = None   # (sim, end_idx)
    step = max(1, window // 3)   # 候選窗以 step 滑動，降重疊
    end = cur_start              # 鐵律①：候選窗必須結束於當下窗起點之前
    candidates = 0
    e = window
    while e <= end:
        feat = _window_features(bars, e - window, e)
        if feat is not None:
            sim = _similarity(cur_feat, feat)
            candidates += 1
            if best is None or sim > best[0]:
                best = (sim, e)
        e += step
    if best is None:
        return {"insufficient": True, "n": n}

    sim, best_end = best
    # 最像月窗的中心日期當「年/月」標籤
    center_ts = bars[max(0, best_end - window // 2)]["ts"]
    cdt = dt.datetime.fromtimestamp(center_ts / 1000, dt.timezone.utc)
    # 該歷史月窗之後 window 根的純價格報酬（鐵律①：best_end ≤ cur_start，故 forward 全在過去）
    fwd = None
    fwd_end = min(best_end + window, cur_start)
    if fwd_end - best_end >= max(5, window // 3):
        c0 = bars[best_end - 1]["close"]
        c1 = bars[fwd_end - 1]["close"]
        if c0 > 0:
            fwd = round((c1 / c0 - 1) * 100, 1)
    return {
        "best_year": cdt.year,
        "best_month": cdt.month,
        "similarity_pct": round(sim * 100, 0),
        "n_candidates": candidates,
        "window_bars": window,
        "forward_after_best_pct": fwd,   # 歷史「最像月」之後 ~1 個月的實際走勢（純參考）
        "cur_cum_ret_pct": round(cur_feat[0] * 100, 1),
        "cur_trend": round(cur_feat[4], 2),
    }


async def crossyear_analogue(symbol: str, timeout_sec: float = 15.0) -> dict | None:
    """抓年級日線 → 跨年類比「今年最像 20XX 年 X 月」。任何失敗回 None（不阻塞）。
    走 backtest.data_loader（Binance 年級、免 key、純讀）。綜合指標不碰。"""
    try:
        async def _run():
            from backtest.data_loader import get_ohlc
            bars = await get_ohlc(symbol, CY_TF, CY_LOOKBACK_DAYS)
            if not bars:
                return None
            return compute_crossyear_analogue(bars)
        return await asyncio.wait_for(_run(), timeout=timeout_sec)
    except Exception:
        return None


_MONTH_ZH = ["", "1月", "2月", "3月", "4月", "5月", "6月", "7月", "8月",
             "9月", "10月", "11月", "12月"]


def render_crossyear_line(stats: dict | None) -> str:
    """渲染「今年最像 20XX 年 X 月」附註行（純顯示；帶誠實標示）。"""
    if stats is None:
        return ("\n🗓️ <i>跨年類比：數據暫不可用（純價格層；綜合指標跨年取不到）</i>")
    if stats.get("insufficient"):
        return ("\n🗓️ <i>跨年類比：年級歷史不足，無法比對（純價格層）</i>")
    yr = stats["best_year"]
    mo = _MONTH_ZH[stats["best_month"]] if 1 <= stats["best_month"] <= 12 else f"{stats['best_month']}月"
    sim = stats["similarity_pct"]
    fwd = stats.get("forward_after_best_pct")
    fwd_txt = ""
    if fwd is not None:
        icon = "🟢" if fwd > 0 else "🔴"
        fwd_txt = f"｜那之後約 1 個月實際 <code>{fwd:+.1f}%</code> {icon}"
    return (f"\n🗓️ <b>跨年類比</b>（純價格層）：當下走勢最像 "
            f"<code>{yr} 年 {mo}</code>（相似度 <code>{sim:.0f}%</code>）{fwd_txt}"
            f"\n   <i>⚠️ 僅比對價格形態，未含 OI/CVD/資金費（跨年取不到）；歷史相似≠未來重演</i>")


if __name__ == "__main__":
    async def selftest():
        # 真實 BTC 數據
        for sym, d in (("BTC", "bull"), ("BTC", "bear"), ("ETH", "bull")):
            s = await analogue_stats(sym, d, vol_mult_now=2.5)
            print(f"{sym} {d}: {s}")
            print(" ", render_analogue_line(s).replace("<code>", "").replace("</code>", "")
                  .replace("<b>", "").replace("</b>", "").replace("<i>", "").replace("</i>", "").strip())
        # 合成測試：人造趨勢市
        import random
        random.seed(42)
        bars = []
        px = 100.0
        for i in range(300):
            o = px
            px *= 1 + random.uniform(-0.01, 0.012)  # 微多頭漂移
            bars.append({"open": o, "close": px, "high": max(o, px) * 1.003,
                         "low": min(o, px) * 0.997, "volume_usd": random.uniform(1e6, 5e6)})
        r = compute_analogue(bars, "bull", 1.0)
        print(f"synthetic uptrend bull analogue: {r}")
        assert r and (r.get("n", 0) >= MIN_SAMPLES or r.get("insufficient")), "synthetic failed"
        print("ALL PASS")
    asyncio.run(selftest())
