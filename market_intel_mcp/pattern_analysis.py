"""K 線型態分析 helper。

不依賴 LLM 的客觀量化指標：
- 趨勢方向（HH/HL = up，LH/LL = down，混合 = range）
- 支撐 / 阻力（近 N 根高低點密集區）
- 變動率 / ATR
- 量價配合（價跌量縮 = 賣壓出盡 / 價漲量增 = 健康趨勢）
- 關鍵蠟燭型態（吞噬、針狀、十字星、錘子）

給 LLM 一個結構化的「型態觀察報告」，讓它做更深層解讀。
"""
from __future__ import annotations

from statistics import mean, stdev


def trend_direction(candles: list[dict], lookback: int = 20) -> dict:
    """從近 N 根判斷趨勢方向。
    使用 swing highs/lows 簡化判定：HH+HL=up、LH+LL=down、其他=range
    """
    if len(candles) < lookback + 5:
        return {"direction": "unknown", "confidence": 0,
                "note": "insufficient_candles"}

    recent = candles[-lookback:]
    closes = [c["close"] for c in recent]
    highs = [c["high"] for c in recent]
    lows = [c["low"] for c in recent]

    # 切兩半比較
    mid = lookback // 2
    h1 = max(highs[:mid]); h2 = max(highs[mid:])
    l1 = min(lows[:mid]); l2 = min(lows[mid:])

    higher_high = h2 > h1
    higher_low = l2 > l1
    lower_high = h2 < h1
    lower_low = l2 < l1

    if higher_high and higher_low:
        direction = "uptrend"
    elif lower_high and lower_low:
        direction = "downtrend"
    elif higher_low and not higher_high:
        direction = "consolidation_higher_lows"   # 可能突破上行
    elif lower_high and not lower_low:
        direction = "consolidation_lower_highs"   # 可能跌破下行
    else:
        direction = "range"

    # 信心：價格與起點的距離 / ATR
    atr_proxy = mean([c["high"] - c["low"] for c in recent])
    move = abs(closes[-1] - closes[0])
    confidence = min(100, int(move / atr_proxy * 20)) if atr_proxy else 0

    return {
        "direction": direction,
        "confidence": confidence,
        "h1": round(h1, 6), "h2": round(h2, 6),
        "l1": round(l1, 6), "l2": round(l2, 6),
        "change_pct": round((closes[-1] - closes[0]) / closes[0] * 100, 2) if closes[0] else 0,
    }


def support_resistance(candles: list[dict], n_levels: int = 3) -> dict:
    """找出近期密集成交區作為 S/R 候選。
    用 histogram 邏輯：把 price range 分成 20 buckets，找 volume 最大者。
    """
    if len(candles) < 30:
        return {"supports": [], "resistances": [], "note": "insufficient_candles"}

    closes = [c["close"] for c in candles]
    vols = [c["volume"] for c in candles]
    cur = closes[-1]

    lo, hi = min(c["low"] for c in candles), max(c["high"] for c in candles)
    if hi == lo:
        return {"supports": [], "resistances": [], "note": "flat_data"}

    n_buckets = 20
    bucket_vol = [0.0] * n_buckets
    for c in candles:
        mid_price = (c["high"] + c["low"]) / 2
        idx = min(n_buckets - 1, int((mid_price - lo) / (hi - lo) * n_buckets))
        bucket_vol[idx] += c["volume"]

    # 排序找量最大的 buckets
    ranked = sorted(range(n_buckets), key=lambda i: bucket_vol[i], reverse=True)

    supports = []
    resistances = []
    for idx in ranked:
        bucket_price = lo + (idx + 0.5) / n_buckets * (hi - lo)
        if bucket_price < cur:
            if len(supports) < n_levels:
                supports.append({
                    "price": round(bucket_price, 6),
                    "distance_pct": round((cur - bucket_price) / cur * -100, 2),
                    "volume_share": round(bucket_vol[idx] / sum(bucket_vol) * 100, 1),
                })
        elif bucket_price > cur:
            if len(resistances) < n_levels:
                resistances.append({
                    "price": round(bucket_price, 6),
                    "distance_pct": round((bucket_price - cur) / cur * 100, 2),
                    "volume_share": round(bucket_vol[idx] / sum(bucket_vol) * 100, 1),
                })

    return {
        "current_price": round(cur, 6),
        "supports": sorted(supports, key=lambda x: -x["price"])[:n_levels],
        "resistances": sorted(resistances, key=lambda x: x["price"])[:n_levels],
    }


