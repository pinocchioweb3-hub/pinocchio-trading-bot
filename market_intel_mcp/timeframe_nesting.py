"""多時框趨勢嵌套（timeframe nesting）純函式核心 — task#34。

「剝洋蔥」式多時框分析：把大週期到小週期的趨勢一層層攤開，
找出 (1) 主導趨勢、(2) 從頂到哪一層仍同向（嵌套深度）、(3) 第一個翻向的層，
並映射到 7 個「行情階段」標籤，輔以左/右側交易偏置與假突破偵測。

設計鐵則（本模組）：
    * 純函式、零第三方相依、無任何 API 呼叫。輸入是「已備好的」OHLCV dict
      （格式同 sources/okx_candles.py：升序、含 ts/open/high/low/close/volume/...）。
    * 只負責「顯示偏置」與「結構描述」，**不下單、不否決訊號**。
    * 對短序列 / 缺層 / 空輸入穩健，永不拋例外，降級回 'unknown'。

接線（dispatcher / synthesizer / macro）需使用者先拍板 7 個 stage 命名等決策，
本輪僅交付純函式核心 + 離線單元測試，**不接線**。
"""
from __future__ import annotations

from statistics import mean

# 可複用 regime 的 ADX（同套 Wilder 平滑，風格一致）。若 import 失敗則退回自帶輕量版。
try:  # pragma: no cover - import 路徑差異的保險
    from market_intel_mcp.regime import _adx as _adx
except Exception:  # pragma: no cover
    try:
        from .regime import _adx as _adx  # type: ignore
    except Exception:  # pragma: no cover
        def _wilder_smooth(values: list[float], period: int) -> list[float]:
            if len(values) < period:
                return []
            out = [mean(values[:period])]
            for v in values[period:]:
                out.append((out[-1] * (period - 1) + v) / period)
            return out

        def _adx(candles: list[dict], period: int = 14):  # type: ignore
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


# ===========================================================================
# 時框順序 & 階段標籤
# ===========================================================================

# 大 → 小。OKX 無原生 5D（D-Q5 待使用者拍板），此版先省略 5D。
# 3d/2d 順序：以「週期長度」由大到小排（3d > 2d）。
TF_ORDER = ["1M", "1w", "1d", "3d", "2d", "12h", "8h", "4h"]


# 階段中文命名：使用者 2026-06-18 拍板採「SMC/Wyckoff 專業術語版」（非白話版）。
#    用中性英文碼當 key，方便日後抽換 value 而不動程式邏輯。
STAGE_LABELS: dict[str, str] = {
    "UP_TREND": "上升擴張",      # 趨勢明確向上、各層共振
    "UP_PULLBACK": "多頭回撤",   # 大層多頭、小層回檔（右側續多 / 回踩找多）
    "TOP_WATCH": "派發見頂",     # 高位轉弱、疑似派發
    "RANGE": "盤整",             # 區間震盪、方向未明
    "BOTTOM_WATCH": "吸籌築底",  # 低位轉強、疑似吸籌
    "DOWN_BOUNCE": "空頭反彈",   # 大層空頭、小層反彈（右側續空 / 反彈找空）
    "DOWN_TREND": "下降擴張",    # 趨勢明確向下、各層共振
}


# ===========================================================================
# 內部 helper
# ===========================================================================

def _slope_sign(closes: list[float]) -> int:
    """近窗 close 線性回歸斜率符號（tie-breaker）。回 1 上 / -1 下 / 0 平。

    用最小平方法的斜率分子（sum((x-x̄)(y-ȳ))）符號即可，不需完整除法。
    """
    n = len(closes)
    if n < 2:
        return 0
    xbar = (n - 1) / 2.0
    ybar = mean(closes)
    num = sum((i - xbar) * (closes[i] - ybar) for i in range(n))
    if abs(num) < 1e-12:
        return 0
    return 1 if num > 0 else -1


def _last_swing(highs: list[float], lows: list[float]) -> str | None:
    """切前後兩半比較，回最近一段的擺動結構標記 HH/HL/LH/LL 或 None。

    借鏡 pattern_analysis.trend_direction 的切半法並正規化為 SMC 用語：
        h2>h1 = Higher High；l2>l1 = Higher Low；反之 LH/LL。
    優先序：先看高點再看低點，回單一最具代表性的標記。
    """
    n = len(highs)
    if n < 4:
        return None
    mid = n // 2
    h1, h2 = max(highs[:mid]), max(highs[mid:])
    l1, l2 = min(lows[:mid]), min(lows[mid:])
    hh, hl = h2 > h1, l2 > l1
    lh, ll = h2 < h1, l2 < l1
    if hh and hl:
        return "HH"      # 多頭結構（高點抬高為主標）
    if lh and ll:
        return "LL"      # 空頭結構（低點降低為主標）
    if hl and not hh:
        return "HL"      # 低點抬高、高點未過 → 收斂偏多
    if lh and not ll:
        return "LH"      # 高點走低、低點未破 → 收斂偏空
    return None


