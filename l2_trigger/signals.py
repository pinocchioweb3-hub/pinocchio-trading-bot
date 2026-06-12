"""訊號評估器：純函式集合。

契約：
    f(snapshot: MarketSnapshot, cfg: TriggerConfig) -> SignalResult

規則：
- 缺料 → STALE 狀態（不抛例外、不計入投票）
- BULL/BEAR/NEUTRAL 純粹由閾值決定，不引入外部狀態
- evidence 內所有值必須 JSON-serializable（dict/list/str/int/float/bool）

訊號分類：
    方向型（投票用）—— cvd_divergence / funding / large_holder
                       / cvd_silent_accumulation / large_holder_creeping
    強度型（不定方向）—— oi_trajectory / oi_steady
    閘 ——              btc_gate（BLOCK 表示閘關）
    過濾 ——            in_hot / trend_4h / atr_coiling / volume_drying / higher_lows
                       （回傳 NEUTRAL = 通過，BEAR = 不通過。引擎用來閘 setup）
"""
from __future__ import annotations

from .types import MarketSnapshot, SignalResult, SignalState, TriggerConfig


# =============================================================================
# 共用小工具
# =============================================================================
def _stale(name: str, missing: list[str]) -> SignalResult:
    """缺料統一回 STALE，evidence 列出缺哪些欄位給 log 用。"""
    return SignalResult(name, SignalState.STALE, 0.0, {"stale": True, "missing": missing})


