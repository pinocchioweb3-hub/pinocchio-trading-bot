"""Setup A: precision_intraday — 趨勢 × 熱點重疊的日內爆發點

特徵：
- symbol 必須在 Hot 名單 (require_hot=True)
- 4h 趨勢必須成立 (require_trend_4h=True)
- BTC 閘必開
- OI 必須蓄勢
- 方向型 3 個（CVD/funding/large_holder）—— ≥2 同向且對向=0 才 FIRE
- 緊 stop（3.5-5%）、TP 1R/1.5R/2R、≤24h 持倉
"""
from __future__ import annotations

from ..types import TriggerConfig


# === BTC / ETH / SOL / 其他主流（流動性好）====================================
# v10 升級：閾值從 baseline 換成 loose（真實 30d 回測 8 幣，loose=67% 勝率 +$3,080
# vs baseline=42.9% -$50；strict=20% -$171）
INTRADAY_DEFAULT = TriggerConfig(
    setup_name="intraday",
    cvd_slope_min=0.08,             # 從 0.15 放寬到 0.08
    cvd_slope_ref=0.50,
    funding_neg_thr=-0.00005,       # 從 -0.0001 放寬到 -0.00005
    funding_hot_thr=0.0008,
    top_trader_long_thr=1.15,
    top_trader_short_thr=0.87,
    retail_short_thr=0.90,
    retail_long_thr=1.11,
    oi_rise_min_pct=2.0,            # 從 3.0 放寬到 2.0
    require_oi_fuel=True,
    require_gate_open=True,
    require_hot=True,
    require_trend_4h=True,
    min_confirmations=1,            # 從 2 放寬到 1（投票，1 個方向同向就 FIRE）
    risk_per_trade_usd=100.0,
    default_leverage=15,
    tp_r_multiples=(1.0, 1.5, 2.0),
    sl_buffer_pct=4.0,              # 從 3.5 放寬到 4.0（給更多空間避免假掃止損）
    hold_max_hours=48,              # 從 24 延長到 48（很多 trade 是 timeout 出，給更多時間）
)


# === SUI 軋空（規格 Part D：流動性較薄，門檻略放寬）==========================
INTRADAY_SUI = TriggerConfig(
    setup_name="intraday",
    cvd_slope_min=0.12,
    cvd_slope_ref=0.40,
    funding_neg_thr=-0.00005,
    funding_hot_thr=0.0008,
    top_trader_long_thr=1.12,
    top_trader_short_thr=0.89,
    retail_short_thr=0.92,
    retail_long_thr=1.09,
    oi_rise_min_pct=4.0,
    require_oi_fuel=True,
    require_gate_open=True,
    require_hot=True,
    require_trend_4h=True,
    min_confirmations=2,
    risk_per_trade_usd=100.0,
    default_leverage=15,  # leverage.py 依 ATR 自動調整
    tp_r_multiples=(1.0, 1.5, 2.0),
    sl_buffer_pct=4.0,
    hold_max_hours=24,
)


# === 低流通量（WLFI / Hot 新進幣前 3 天）保守版 ==============================
INTRADAY_LOWCAP = TriggerConfig(
    setup_name="intraday",
    cvd_slope_min=0.20,           # 嚴格，避免雜訊
    cvd_slope_ref=0.50,
    funding_neg_thr=-0.0002,
    funding_hot_thr=0.0010,       # 過熱閾更寬（WLFI funding 本就大）
    top_trader_long_thr=1.20,
    top_trader_short_thr=0.83,
    retail_short_thr=0.85,
    retail_long_thr=1.18,
    oi_rise_min_pct=5.0,
    require_oi_fuel=True,
    require_gate_open=True,
    require_hot=True,
    require_trend_4h=True,
    min_confirmations=2,
    risk_per_trade_usd=100.0,
    default_leverage=5,
    tp_r_multiples=(1.0, 1.5, 2.0),
    sl_buffer_pct=5.0,            # 寬 stop 給波動空間
    hold_max_hours=24,
)


# === 每幣別查表 ==============================================================
INTRADAY_BY_SYMBOL: dict[str, TriggerConfig] = {
    "SUI":  INTRADAY_SUI,
    "WLFI": INTRADAY_LOWCAP,
}


def get_intraday_config(symbol: str, is_hot_new: bool = False) -> TriggerConfig:
    """取 intraday config。
    is_hot_new=True 且 symbol 不在主名單 → 用 LOWCAP（前 3 天保守）
    """
    if symbol in INTRADAY_BY_SYMBOL:
        return INTRADAY_BY_SYMBOL[symbol]
    if is_hot_new:
        return INTRADAY_LOWCAP
    return INTRADAY_DEFAULT
