"""regime_vector.py — 進場當下行情狀態向量組裝（復盤引擎 step4 / 影子層 v56）。

為什麼存在（回應使用者「per-symbol × per-regime 自適應」哲學）：
    plan_snapshot 的 regime_at_entry（4 維）與 context_at_entry（10 維）原本全是 None
    佔位。本模組把『進場那一刻引擎手上已經算好的觀測值』純資料地打包進這兩個向量，
    讓每筆前向捕捉的單都帶著當時的行情狀態（熊市反彈／假突破／牛市底單／主流幣穩定…），
    供 step7 自適應優化器做類比學習——學出「哪種數據／形態／流派適用哪種當下行情」。

設計鐵則（與 plan_snapshot 同源、與三紅線並存）：
    • 純資料分類，零策略數學：**不 import strength、不呼叫 evaluate / eval_cvd_divergence、
      不發任何新的網路請求**。只把『已經在手上的值』映射成穩定的狀態標籤；廣度／均資費
      走 market_scanner 本地 DB 讀（cheap、已算好），不重算。
    • 全函式 exception-safe：任何環節出錯一律安全降級（該欄 None），絕不拖垮出單/記帳。
      本模組是觀測層，壞掉只會少記一格 context，不可影響任何 FIRE/下單決策。
    • 鍵恆在、值可空：抓不到的維度留 None，本身就是訊號（missing_context_keys 會記）。
    • 純觀測單向：輸出只進 plan_snapshot 影子欄，永不回饋任何訊號/下單路徑。

閾值說明：以下分類邊界沿用 L2 TriggerConfig 的同名門檻（funding_neg/hot、oi_rise_min、
    cvd_slope_min），但這裡只當『描述性分桶』用，與真正的投票/出單判斷物理隔離——
    就算邊界日後漂移也不影響任何下單數學，只影響復盤標籤的粒度。
"""
from __future__ import annotations

from .plan_snapshot import _CONTEXT_KEYS, _REGIME_KEYS

# 描述性分桶邊界（沿用 L2 同名門檻；純標籤，不參與任何下單判斷）
_FUNDING_HOT = 0.0008      # 多方付高費 → 過熱/多殺多風險
_FUNDING_NEG = -0.0001     # 空方付費 → 偏多有利
_CVD_SLOPE_MIN = 0.15      # CVD 斜率「明顯」邊界
_OI_RISE_MIN = 3.0         # OI 24h 上升算「蓄勢」
_OI_FALL_MAX = -2.0        # OI 24h 下降算「退潮」
_PRICE_TREND_DEADBAND_PCT = 1.0   # 24h 價格變化 |x|<此值＝方向不明（死區留 None，不硬湊；紅線③）


def _get(obj, key):
    """讀 snapshot 欄位，相容 dict 與 dataclass；缺欄回 None。"""
    if obj is None:
        return None
    try:
        v = obj.get(key) if isinstance(obj, dict) else getattr(obj, key, None)
    except Exception:
        return None
    return v


def classify_funding_state(funding):
    """資金費率狀態。None→None；過熱→hot；空方付費→negative；其餘→neutral。"""
    if funding is None:
        return None
    try:
        f = float(funding)
    except Exception:
        return None
    if f >= _FUNDING_HOT:
        return "hot"
    if f <= _FUNDING_NEG:
        return "negative"
    return "neutral"


def classify_cvd_state(cvd_slope, cvd_divergence=None):
    """CVD 狀態。背離優先（較強訊號）→ bull_divergence/bear_divergence；
    否則依斜率 rising/falling/flat；皆無回 None。"""
    if cvd_divergence in ("bull", "bear"):
        return f"{cvd_divergence}_divergence"
    if cvd_slope is None:
        return None
    try:
        s = float(cvd_slope)
    except Exception:
        return None
    if s >= _CVD_SLOPE_MIN:
        return "rising"
    if s <= -_CVD_SLOPE_MIN:
        return "falling"
    return "flat"


def classify_oi_price_quadrant(oi_delta_pct, price_trend):
    """OI×價格象限（標準四象限的描述標籤）：
        price_up_oi_up   = 新多進場（健康趨勢）
        price_up_oi_down = 軋空回補（較弱）
        price_down_oi_up = 新空進場
        price_down_oi_down = 多單清算
    缺 OI 或價格方向不明 → None（不硬湊）。"""
    if oi_delta_pct is None or price_trend not in ("up", "down"):
        return None
    try:
        oi = float(oi_delta_pct)
    except Exception:
        return None
    if oi >= _OI_RISE_MIN:
        oi_dir = "up"
    elif oi <= _OI_FALL_MAX:
        oi_dir = "down"
    else:
        oi_dir = "flat"
    return f"price_{price_trend}_oi_{oi_dir}"


def _btc_above_from_regime(btc_regime):
    """由 BTC 4h regime 推『是否站上 4h 200MA』的保守代理。
    trend_up→True、trend_down→False、range/None→None（曖昧不造假）。"""
    if btc_regime == "trend_up":
        return True
    if btc_regime == "trend_down":
        return False
    return None


def _htf_aligned(above_4h_200ma, direction):
    """高時框是否與交易方向同向：站上 4h 200MA ⇔ 做多算同向。缺料回 None。"""
    if above_4h_200ma is None or direction is None:
        return None
    is_long = direction in ("bull", "long")
    return bool(above_4h_200ma) == is_long