# ===========================================================================
# 單時框趨勢分類
# ===========================================================================

def classify_tf_trend(candles: list[dict], lookback: int = 20) -> dict:
    """單一時框趨勢分類（三源融合）。

    三源：
        1. swing 結構（HH/HL → up；LH/LL → down）
        2. ADX（≥25 趨勢 / <20 區間）+ DI 方向
        3. 近 lookback 根 close 線性回歸斜率（tie-breaker）

    回 dict:
        direction:    'up' | 'down' | 'range' | 'unknown'
        strength:     0-100（綜合 ADX + 位移幅度）
        change_pct:   近窗首尾 close 變動百分比
        swing_high:   近窗最高
        swing_low:    近窗最低
        price_position: 現價在 [swing_low, swing_high] 的相對位置 0-1
        last_swing:   'HH'|'HL'|'LH'|'LL'|None
        adx:          ADX 值（或 None）

    資料 < lookback+5 → direction='unknown'。
    """
    if not candles or len(candles) < lookback + 5:
        return {
            "direction": "unknown", "strength": 0, "change_pct": 0.0,
            "swing_high": None, "swing_low": None, "price_position": None,
            "last_swing": None, "adx": None, "note": "insufficient_candles",
        }

    recent = candles[-lookback:]
    closes = [c["close"] for c in recent]
    highs = [c["high"] for c in recent]
    lows = [c["low"] for c in recent]

    swing_high = max(highs)
    swing_low = min(lows)
    cur = closes[-1]
    rng = swing_high - swing_low
    price_position = (cur - swing_low) / rng if rng > 0 else 0.5
    price_position = max(0.0, min(1.0, price_position))
    change_pct = (cur - closes[0]) / closes[0] * 100 if closes[0] else 0.0

    last_swing = _last_swing(highs, lows)

    # ADX 用「完整」candles（需要 period*2+1 根；regime._adx 內部會自行判不足）
    adx, pdi, mdi = _adx(candles)
    slope = _slope_sign(closes)

    # --- 三源融合定方向 ---
    # 1) 結構票
    struct_vote = 0
    if last_swing in ("HH", "HL"):
        struct_vote = 1
    elif last_swing in ("LH", "LL"):
        struct_vote = -1

    # 2) ADX 票（趨勢時才出方向；區間 → 視為 range 傾向）
    adx_trending = adx is not None and adx >= 25
    adx_ranging = adx is not None and adx < 20
    adx_vote = 0
    if adx_trending:
        adx_vote = 1 if (pdi or 0) >= (mdi or 0) else -1

    # 3) 斜率票（tie-breaker）
    slope_vote = slope

    score = struct_vote + adx_vote + slope_vote

    if adx_ranging and struct_vote == 0:
        # ADX 明確區間且結構不明 → range（不被斜率拉走）
        direction = "range"
    elif score > 0:
        direction = "up"
    elif score < 0:
        direction = "down"
    else:
        # 票數打平：結構不明 + 無趨勢 → range
        direction = "range"

    # --- strength 0-100：ADX 為主軸（0~50 映 0~100），疊加位移幅度 ---
    if adx is not None:
        adx_component = min(100.0, adx / 50.0 * 100.0)
    else:
        adx_component = 0.0
    atr_proxy = mean([h - l for h, l in zip(highs, lows)]) or 1e-9
    move = abs(cur - closes[0])
    move_component = min(100.0, move / atr_proxy * 12.0)
    strength = int(round(min(100.0, 0.6 * adx_component + 0.4 * move_component)))
    if direction == "range":
        strength = min(strength, 35)  # 區間天花板，避免誤標強趨勢

    return {
        "direction": direction,
        "strength": strength,
        "change_pct": round(change_pct, 2),
        "swing_high": round(swing_high, 8),
        "swing_low": round(swing_low, 8),
        "price_position": round(price_position, 3),
        "last_swing": last_swing,
        "adx": round(adx, 1) if adx is not None else None,
    }


# ===========================================================================
# 階段推斷（決策表）
# ===========================================================================