def volume_price_health(candles: list[dict], lookback: int = 30) -> dict:
    """量價配合：上漲時量增 = 健康；下跌時量縮 = 賣壓出盡。"""
    if len(candles) < lookback:
        return {"score": 0, "note": "insufficient_candles"}

    recent = candles[-lookback:]
    up_vols = []
    down_vols = []
    for c in recent:
        if c["close"] > c["open"]:
            up_vols.append(c["volume"])
        elif c["close"] < c["open"]:
            down_vols.append(c["volume"])

    avg_up = mean(up_vols) if up_vols else 0
    avg_down = mean(down_vols) if down_vols else 0

    if avg_up == avg_down == 0:
        return {"score": 0, "note": "no_volume"}

    # 比率 > 1 = 上漲量大、健康看多；< 1 = 下跌量大、看空
    ratio = avg_up / avg_down if avg_down > 0 else float("inf")
    interpretation = (
        "buying_pressure" if ratio > 1.3 else
        "selling_pressure" if ratio < 0.7 else
        "balanced"
    )

    return {
        "up_avg_volume": round(avg_up, 2),
        "down_avg_volume": round(avg_down, 2),
        "up_down_ratio": round(ratio, 3) if ratio != float("inf") else "inf",
        "interpretation": interpretation,
        "up_count": len(up_vols),
        "down_count": len(down_vols),
    }


def key_candlestick_patterns(candles: list[dict], lookback: int = 5) -> list[dict]:
    """檢測近 N 根的關鍵蠟燭型態（吞噬、針狀、十字、錘子）"""
    if len(candles) < 2:
        return []

    patterns = []
    recent = candles[-lookback:]

    for i, c in enumerate(recent):
        body = abs(c["close"] - c["open"])
        upper_shadow = c["high"] - max(c["close"], c["open"])
        lower_shadow = min(c["close"], c["open"]) - c["low"]
        candle_range = c["high"] - c["low"]
        if candle_range == 0:
            continue
        body_ratio = body / candle_range
        upper_ratio = upper_shadow / candle_range
        lower_ratio = lower_shadow / candle_range

        bar_idx_from_end = lookback - 1 - i

        # 十字星 (body < 10% of range)
        if body_ratio < 0.1:
            patterns.append({"pattern": "doji", "bars_ago": bar_idx_from_end,
                             "note": "猶豫信號，可能反轉"})

        # 錘子（長下影，小實體在頂部）
        elif lower_ratio > 0.6 and body_ratio < 0.3 and upper_ratio < 0.1:
            patterns.append({"pattern": "hammer", "bars_ago": bar_idx_from_end,
                             "note": "下跌中可能反轉信號"})

        # 流星（長上影，小實體在底部）
        elif upper_ratio > 0.6 and body_ratio < 0.3 and lower_ratio < 0.1:
            patterns.append({"pattern": "shooting_star", "bars_ago": bar_idx_from_end,
                             "note": "上漲中可能反轉信號"})

        # 看漲吞噬（上一根紅、這根綠 + 完全包覆）
        if i > 0:
            prev = recent[i-1]
            if prev["close"] < prev["open"] and c["close"] > c["open"]:
                if c["close"] >= prev["open"] and c["open"] <= prev["close"]:
                    patterns.append({"pattern": "bullish_engulfing",
                                     "bars_ago": bar_idx_from_end,
                                     "note": "強看漲反轉信號"})

            elif prev["close"] > prev["open"] and c["close"] < c["open"]:
                if c["close"] <= prev["open"] and c["open"] >= prev["close"]:
                    patterns.append({"pattern": "bearish_engulfing",
                                     "bars_ago": bar_idx_from_end,
                                     "note": "強看跌反轉信號"})

    return patterns


def analyze_timeframe(candles: list[dict], symbol: str, interval: str) -> dict:
    """對單一時框做完整型態分析。"""
    if not candles or len(candles) < 10:
        return {"symbol": symbol, "interval": interval, "error": "insufficient_data"}

    return {
        "symbol": symbol, "interval": interval,
        "candle_count": len(candles),
        "trend": trend_direction(candles),
        "sr": support_resistance(candles),
        "volume_price": volume_price_health(candles),
        "patterns": key_candlestick_patterns(candles),
    }


def summarize_multi_tf(symbol: str, by_tf: dict[str, dict]) -> dict:
    """跨時框比較：哪些時框同向 = 強訊號；分歧 = 等待。"""
    timeframes_ordered = ["5m", "15m", "1h", "4h", "12h", "1d", "1w"]
    summary = {"symbol": symbol, "by_tf": {}}
    direction_count = {"uptrend": 0, "downtrend": 0, "range": 0, "other": 0}

    for tf in timeframes_ordered:
        if tf not in by_tf or by_tf[tf].get("error"):
            continue
        candles = by_tf[tf].get("candles", [])
        if not candles:
            continue
        analysis = analyze_timeframe(candles, symbol, tf)
        summary["by_tf"][tf] = analysis
        d = analysis.get("trend", {}).get("direction", "other")
        if d in direction_count:
            direction_count[d] += 1
        elif "uptrend" in d:
            direction_count["uptrend"] += 1
        elif "downtrend" in d:
            direction_count["downtrend"] += 1
        else:
            direction_count["other"] += 1

    # 跨時框共識判斷
    total = sum(direction_count.values())
    if total > 0:
        max_dir = max(direction_count, key=direction_count.get)
        consensus_pct = direction_count[max_dir] / total * 100
        if consensus_pct >= 70:
            consensus = f"strong_{max_dir}"
        elif consensus_pct >= 50:
            consensus = f"weak_{max_dir}"
        else:
            consensus = "mixed"
    else:
        consensus = "unknown"

    summary["direction_count"] = direction_count
    summary["consensus"] = consensus
    return summary