def _price_trend(snap):
    """由已算好的欄位推短線價格方向（只認確定方向；不確定回 None）。

    來源優先序（皆為『已算好的觀測值』，本函式不重算任何趨勢）：
      1. breakout_1h_high＝True（dispatcher 直發路徑的 1h 突破旗標）→ up
      2. us_breakout_dir（美股突破方向）bull→up / bear→down
      3. regime_trend_dir（per-symbol 4h regime 的 classify_regime.trend_dir：上/下；
         盤整為 None）——v56：deepdive 等無突破旗標的來源用此後備，治本『象限恆 None』。
         注意：這是『市場已觀測到的趨勢方向』，非我方下單方向；逆勢單也照市場實況分桶。
      4. price_chg_24h_pct（24h 價格變化%）——最低優先後備。治本實測缺口：deepdive 在
         4h 盤整(ADX<20→trend_dir=None)時前三源全空，但『4h 無趨勢 ≠ 24h 沒動』。此欄
         與 oi_delta_pct 同為 24h 窗的已觀測值，是 OI×價格象限最自然的價格配對。死區
         ±_PRICE_TREND_DEADBAND_PCT%：微小波動仍回 None（不硬湊方向；紅線③）。
    """
    if _get(snap, "breakout_1h_high") is True:
        return "up"
    us_dir = _get(snap, "us_breakout_dir")
    if us_dir == "bull":
        return "up"
    if us_dir == "bear":
        return "down"
    td = _get(snap, "regime_trend_dir")
    if td in ("上", "up", "bull"):
        return "up"
    if td in ("下", "down", "bear"):
        return "down"
    chg = _get(snap, "price_chg_24h_pct")   # 後備：24h 已觀測價格方向（同 OI 窗）
    if chg is not None:
        try:
            c = float(chg)
            if c >= _PRICE_TREND_DEADBAND_PCT:
                return "up"
            if c <= -_PRICE_TREND_DEADBAND_PCT:
                return "down"
        except (TypeError, ValueError):
            pass
    return None


def _market_context():
    """市場層上下文（上漲廣度 % ／ 全市場均資金費）— 純本地 DB 讀，已算好不重算。
    失敗（無數據/DB 異常）回兩個 None。"""
    out = {"breadth_up_pct": None, "avg_funding": None}
    try:
        from .market_scanner import get_latest_breadth
        b = get_latest_breadth()
        if b:
            up = b.get("n_up24h") or 0
            dn = b.get("n_down24h") or 0
            tot = up + dn
            if tot > 0:
                out["breadth_up_pct"] = round(up / tot * 100, 1)
            if b.get("avg_funding") is not None:
                out["avg_funding"] = b.get("avg_funding")
    except Exception:
        pass
    return out


def assemble(snap, direction=None, *, include_market: bool = True,
             extra_context=None):
    """組裝 (regime_vector, context) 兩個影子向量。純資料、全程 exception-safe。

    參數：
      snap            MarketSnapshot（dataclass）或其 dict 序列化；可為 None（只取市場層）。
      direction       交易方向 bull/bear（給 htf_aligned 用）。
      include_market  是否補市場層 context（廣度/均資費，本地 DB 讀）；預設 True。
      extra_context   呼叫端已算好的額外 context（如 deepdive 的 macro_confluence_score /
                      wyckoff_phase）；非 None 的鍵覆蓋上去（只認 schema 內鍵）。

    回傳：(regime_vector: dict, context: dict)，鍵恆在、抓不到的值為 None。
    """
    regime_vector = {k: None for k in _REGIME_KEYS}
    context = {k: None for k in _CONTEXT_KEYS}
    try:
        # --- regime 向量（per-symbol 行情狀態）---
        regime_vector["funding_state"] = classify_funding_state(_get(snap, "funding"))
        regime_vector["cvd_state"] = classify_cvd_state(
            _get(snap, "cvd_slope"), _get(snap, "cvd_price_divergence"))
        regime_vector["oi_price_quadrant"] = classify_oi_price_quadrant(
            _get(snap, "oi_delta_pct"), _price_trend(snap))
        # vol_trend 交由 plan_snapshot 以 regime 字串帶入，此處不覆蓋。

        # --- context 向量（per-symbol 已觀測值）---
        context["oi_delta_pct"] = _get(snap, "oi_delta_pct")
        context["cvd_slope"] = _get(snap, "cvd_slope")
        context["top_trader_ratio"] = _get(snap, "top_trader_ratio")
        context["btc_above_200ma_4h"] = _btc_above_from_regime(_get(snap, "btc_regime"))
        context["htf_aligned"] = _htf_aligned(_get(snap, "above_4h_200ma"), direction)

        # --- 市場層 context（廣度/均資費，cheap 本地讀）---
        if include_market:
            mc = _market_context()
            context["breadth_up_pct"] = mc["breadth_up_pct"]
            context["avg_funding"] = mc["avg_funding"]

        # --- 呼叫端額外算好的 context（覆蓋）---
        if isinstance(extra_context, dict):
            for k in _CONTEXT_KEYS:
                if k in extra_context and extra_context[k] is not None:
                    context[k] = extra_context[k]
    except Exception:
        pass
    return regime_vector, context