def _clip(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


# =============================================================================
# === Setup A 主訊號（方向型，會被投票）===
# =============================================================================
def eval_cvd_divergence(s: MarketSnapshot, c: TriggerConfig) -> SignalResult:
    """價走平/跌但 CVD 上揚 = 吸籌 (bull)；反之 = 派發 (bear)。

    snapshot.cvd_price_divergence 由 snapshot 建構期算好（Q4：在 L2 端）。
    這裡只負責「強度夠不夠 → 給 score」。
    """
    if s.is_stale("cvd_slope"):
        return _stale("cvd_divergence", ["cvd_slope"])

    if s.cvd_price_divergence == "bull" and s.cvd_slope >= c.cvd_slope_min:
        score = _clip(s.cvd_slope / c.cvd_slope_ref)
        return SignalResult("cvd_divergence", SignalState.BULL, score,
                            {"divergence": "bull", "cvd_slope": s.cvd_slope})

    if s.cvd_price_divergence == "bear" and s.cvd_slope <= -c.cvd_slope_min:
        score = _clip(-abs(s.cvd_slope) / c.cvd_slope_ref)
        return SignalResult("cvd_divergence", SignalState.BEAR, score,
                            {"divergence": "bear", "cvd_slope": s.cvd_slope})

    return SignalResult("cvd_divergence", SignalState.NEUTRAL, 0.0,
                        {"divergence": s.cvd_price_divergence,
                         "cvd_slope": s.cvd_slope})


def eval_funding(s: MarketSnapshot, c: TriggerConfig) -> SignalResult:
    """資金費率：負/極低 → 空方付錢 → 軋空燃料 (bull)。
    過熱正費率 → 多殺多風險 (bear)。
    """
    if s.is_stale("funding"):
        return _stale("funding", ["funding"])

    if s.funding <= c.funding_neg_thr:
        return SignalResult("funding", SignalState.BULL, 0.6,
                            {"funding": s.funding, "regime": "shorts_pay"})
    if s.funding >= c.funding_hot_thr:
        return SignalResult("funding", SignalState.BEAR, -0.6,
                            {"funding": s.funding, "regime": "overheated"})
    return SignalResult("funding", SignalState.NEUTRAL, 0.0,
                        {"funding": s.funding, "regime": "neutral"})


def eval_large_holder(s: MarketSnapshot, c: TriggerConfig) -> SignalResult:
    """大戶 vs 散戶背離：大戶轉多 + 散戶偏空 = 聰明錢做多 (bull)。

    Q3 修正：閾值獨立（top_trader_long_thr / top_trader_short_thr），
    不再用 1/x 推對稱（原規格 1/1.15≈0.870 偏差小但累積會影響觸發）。
    """
    if s.is_stale("top_trader_ratio", "ls_ratio"):
        return _stale("large_holder",
                      [f for f in ("top_trader_ratio", "ls_ratio") if s.is_stale(f)])

    smart_long = s.top_trader_ratio >= c.top_trader_long_thr
    retail_short = s.ls_ratio <= c.retail_short_thr
    if smart_long and retail_short:
        return SignalResult("large_holder", SignalState.BULL, 0.8,
                            {"top_trader": s.top_trader_ratio, "retail": s.ls_ratio,
                             "view": "smart_money_long_vs_retail_short"})

    smart_short = s.top_trader_ratio <= c.top_trader_short_thr
    retail_long = s.ls_ratio >= c.retail_long_thr
    if smart_short and retail_long:
        return SignalResult("large_holder", SignalState.BEAR, -0.8,
                            {"top_trader": s.top_trader_ratio, "retail": s.ls_ratio,
                             "view": "smart_money_short_vs_retail_long"})

    return SignalResult("large_holder", SignalState.NEUTRAL, 0.0,
                        {"top_trader": s.top_trader_ratio, "retail": s.ls_ratio})


# =============================================================================
# === Setup A 強度型訊號（不定方向，做 fuel 閘）===
# =============================================================================
def eval_oi_trajectory(s: MarketSnapshot, c: TriggerConfig) -> SignalResult:
    """OI 上升 + 價格走平 = 蓄勢（擠壓燃料）。
    本訊號不投票，只回傳 evidence['fuel'] 給引擎判斷 require_oi_fuel。
    """
    if s.is_stale("oi_delta_pct"):
        return _stale("oi_trajectory", ["oi_delta_pct"])

    fuel = s.oi_delta_pct >= c.oi_rise_min_pct
    return SignalResult("oi_trajectory", SignalState.NEUTRAL, 0.0,
                        {"oi_delta_pct": s.oi_delta_pct, "fuel": fuel,
                         "threshold": c.oi_rise_min_pct})


# =============================================================================
# === BTC 閘（特殊：BLOCK 則整包 HOLD）===
# =============================================================================
def eval_btc_gate(s: MarketSnapshot, c: TriggerConfig) -> SignalResult:
    """Q5 修正：閘開規則 = BTC 4h 收 > 4h 200MA AND regime ≠ trend_down。
    本檢查在 snapshot 建構時完成，這裡只讀 btc_gate_open 布林。
    """
    if s.is_stale("btc_gate_open"):
        return _stale("btc_gate", ["btc_gate_open"])

    state = SignalState.NEUTRAL if s.btc_gate_open else SignalState.BLOCK
    return SignalResult("btc_gate", state, 0.0,
                        {"gate_open": s.btc_gate_open, "regime": s.btc_regime})


# =============================================================================
# === Setup A 過濾型訊號（BEAR = 不通過閘）===
# =============================================================================
def eval_in_hot(s: MarketSnapshot, c: TriggerConfig) -> SignalResult:
    """Setup A 要求 symbol 在 Hot 名單。is_hot 由 dispatcher 注入 snapshot。"""
    if s.is_hot:
        return SignalResult("in_hot", SignalState.NEUTRAL, 0.0,
                            {"is_hot": True, "strength_score": s.strength_score})
    return SignalResult("in_hot", SignalState.BEAR, 0.0,
                        {"is_hot": False, "strength_score": s.strength_score,
                         "view": "filter_failed"})


def eval_trend_4h(s: MarketSnapshot, c: TriggerConfig) -> SignalResult:
    """Setup A 要求 4h 收 > 200MA（趨勢成立）。"""
    if s.is_stale("above_4h_200ma"):
        return _stale("trend_4h", ["above_4h_200ma"])

    if s.above_4h_200ma:
        return SignalResult("trend_4h", SignalState.NEUTRAL, 0.0,
                            {"above_4h_200ma": True})
    return SignalResult("trend_4h", SignalState.BEAR, 0.0,
                        {"above_4h_200ma": False, "view": "filter_failed"})


# =============================================================================
# === Setup B 結構訊號 ===
# =============================================================================
def eval_atr_coiling(s: MarketSnapshot, c: TriggerConfig) -> SignalResult:
    """7d ATR/price 小 → 價格 coiling，是埋伏前提。"""
    if s.is_stale("atr_pct_7d"):
        return _stale("atr_coiling", ["atr_pct_7d"])

    if s.atr_pct_7d <= c.atr_coil_max_pct:
        return SignalResult("atr_coiling", SignalState.NEUTRAL, 0.0,
                            {"atr_pct_7d": s.atr_pct_7d, "threshold": c.atr_coil_max_pct})
    return SignalResult("atr_coiling", SignalState.BEAR, 0.0,
                        {"atr_pct_7d": s.atr_pct_7d, "view": "too_volatile_for_ambush"})


def eval_volume_drying(s: MarketSnapshot, c: TriggerConfig) -> SignalResult:
    """24h 量 / 30d 均量 < 0.7 → 量能枯竭（賣壓出盡）。"""
    if s.is_stale("vol_24h_vs_30d"):
        return _stale("volume_drying", ["vol_24h_vs_30d"])

    if s.vol_24h_vs_30d <= c.vol_dry_max_ratio:
        return SignalResult("volume_drying", SignalState.NEUTRAL, 0.0,
                            {"vol_ratio": s.vol_24h_vs_30d})
    return SignalResult("volume_drying", SignalState.BEAR, 0.0,
                        {"vol_ratio": s.vol_24h_vs_30d, "view": "still_distributing"})


def eval_oi_steady(s: MarketSnapshot, c: TriggerConfig) -> SignalResult:
    """OI 7d 變化在 [-2%, +5%] = 籌碼穩定不撤退（Setup B 用，不同於 trajectory）。"""
    if s.is_stale("oi_delta_7d_pct"):
        return _stale("oi_steady", ["oi_delta_7d_pct"])

    in_range = c.oi_steady_min_pct <= s.oi_delta_7d_pct <= c.oi_steady_max_pct
    state = SignalState.NEUTRAL if in_range else SignalState.BEAR
    return SignalResult("oi_steady", state, 0.0,
                        {"oi_delta_7d_pct": s.oi_delta_7d_pct,
                         "range": [c.oi_steady_min_pct, c.oi_steady_max_pct]})


def eval_cvd_silent_accumulation(s: MarketSnapshot, c: TriggerConfig) -> SignalResult:
    """7d CVD 緩升（不需要爆量背離，只要主動買單在累積）。"""
    if s.is_stale("cvd_slope_7d"):
        return _stale("cvd_silent_accumulation", ["cvd_slope_7d"])

    if s.cvd_slope_7d >= c.cvd_slope_7d_min:
        return SignalResult("cvd_silent_accumulation", SignalState.BULL, 0.5,
                            {"cvd_slope_7d": s.cvd_slope_7d})
    if s.cvd_slope_7d <= -c.cvd_slope_7d_min:
        return SignalResult("cvd_silent_accumulation", SignalState.BEAR, -0.5,
                            {"cvd_slope_7d": s.cvd_slope_7d})
    return SignalResult("cvd_silent_accumulation", SignalState.NEUTRAL, 0.0,
                        {"cvd_slope_7d": s.cvd_slope_7d})


def eval_large_holder_creeping(s: MarketSnapshot, c: TriggerConfig) -> SignalResult:
    """大戶多空比 7d 緩升（聰明錢慢慢加倉）。比 Setup A 的 large_holder 寬鬆。"""
    if s.is_stale("top_trader_slope_7d"):
        return _stale("large_holder_creeping", ["top_trader_slope_7d"])

    if s.top_trader_slope_7d >= c.top_trader_slope_7d_min:
        return SignalResult("large_holder_creeping", SignalState.BULL, 0.6,
                            {"top_trader_slope_7d": s.top_trader_slope_7d})
    if s.top_trader_slope_7d <= -c.top_trader_slope_7d_min:
        return SignalResult("large_holder_creeping", SignalState.BEAR, -0.6,
                            {"top_trader_slope_7d": s.top_trader_slope_7d})
    return SignalResult("large_holder_creeping", SignalState.NEUTRAL, 0.0,
                        {"top_trader_slope_7d": s.top_trader_slope_7d})


def eval_higher_lows(s: MarketSnapshot, c: TriggerConfig) -> SignalResult:
    """7d 高低點抬升（打底結構成立）。"""
    if s.is_stale("higher_lows_7d"):
        return _stale("higher_lows", ["higher_lows_7d"])

    if s.higher_lows_7d:
        return SignalResult("higher_lows", SignalState.NEUTRAL, 0.0,
                            {"higher_lows_7d": True})
    return SignalResult("higher_lows", SignalState.BEAR, 0.0,
                        {"higher_lows_7d": False, "view": "no_base_yet"})


# =============================================================================
# === Setup C 主訊號：BTC 4h 200MA 穿越 ===
# =============================================================================
def eval_ma200_crossover(s: MarketSnapshot, c: TriggerConfig) -> SignalResult:
    """4h 收盤穿越 200MA → 方向型訊號。

    需要 snapshot 攜帶 ma200_4h（200 期 SMA）和 prev_close_4h（前一根 4h 收盤）。
    - 前一根 < MA 且 當前 ≥ MA → 金叉 BULL
    - 前一根 > MA 且 當前 ≤ MA → 死叉 BEAR
    - 其他 → NEUTRAL（已在趨勢中，不重複觸發）
    """
    if s.is_stale("ma200_4h", "prev_close_4h"):
        return _stale("ma200_crossover", ["ma200_4h", "prev_close_4h"])

    close = s.price
    ma = s.ma200_4h
    prev = s.prev_close_4h

    # 金叉：前一根收在 MA 下方，這根收在 MA 上方
    if prev < ma and close >= ma:
        score = _clip((close - ma) / ma * 100, 0.0, 1.0)
        return SignalResult("ma200_crossover", SignalState.BULL, max(0.5, score),
                            {"cross": "golden", "close": close, "ma200": round(ma, 2),
                             "prev_close": prev, "distance_pct": round((close - ma) / ma * 100, 3)})

    # 死叉：前一根收在 MA 上方，這根收在 MA 下方
    if prev > ma and close <= ma:
        score = _clip((ma - close) / ma * 100, 0.0, 1.0)
        return SignalResult("ma200_crossover", SignalState.BEAR, -max(0.5, score),
                            {"cross": "death", "close": close, "ma200": round(ma, 2),
                             "prev_close": prev, "distance_pct": round((close - ma) / ma * 100, 3)})

    # 無穿越
    side = "above" if close > ma else "below"
    return SignalResult("ma200_crossover", SignalState.NEUTRAL, 0.0,
                        {"cross": "none", "close": close, "ma200": round(ma, 2),
                         "side": side, "distance_pct": round((close - ma) / ma * 100, 3)})


def eval_ma200_trend(s: MarketSnapshot, c: TriggerConfig) -> SignalResult:
    """4h 收盤在 200MA 上方/下方 → 趨勢確認（輔助訊號）。

    與 crossover 搭配：crossover 決定進場時機，trend 確認方向。
    """
    if s.is_stale("ma200_4h"):
        return _stale("ma200_trend", ["ma200_4h"])

    close = s.price
    ma = s.ma200_4h
    dist_pct = (close - ma) / ma * 100

    if close > ma:
        score = _clip(dist_pct / 5.0, 0.0, 1.0)  # 離 MA 越遠分數越高，5% 封頂
        return SignalResult("ma200_trend", SignalState.BULL, score,
                            {"above_ma200": True, "distance_pct": round(dist_pct, 3)})
    else:
        score = _clip(-abs(dist_pct) / 5.0, -1.0, 0.0)
        return SignalResult("ma200_trend", SignalState.BEAR, score,
                            {"above_ma200": False, "distance_pct": round(dist_pct, 3)})


# =============================================================================
# === Setup 訊號集（給引擎選用）===
# =============================================================================
# 方向型訊號（會被投票）
INTRADAY_DIRECTIONAL = (eval_cvd_divergence, eval_funding, eval_large_holder)

# Setup A 過濾閘（任一 BEAR → HOLD）
INTRADAY_FILTERS = (eval_in_hot, eval_trend_4h)

# Setup B 方向型訊號（緩升版）
AMBUSH_DIRECTIONAL = (eval_cvd_silent_accumulation, eval_large_holder_creeping)

# Setup B 結構過濾（任一 BEAR → HOLD）
AMBUSH_FILTERS = (eval_atr_coiling, eval_volume_drying, eval_oi_steady, eval_higher_lows)

# Setup C 方向型訊號（僅穿越事件投票，trend 做輔助過濾）
MA_CROSSOVER_DIRECTIONAL = (eval_ma200_crossover,)

# Setup C 無過濾閘（MA 策略自帶方向判斷）
MA_CROSSOVER_FILTERS = ()
