"""SMC (Smart Money Concepts) 量化指標。

用 joshyattridge/smart-money-concepts 套件計算：
- FVG (Fair Value Gap)：3 根 K 形成的不平衡缺口
- Order Block：機構掛單區（趨勢前最後反向 K）
- BoS (Break of Structure)：突破結構（趨勢延續）
- CHoCH (Change of Character)：結構翻轉（趨勢轉向）
- Liquidity：流動性聚集區（stop hunt 目標）
- Swing Highs/Lows：擺動高低點

把 OHLC 餵進去 → 拿到精確的結構化價位，給 Claude 寫具體 SMC 交易計畫。
"""
from __future__ import annotations

import warnings

warnings.filterwarnings("ignore", category=FutureWarning)
warnings.filterwarnings("ignore", category=UserWarning)


def candles_to_df(candles: list[dict]):
    """OKX candles list → pandas DataFrame (smc 套件需要的格式)"""
    import pandas as pd
    if not candles:
        return None
    df = pd.DataFrame([
        {"open": c["open"], "high": c["high"], "low": c["low"],
         "close": c["close"], "volume": c["volume"]}
        for c in candles
    ])
    return df


def compute_smc_levels(candles: list[dict], swing_length: int = 10) -> dict:
    """跑全套 SMC 指標、輸出結構化結果（最新 N 個有效訊號）。

    Returns:
        {
            "fvg": [{type, top, bottom, mitigated, ago_bars}, ...],
            "order_blocks": [{type, top, bottom, mitigated, ago_bars, strength}, ...],
            "bos_choch": [{type='BOS'|'CHOCH', direction='bull'|'bear', level, ago_bars}],
            "liquidity": [{type, level, ago_bars}, ...],
            "swing_points": [{type='high'|'low', level, ago_bars}, ...],
            "current_price": float,
            "candle_count": int,
        }
    """
    if not candles or len(candles) < 30:
        return {"error": "insufficient_candles", "needed": 30, "got": len(candles)}

    try:
        from smartmoneyconcepts import smc
    except ImportError as e:
        return {"error": f"smc package not installed: {e}"}

    df = candles_to_df(candles)
    if df is None or len(df) < 30:
        return {"error": "dataframe_conversion_failed"}

    current_price = float(df["close"].iloc[-1])
    n = len(df)
    out = {"current_price": current_price, "candle_count": n}

    try:
        # === Swing Highs / Lows ===
        swings = smc.swing_highs_lows(df, swing_length=swing_length)
        swing_points = []
        # smc 套件回傳同長度 DataFrame，HighLow=1 表 high, -1 表 low
        for i in range(len(swings) - 1, -1, -1):
            hl = swings["HighLow"].iloc[i] if "HighLow" in swings.columns else None
            lvl = swings["Level"].iloc[i] if "Level" in swings.columns else None
            if hl is not None and not _isnan(hl) and lvl is not None and not _isnan(lvl):
                swing_points.append({
                    "type": "high" if hl == 1 else "low",
                    "level": round(float(lvl), 6),
                    "ago_bars": n - 1 - i,
                    "distance_pct": round((float(lvl) - current_price) / current_price * 100, 3),
                })
            if len(swing_points) >= 6:
                break
        out["swing_points"] = swing_points
    except Exception as e:
        out["swing_points_error"] = str(e)
        swings = None

    try:
        # === Order Blocks ===
        if swings is not None:
            ob_df = smc.ob(df, swings, close_mitigation=False)
            order_blocks = []
            for i in range(len(ob_df) - 1, -1, -1):
                ob_type = ob_df["OB"].iloc[i] if "OB" in ob_df.columns else None
                if ob_type is not None and not _isnan(ob_type):
                    top = ob_df["Top"].iloc[i] if "Top" in ob_df.columns else None
                    bot = ob_df["Bottom"].iloc[i] if "Bottom" in ob_df.columns else None
                    mit = ob_df.get("MitigatedIndex", [None] * n)
                    mitigated = mit.iloc[i] if mit is not None and not _isnan(mit.iloc[i] if hasattr(mit, 'iloc') else None) else None
                    if top is not None and bot is not None:
                        order_blocks.append({
                            "type": "bullish" if ob_type == 1 else "bearish",
                            "top": round(float(top), 6),
                            "bottom": round(float(bot), 6),
                            "mitigated": mitigated is not None,
                            "ago_bars": n - 1 - i,
                            "mid_distance_pct": round(((float(top) + float(bot))/2 - current_price) / current_price * 100, 3),
                        })
                if len(order_blocks) >= 5:
                    break
            out["order_blocks"] = order_blocks
    except Exception as e:
        out["order_blocks_error"] = str(e)

    try:
        # === FVG (Fair Value Gap) ===
        fvg_df = smc.fvg(df, join_consecutive=True)
        fvg_list = []
        for i in range(len(fvg_df) - 1, -1, -1):
            f_type = fvg_df["FVG"].iloc[i] if "FVG" in fvg_df.columns else None
            if f_type is not None and not _isnan(f_type):
                top = fvg_df["Top"].iloc[i] if "Top" in fvg_df.columns else None
                bot = fvg_df["Bottom"].iloc[i] if "Bottom" in fvg_df.columns else None
                if top is not None and bot is not None and not _isnan(top) and not _isnan(bot):
                    fvg_list.append({
                        "type": "bullish" if f_type == 1 else "bearish",
                        "top": round(float(top), 6),
                        "bottom": round(float(bot), 6),
                        "ago_bars": n - 1 - i,
                        "mid_distance_pct": round(((float(top) + float(bot))/2 - current_price) / current_price * 100, 3),
                    })
            if len(fvg_list) >= 5:
                break
        out["fvg"] = fvg_list
    except Exception as e:
        out["fvg_error"] = str(e)

    try:
        # === BoS / CHoCH ===
        if swings is not None:
            bc_df = smc.bos_choch(df, swings, close_break=True)
            bos_choch_list = []
            for i in range(len(bc_df) - 1, -1, -1):
                bos = bc_df["BOS"].iloc[i] if "BOS" in bc_df.columns else None
                choch = bc_df["CHOCH"].iloc[i] if "CHOCH" in bc_df.columns else None
                lvl = bc_df["Level"].iloc[i] if "Level" in bc_df.columns else None
                if bos is not None and not _isnan(bos):
                    bos_choch_list.append({
                        "type": "BOS",
                        "direction": "bull" if bos == 1 else "bear",
                        "level": round(float(lvl), 6) if lvl is not None and not _isnan(lvl) else None,
                        "ago_bars": n - 1 - i,
                    })
                elif choch is not None and not _isnan(choch):
                    bos_choch_list.append({
                        "type": "CHOCH",
                        "direction": "bull" if choch == 1 else "bear",
                        "level": round(float(lvl), 6) if lvl is not None and not _isnan(lvl) else None,
                        "ago_bars": n - 1 - i,
                    })
                if len(bos_choch_list) >= 5:
                    break
            out["bos_choch"] = bos_choch_list
    except Exception as e:
        out["bos_choch_error"] = str(e)

    try:
        # === Liquidity ===
        if swings is not None:
            liq_df = smc.liquidity(df, swings, range_percent=0.01)
            liq_list = []
            for i in range(len(liq_df) - 1, -1, -1):
                liq_type = liq_df["Liquidity"].iloc[i] if "Liquidity" in liq_df.columns else None
                if liq_type is not None and not _isnan(liq_type):
                    lvl = liq_df["Level"].iloc[i] if "Level" in liq_df.columns else None
                    if lvl is not None and not _isnan(lvl):
                        liq_list.append({
                            "type": "high_liquidity" if liq_type == 1 else "low_liquidity",
                            "level": round(float(lvl), 6),
                            "ago_bars": n - 1 - i,
                            "distance_pct": round((float(lvl) - current_price) / current_price * 100, 3),
                        })
                if len(liq_list) >= 5:
                    break
            out["liquidity"] = liq_list
    except Exception as e:
        out["liquidity_error"] = str(e)

    return out


def _isnan(x) -> bool:
    """安全的 NaN 檢查（dataframe 取值常常是 NaN）"""
    try:
        import math
        return x is None or (isinstance(x, float) and math.isnan(x))
    except Exception:
        return x is None


def compute_smc_multi_tf(candles_by_tf: dict[str, list[dict]]) -> dict:
    """對多時框各跑 SMC 指標。"""
    out = {}
    for tf, candles in candles_by_tf.items():
        if not candles:
            out[tf] = {"error": "no_candles"}
            continue
        out[tf] = compute_smc_levels(candles)
    return out