def infer_stage(dominant_trend: str, ltf_direction: str,
                price_position: float | None, agreement_depth: int) -> dict:
    """純查表決策：(主導趨勢, 小層方向, 價格位置, 嵌套深度) → 7 階段之一。

    回 {stage_code, stage_label, rationale}。

    決策邏輯（可窮舉測）：
        主導 up：
            小層也 up + 深度足  → UP_TREND（上升擴張）
            小層 down/range     → 高位(pos≥0.7) 看 TOP_WATCH（派發見頂）
                                  否則 UP_PULLBACK（多頭回撤）
        主導 down：
            小層也 down + 深度足 → DOWN_TREND（下降擴張）
            小層 up/range        → 低位(pos≤0.3) 看 BOTTOM_WATCH（吸籌築底）
                                  否則 DOWN_BOUNCE（空頭反彈）
        主導 range / 其他        → RANGE（盤整）
    """
    pos = 0.5 if price_position is None else price_position
    dt = dominant_trend
    lt = ltf_direction

    if dt == "up":
        if lt == "up" and agreement_depth >= 2:
            code = "UP_TREND"
            rationale = f"大小層同向上、嵌套深度{agreement_depth} → 主升段"
        elif lt in ("down", "range"):
            if pos >= 0.7:
                code = "TOP_WATCH"
                rationale = f"大層上但小層轉弱且價處高位(pos={pos:.2f}) → 見頂待確認"
            else:
                code = "UP_PULLBACK"
                rationale = f"大層上、小層回落(pos={pos:.2f}) → 高位回調"
        else:  # lt == 'up' 但深度不足
            code = "UP_PULLBACK"
            rationale = f"大層上但小層同向深度不足(depth={agreement_depth}) → 視為回調整理"
    elif dt == "down":
        if lt == "down" and agreement_depth >= 2:
            code = "DOWN_TREND"
            rationale = f"大小層同向下、嵌套深度{agreement_depth} → 主跌段"
        elif lt in ("up", "range"):
            if pos <= 0.3:
                code = "BOTTOM_WATCH"
                rationale = f"大層下但小層轉強且價處低位(pos={pos:.2f}) → 觸底待確認"
            else:
                code = "DOWN_BOUNCE"
                rationale = f"大層下、小層反彈(pos={pos:.2f}) → 低位反彈"
        else:  # lt == 'down' 但深度不足
            code = "DOWN_BOUNCE"
            rationale = f"大層下但小層同向深度不足(depth={agreement_depth}) → 視為反彈整理"
    else:
        code = "RANGE"
        rationale = "主導趨勢為區間/不明 → 區間震盪"

    return {
        "stage_code": code,
        "stage_label": STAGE_LABELS[code],
        "rationale": rationale,
    }


# ===========================================================================
# 假突破偵測
# ===========================================================================

