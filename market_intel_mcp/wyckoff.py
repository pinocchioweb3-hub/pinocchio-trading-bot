"""Wyckoff 階段 heuristic 偵測（v33）。

研究結論（task wzancy058）：Wyckoff 定宏觀階段與方向偏置；Spring/UTAD 是高勝率
反轉前置；須用 effort-vs-result（CVD/OI）驗證真假突破。

本模組用「量價 + 交易區間(TR)箱體 + sweep」做 heuristic 粗判，回吸籌/派發階段與
關鍵事件（Spring/UTAD/SOS/SOW）。**heuristic 推定、需人工複核**，僅作圖上敘事與
confluence 參考，不單獨作進場依據。純 Python、無第三方相依。
"""
from __future__ import annotations

from statistics import mean


def _pct(sorted_vals: list[float], q: float) -> float:
    if not sorted_vals:
        return 0.0
    i = max(0, min(len(sorted_vals) - 1, int(q * (len(sorted_vals) - 1))))
    return sorted_vals[i]


def classify_wyckoff(candles: list[dict], cvd_slope: float | None = None,
                     oi_delta_pct: float | None = None,
                     window: int = 40) -> dict:
    """回 {box_lo, box_hi, events:[{type,ago_bars,level}], phase, bias,
    narrative, caveat}。資料不足回 {phase:None}。"""
    n = len(candles)
    if n < 30:
        return {"phase": None, "narrative": "資料不足", "events": []}
    w = min(window, n - 5)
    seg = candles[-w:]
    highs = sorted(c["high"] for c in seg)
    lows = sorted(c["low"] for c in seg)
    # TR 箱體：用 85/15 分位修掉 spring/utad 的極端影線，取「箱體本體」
    box_hi = _pct(highs, 0.85)
    box_lo = _pct(lows, 0.15)
    if box_hi <= box_lo:
        return {"phase": None, "narrative": "資料不足", "events": []}
    vols = [c.get("volume", 0) or 0 for c in seg]
    avg_vol = mean(vols) if vols else 0
    cur = candles[-1]["close"]

    # 事件偵測（在窗內）
    events = []
    for i, c in enumerate(seg):
        ago = w - 1 - i
        hv = avg_vol and c.get("volume", 0) >= 1.5 * avg_vol
        # Spring：刺破箱底但收回箱內
        if c["low"] < box_lo and c["close"] > box_lo:
            events.append({"type": "Spring", "ago_bars": ago, "level": c["low"]})
        # UTAD：刺破箱頂但收回箱內
        elif c["high"] > box_hi and c["close"] < box_hi:
            events.append({"type": "UTAD", "ago_bars": ago, "level": c["high"]})
        # SOS：放量收在箱頂之上（突破力道）
        elif c["close"] > box_hi and hv:
            events.append({"type": "SOS", "ago_bars": ago, "level": c["close"]})
        # SOW：放量收在箱底之下（弱勢破位）
        elif c["close"] < box_lo and hv:
            events.append({"type": "SOW", "ago_bars": ago, "level": c["close"]})
    events = events[-6:]   # 只留最近 6 個

    # 先前趨勢方向（箱體前一段 vs 箱體中點）→ 吸籌 or 派發脈絡
    mid = (box_hi + box_lo) / 2
    prior = candles[max(0, n - w - w // 2):n - w]
    prior_px = mean(c["close"] for c in prior) if prior else mid
    # v181：前段較高＝價格「跌進」箱體＝低位吸籌脈絡；前段較低＝「漲進」箱體＝高位派發。
    # 舊碼此處曾有一行「修正」覆蓋把方向整個反轉（深跌進箱標成派發），2026-08-01 XRP
    # 週線活案例實證誤標（前置均價 2.77 vs 箱頂 2.22、現價貼箱底,被標「高檔派發」）。
    # 該覆蓋已刪除——本行已正確涵蓋全部情形,測試 test_wyckoff_context_v181 雙向鎖死。
    context = "吸籌" if prior_px > mid else "派發"

    recent_types = [e["type"] for e in events if e["ago_bars"] <= 8]
    # 階段與下一關鍵
    if "SOS" in recent_types and cur > box_hi:
        phase, bias, nxt = "Phase D/E", "bull", "回踩確認(LPS)後續漲"
    elif "SOW" in recent_types and cur < box_lo:
        phase, bias, nxt = "Phase D/E", "bear", "反抽確認(LPSY)後續跌"
    elif "Spring" in recent_types:
        phase, bias, nxt = "Phase C", "bull", "SOS 站上箱頂確認"
    elif "UTAD" in recent_types:
        phase, bias, nxt = "Phase C", "bear", "SOW 跌破箱底確認"
    else:
        phase, bias, nxt = "Phase B", None, "等 Spring/UTAD 測試"

    # 讓 context 與近期 bias 一致，避免「吸籌…下一關鍵=SOW」這種矛盾敘事
    if bias == "bull":
        context = "吸籌"
    elif bias == "bear":
        context = "派發"

    # effort-vs-result 驗證（CVD/OI）
    flags = []
    if bias == "bull" and "SOS" in recent_types:
        if (cvd_slope is not None and cvd_slope <= 0) or (oi_delta_pct is not None and oi_delta_pct <= 0):
            flags.append("⚠️ SOS 但 CVD/OI 未同升，疑似假突破")
    if bias == "bear" and "SOW" in recent_types:
        if cvd_slope is not None and cvd_slope >= 0:
            flags.append("⚠️ SOW 但 CVD 未轉弱，留意假跌破")

    narrative = f"{context} {phase}｜下一關鍵={nxt}"
    if flags:
        narrative += "｜" + "；".join(flags)

    # v33 一致性：未進 Phase D/E 不顯示 SOS/SOW（避免「Phase B 卻標 SOS」自相矛盾；
    # 那種多半是失敗突破/假 SOS，回到區間內，不應當確認事件呈現）
    if not phase.startswith("Phase D"):
        events = [ev for ev in events if ev["type"] not in ("SOS", "SOW")]

    return {"box_lo": box_lo, "box_hi": box_hi, "events": events,
            "phase": phase, "bias": bias, "context": context,
            "narrative": narrative, "caveat": "heuristic 推定，需人工複核"}


if __name__ == "__main__":
    import asyncio
    from market_intel_mcp.sources.okx_candles import get_okx_candles

    async def t():
        src = get_okx_candles()
        try:
            for sym in ("BTC", "ETH", "SOL"):
                d = await src.get_candles(sym, "4h", 120)
                r = classify_wyckoff(d.get("candles", []))
                print(sym, "→", r["narrative"],
                      "| box", round(r.get("box_lo", 0), 2), "-", round(r.get("box_hi", 0), 2),
                      "| events", [(e["type"], e["ago_bars"]) for e in r["events"]])
        finally:
            await src.close()
    asyncio.run(t())
