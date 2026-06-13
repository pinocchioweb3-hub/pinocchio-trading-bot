"""市場狀態（regime）輕量判定（v33）。

研究結論（task wzancy058）：「沒有一套策略能贏到底」「單純匯合≠高勝率」——
正解是 regime 條件權重而非數票數。本模組先提供「顯示用」的 regime 標籤，
讓 deepdive/訊號卡標出當前狀態；之後可接成訊號權重調節器（非硬門檻）。

判定：ADX(14) 趨勢強度 + ATR% 波動分位 → 趨勢/盤整/轉換 × 高/正常/低波動。
純 Python、無第三方相依、對短序列穩健。
"""
from __future__ import annotations

from statistics import mean


def _wilder_smooth(values: list[float], period: int) -> list[float]:
    """Wilder 平滑（RMA）。"""
    if len(values) < period:
        return []
    out = [mean(values[:period])]
    for v in values[period:]:
        out.append((out[-1] * (period - 1) + v) / period)
    return out


def _adx(candles: list[dict], period: int = 14) -> tuple[float | None, float | None, float | None]:
    """回 (ADX, +DI, -DI)。資料不足回 (None,None,None)。"""
    if len(candles) < period * 2 + 1:
        return None, None, None
    trs, plus_dm, minus_dm = [], [], []
    for i in range(1, len(candles)):
        h, l = candles[i]["high"], candles[i]["low"]
        ph, pl, pc = candles[i - 1]["high"], candles[i - 1]["low"], candles[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
        up, dn = h - ph, pl - l
        plus_dm.append(up if (up > dn and up > 0) else 0.0)
        minus_dm.append(dn if (dn > up and dn > 0) else 0.0)
    atr = _wilder_smooth(trs, period)
    sp = _wilder_smooth(plus_dm, period)
    sm = _wilder_smooth(minus_dm, period)
    if not atr or not sp or not sm:
        return None, None, None
    dxs = []
    for a, p, m in zip(atr, sp, sm):
        if a <= 0:
            continue
        pdi, mdi = 100 * p / a, 100 * m / a
        s = pdi + mdi
        dxs.append(100 * abs(pdi - mdi) / s if s else 0.0)
    adx_series = _wilder_smooth(dxs, period)
    if not adx_series:
        return None, None, None
    last_a = atr[-1]
    pdi = 100 * sp[-1] / last_a if last_a else None
    mdi = 100 * sm[-1] / last_a if last_a else None
    return adx_series[-1], pdi, mdi


def classify_regime(candles: list[dict]) -> dict:
    """回 {regime, trend_dir, vol, adx, atr_pct, label}。資料不足回 label='資料不足'。"""
    if not candles or len(candles) < 30:
        return {"regime": None, "label": "資料不足"}
    adx, pdi, mdi = _adx(candles)
    closes = [c["close"] for c in candles]
    # ATR% 與其分位
    trs = []
    for i in range(1, len(candles)):
        h, l, pc = candles[i]["high"], candles[i]["low"], candles[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    atr_pct_series = [trs[i] / closes[i + 1] * 100 for i in range(len(trs)) if closes[i + 1]]
    atr_pct = atr_pct_series[-1] if atr_pct_series else None
    # 波動分位（當前 ATR% 在近窗的位置）
    vol = "正常波動"
    if atr_pct is not None and len(atr_pct_series) >= 20:
        srt = sorted(atr_pct_series)
        rank = sum(1 for x in srt if x <= atr_pct) / len(srt)
        vol = "高波動" if rank >= 0.8 else "低波動" if rank <= 0.2 else "正常波動"
    # 趨勢/盤整
    if adx is None:
        regime, trend_dir = None, None
    elif adx >= 25:
        regime = "趨勢"
        trend_dir = "上" if (pdi or 0) >= (mdi or 0) else "下"
    elif adx < 20:
        regime, trend_dir = "盤整", None
    else:
        regime = "轉換"
        trend_dir = "上" if (pdi or 0) >= (mdi or 0) else "下"
    # 標籤
    if regime is None:
        label = f"波動 {vol}"
    elif regime == "盤整":
        label = f"盤整・{vol}（ADX {adx:.0f}）"
    else:
        label = f"{regime}（{trend_dir}）・{vol}（ADX {adx:.0f}）"
    return {"regime": regime, "trend_dir": trend_dir, "vol": vol,
            "adx": round(adx, 1) if adx is not None else None,
            "atr_pct": round(atr_pct, 2) if atr_pct is not None else None,
            "label": label}


if __name__ == "__main__":
    import asyncio
    from market_intel_mcp.sources.okx_candles import get_okx_candles

    async def t():
        src = get_okx_candles()
        try:
            for sym in ("BTC", "ETH", "SOL"):
                d = await src.get_candles(sym, "4h", 120)
                print(sym, classify_regime(d.get("candles", [])))
        finally:
            await src.close()
    asyncio.run(t())