def detect_false_break(candles_by_tf: dict, tf: str,
                       key_level: float | None = None,
                       oi_delta_pct: float | None = None) -> dict:
    """假突破偵測（單一時框）。

    判據（累積信心）：
        * 突破 swing 後 1-2 根內收回（最強訊號）
        * 突破未放量（量能不足）
        * 針狀長影（影線吞噬實體 + 反向）
        * 大層方向與突破相反（需 candles_by_tf 內有更大層；此處用同 dict 推估）
        * oi_delta_pct < 0（減倉突破 = 假）

    回 {is_false_break, confidence(0-1), reasons:[...], side:'up'|'down'|None}。
    缺資料 → is_false_break=False、confidence=0、reasons 標 insufficient。
    """
    reasons: list[str] = []
    candles = (candles_by_tf or {}).get(tf)
    if not candles or len(candles) < 6:
        return {"is_false_break": False, "confidence": 0.0,
                "reasons": ["insufficient_candles"], "side": None}

    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]
    closes = [c["close"] for c in candles]
    vols = [c.get("volume", 0.0) for c in candles]

    # 突破參考位：未給 key_level 時，用「突破棒之前」的 swing 高/低當參考。
    # 看最後 1-2 根是否曾刺破近窗 swing 又收回。
    body_window = candles[:-2] if len(candles) >= 8 else candles[:-1]
    ref_high = max(c["high"] for c in body_window)
    ref_low = min(c["low"] for c in body_window)
    up_level = key_level if key_level is not None else ref_high
    dn_level = key_level if key_level is not None else ref_low

    last = candles[-1]
    side = None
    confidence = 0.0

    # --- 向上假突破：近 1-2 根高點刺破 up_level，但收盤收回 level 之下 ---
    pierced_up = any(highs[i] > up_level for i in range(len(highs) - 2, len(highs)))
    closed_back_up = last["close"] < up_level
    # --- 向下假突破：近 1-2 根低點刺破 dn_level，但收盤收回 level 之上 ---
    pierced_dn = any(lows[i] < dn_level for i in range(len(lows) - 2, len(lows)))
    closed_back_dn = last["close"] > dn_level

    if pierced_up and closed_back_up:
        side = "up"
        confidence += 0.4
        reasons.append("突破上沿後 1-2 根收回 level 之下")
    elif pierced_dn and closed_back_dn:
        side = "down"
        confidence += 0.4
        reasons.append("跌破下沿後 1-2 根收回 level 之上")

    if side is not None:
        # 量能：突破棒量 vs 近窗均量
        avg_vol = mean(vols[:-1]) if len(vols) > 1 and any(vols[:-1]) else 0.0
        brk_vol = max(vols[-2:]) if len(vols) >= 2 else vols[-1]
        if avg_vol > 0 and brk_vol < avg_vol:
            confidence += 0.2
            reasons.append("突破未放量（量 < 近窗均量）")

        # 針狀長影：最後一根反向影線 > 實體
        body = abs(last["close"] - last["open"])
        upper_shadow = last["high"] - max(last["close"], last["open"])
        lower_shadow = min(last["close"], last["open"]) - last["low"]
        if side == "up" and upper_shadow > max(body, 1e-9):
            confidence += 0.2
            reasons.append("上影線長於實體（針狀拒絕）")
        elif side == "down" and lower_shadow > max(body, 1e-9):
            confidence += 0.2
            reasons.append("下影線長於實體（針狀拒絕）")

        # 大層方向與突破相反 → 加成（用 TF_ORDER 找比 tf 更大的層）
        htf_dir = _bigger_tf_direction(candles_by_tf, tf)
        if htf_dir == "down" and side == "up":
            confidence += 0.15
            reasons.append("大層為空、向上突破逆勢 → 更可能假突破")
        elif htf_dir == "up" and side == "down":
            confidence += 0.15
            reasons.append("大層為多、向下突破逆勢 → 更可能假突破")

        # OI：減倉突破 = 假突破加權
        if oi_delta_pct is not None and oi_delta_pct < 0:
            confidence += 0.15
            reasons.append(f"突破伴隨減倉(OIΔ={oi_delta_pct:.1f}%) → 無增量資金跟進")

    confidence = round(min(1.0, confidence), 3)
    is_false = confidence >= 0.5 and side is not None
    if side is None:
        reasons.append("近窗無刺破-收回型態")
    return {"is_false_break": is_false, "confidence": confidence,
            "reasons": reasons, "side": side}


def _bigger_tf_direction(candles_by_tf: dict, tf: str) -> str | None:
    """找比 tf 更大的、第一個有足夠資料的層之方向（供假突破大層判據）。"""
    if not candles_by_tf or tf not in TF_ORDER:
        return None
    idx = TF_ORDER.index(tf)
    for bigger in TF_ORDER[:idx]:  # 比 tf 大的層（在 TF_ORDER 中靠前）
        cs = candles_by_tf.get(bigger)
        if not cs:
            continue
        r = classify_tf_trend(cs)
        if r["direction"] in ("up", "down"):
            return r["direction"]
    return None


# ===========================================================================
# 左/右側交易偏置
# ===========================================================================

def classify_trade_side(nesting: dict) -> dict:
    """左側 / 右側交易偏置（純顯示，不下單、不否決）。

    右側（順勢）：高 alignment + 小層順著主導趨勢。
    左側（逆勢抄底/摸頂）：大層趨勢 + 小層反向 + 價格在極端位置。
    其餘 → neutral。

    回 {side:'left'|'right'|'neutral', rationale}。
    """
    if not nesting or not nesting.get("layers"):
        return {"side": "neutral", "rationale": "無足夠分層資料"}

    dt = nesting.get("dominant_trend", "unknown")
    align = nesting.get("alignment_score", 0.0)
    layers = nesting["layers"]
    ltf = layers[-1]  # 最小層
    ltf_dir = ltf.get("direction", "unknown")
    pos = ltf.get("price_position")
    pos = 0.5 if pos is None else pos

    # 右側：主導明確 + 高度對齊 + 小層順勢
    if dt in ("up", "down") and align >= 0.6 and ltf_dir == dt:
        return {"side": "right",
                "rationale": f"主導{dt}、對齊度{align:.2f}、小層順勢 → 右側順勢"}

    # 左側：大層趨勢明確、小層反向、價格在極端位置
    if dt == "up" and ltf_dir in ("down", "range") and pos <= 0.35:
        return {"side": "left",
                "rationale": f"大層多、小層回落至低位(pos={pos:.2f}) → 左側順大勢低接"}
    if dt == "down" and ltf_dir in ("up", "range") and pos >= 0.65:
        return {"side": "left",
                "rationale": f"大層空、小層反彈至高位(pos={pos:.2f}) → 左側順大勢高空"}

    return {"side": "neutral",
            "rationale": f"主導{dt}、對齊度{align:.2f}、小層{ltf_dir} → 偏置不明確"}


