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
from statistics import mean, median

SL_PCT = 4.0          # 與實盤 intraday 止損一致
FORWARD_BARS = 12     # 向前模擬 12 根 1h
MIN_SAMPLES = 8


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
