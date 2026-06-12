"""Setup B: left_side_ambush — 打底/杯柄成形後 3-7 天左側埋伏

特徵：
- 不要求 symbol 在 Hot 名單（趨勢通常先行於熱點）
- 不要求 4h 趨勢（打底階段價格本就盤整）
- BTC 閘允許關（require_gate_open=False，BTC 盤整時也能埋伏）
- 不要求 OI 蓄勢（require_oi_fuel=False；改用 oi_steady 過濾）
- 結構過濾：atr_coiling + volume_drying + oi_steady + higher_lows 全通過
- 方向型 2 個（cvd_silent_accumulation/large_holder_creeping）—— ≥2 同向才 FIRE
- 寬 stop（4-6%）、TP 1R/1.5R/2.5R、可持 3-7 天
"""
from __future__ import annotations

from ..types import TriggerConfig


# === 預設 ambush（BTC/ETH/SOL/SUI 等主流）====================================
AMBUSH_DEFAULT = TriggerConfig(
    setup_name="ambush",
    # 結構閾值
    atr_coil_max_pct=4.0,
    vol_dry_max_ratio=0.70,
    cvd_slope_7d_min=0.05,
    top_trader_slope_7d_min=0.005,
    oi_steady_min_pct=-2.0,
    oi_steady_max_pct=5.0,
    # 方向（B 的方向型寬鬆，因為已被結構閘把關過）
    min_confirmations=2,           # 兩個方向訊號都要同向（共 2 個）
    # 不要求 fuel/gate/hot/trend
    require_oi_fuel=False,
    require_gate_open=False,
    require_hot=False,
    require_trend_4h=False,
    # 風控（寬 stop、拉遠 TP）
    risk_per_trade_usd=100.0,
    default_leverage=15,
    tp_r_multiples=(1.0, 1.5, 2.5),
    sl_buffer_pct=5.0,             # 比 intraday 寬
    hold_max_hours=24 * 7,         # 最多 7 天
)


# === 低流通量（WLFI / Hot 新進幣）即便埋伏也要保守 ============================
AMBUSH_LOWCAP = TriggerConfig(
    setup_name="ambush",
    atr_coil_max_pct=6.0,           # 容許 ATR 略大（低流通量本就波動）
    vol_dry_max_ratio=0.60,         # 量能要更明顯枯竭
    cvd_slope_7d_min=0.08,          # 主動買壓要更明顯
    top_trader_slope_7d_min=0.008,
    oi_steady_min_pct=-3.0,
    oi_steady_max_pct=6.0,
    min_confirmations=2,
    require_oi_fuel=False,
    require_gate_open=False,
    require_hot=False,
    require_trend_4h=False,
    risk_per_trade_usd=100.0,
    default_leverage=5,
    tp_r_multiples=(1.0, 1.5, 2.5),
    sl_buffer_pct=6.0,              # 更寬
    hold_max_hours=24 * 5,          # 縮成 5 天（低流通量風險高）
)


AMBUSH_BY_SYMBOL: dict[str, TriggerConfig] = {
    "WLFI": AMBUSH_LOWCAP,
}


def get_ambush_config(symbol: str, is_hot_new: bool = False) -> TriggerConfig:
    """取 ambush config。is_hot_new 或低流通量 → LOWCAP 版"""
    if symbol in AMBUSH_BY_SYMBOL:
        return AMBUSH_BY_SYMBOL[symbol]
    if is_hot_new:
        return AMBUSH_LOWCAP
    return AMBUSH_DEFAULT
