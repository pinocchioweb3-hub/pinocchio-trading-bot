"""Setup C: btc_ma200 -- BTC 4h 200MA 穿越策略

特徵：
- 只交易 BTC（最大流動性、最低滑價）
- 方向型訊號：ma200_crossover（金叉/死叉）+ ma200_trend（趨勢確認）
- 不要求 Hot/OI 蓄勢/BTC 閘（本身就是 BTC）
- ≥1 同向確認即 FIRE（crossover 本身就是強訊號）
- 適中 stop（2%）、TP 1R/2R/3R、≤72h 持倉（趨勢型持久一些）
"""
from __future__ import annotations

from ..types import TriggerConfig


MA_CROSSOVER_BTC = TriggerConfig(
    setup_name="ma_crossover",

    # --- MA 策略不用 Setup A/B 的方向型閾值，但要填（frozen dataclass）---
    cvd_slope_min=0.15,
    cvd_slope_ref=0.50,
    funding_neg_thr=-0.0001,
    funding_hot_thr=0.0008,
    top_trader_long_thr=1.15,
    top_trader_short_thr=0.87,
    retail_short_thr=0.90,
    retail_long_thr=1.11,
    oi_rise_min_pct=3.0,

    # --- 不啟用 gate/hot/trend/fuel（MA 策略自帶方向判斷）---
    require_oi_fuel=False,
    require_gate_open=False,
    require_hot=False,
    require_trend_4h=False,

    # --- 投票：crossover + trend 兩個訊號，≥1 即可（crossover 夠強）---
    min_confirmations=1,

    # --- 風控（適中參數）---
    # ⚠️ v23-2 起，「實盤 dispatcher」的 SL/TP/風險/槓桿全部改走 botconfig 單一來源
    #    （dispatcher.py：CONFIG.sl_pct / risk_per_trade_usd / tp_r / choose_leverage）。
    #    下列 risk_per_trade_usd / default_leverage / sl_buffer_pct / tp_r_multiples 僅供
    #    backtest 等獨立工具使用；改這裡【不會】影響線上發單。要調線上風控請改 botconfig。
    risk_per_trade_usd=100.0,
    default_leverage=10,
    tp_r_multiples=(1.0, 2.0, 3.0),
    sl_buffer_pct=2.0,          # BTC 4h 級別 2% 止損（僅 backtest）
    hold_max_hours=72,          # 趨勢型持倉較久
)


def get_ma_crossover_config() -> TriggerConfig:
    """取 MA 穿越策略 config（只交易 BTC）。"""
    return MA_CROSSOVER_BTC