# ===========================================================================
# 嵌套組裝（剝洋蔥）
# ===========================================================================

def build_nesting(candles_by_tf: dict, tf_order: list[str] | None = None) -> dict:
    """剝洋蔥：把各層趨勢由大到小排，量化嵌套關係。

    回 dict:
        layers:          [{tf, direction, strength, price_position, change_pct,
                           last_swing, adx, swing_high, swing_low}, ...] 大→小
        dominant_trend:  'up'|'down'|'range'|'unknown'（最大有效層的方向）
        agreement_depth: 從頂（最大層）連續同向到第幾層（含頂；頂不明則 0）
        divergence_tf:   第一個翻向（與頂不同向）的層 tf，或 None
        alignment_score: 0-1，各層方向與主導一致的加權比例（大層權重高）
        stage / stage_code / stage_label / stage_rationale
        false_break:     最小層的 detect_false_break 結果
        trade_side:      classify_trade_side 結果
        layer_count

    缺層 / 該層 error / 資料不足 → 跳過該層，不崩。空 dict → 安全 unknown。
    """
    order = tf_order or TF_ORDER
    layers: list[dict] = []

    for tf in order:
        entry = (candles_by_tf or {}).get(tf)
        if not entry:
            continue
        # 容忍兩種輸入：直接 candles list，或 {'candles': [...], 'error':...}
        if isinstance(entry, dict):
            if entry.get("error"):
                continue
            candles = entry.get("candles")
        elif isinstance(entry, list):
            candles = entry
        else:
            continue
        if not candles:
            continue

        tr = classify_tf_trend(candles)
        if tr["direction"] == "unknown":
            continue  # 資料不足的層跳過，不污染嵌套
        layers.append({
            "tf": tf,
            "direction": tr["direction"],
            "strength": tr["strength"],
            "price_position": tr["price_position"],
            "change_pct": tr["change_pct"],
            "last_swing": tr["last_swing"],
            "adx": tr["adx"],
            "swing_high": tr["swing_high"],
            "swing_low": tr["swing_low"],
        })

    if not layers:
        empty = {
            "layers": [], "layer_count": 0,
            "dominant_trend": "unknown", "agreement_depth": 0,
            "divergence_tf": None, "alignment_score": 0.0,
            "stage": STAGE_LABELS["RANGE"], "stage_code": "RANGE",
            "stage_label": STAGE_LABELS["RANGE"],
            "stage_rationale": "無有效分層 → 預設區間",
            "false_break": {"is_false_break": False, "confidence": 0.0,
                            "reasons": ["no_layers"], "side": None},
            "trade_side": {"side": "neutral", "rationale": "無分層資料"},
        }
        return empty

    dominant_trend = layers[0]["direction"]

    # agreement_depth：從頂連續同向的層數
    agreement_depth = 0
    divergence_tf = None
    for ly in layers:
        if ly["direction"] == dominant_trend:
            agreement_depth += 1
        else:
            divergence_tf = ly["tf"]
            break

    # alignment_score：大層權重高（線性遞減權重），方向==主導者計入
    n = len(layers)
    weights = [n - i for i in range(n)]  # 頂層權重最大
    total_w = sum(weights)
    aligned_w = sum(w for ly, w in zip(layers, weights) if ly["direction"] == dominant_trend)
    alignment_score = round(aligned_w / total_w, 3) if total_w else 0.0

    # stage：用主導趨勢 + 最小層方向 + 最小層位置 + 嵌套深度
    ltf = layers[-1]
    stage = infer_stage(dominant_trend, ltf["direction"],
                        ltf["price_position"], agreement_depth)

    # false_break：對最小層做（最易出現假突破的層）
    fb = detect_false_break(candles_by_tf, ltf["tf"])

    result = {
        "layers": layers,
        "layer_count": n,
        "dominant_trend": dominant_trend,
        "agreement_depth": agreement_depth,
        "divergence_tf": divergence_tf,
        "alignment_score": alignment_score,
        "stage": stage["stage_label"],
        "stage_code": stage["stage_code"],
        "stage_label": stage["stage_label"],
        "stage_rationale": stage["rationale"],
        "false_break": fb,
    }
    result["trade_side"] = classify_trade_side(result)
    return result
