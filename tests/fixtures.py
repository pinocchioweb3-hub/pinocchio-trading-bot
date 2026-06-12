"""可重用 mock MarketSnapshot 工廠。

每個函式回傳一個 frozen MarketSnapshot，代表一種情境：
- *_fire_*    應產生 FIRE
- *_hold_*    應產生 HOLD
- *_stale_*   含缺料欄位
"""
from __future__ import annotations

from l2_trigger.types import MarketSnapshot


# ---------------------------------------------------------------------------
# Setup A (intraday) — 應 FIRE BULL（SUI 軋空情境）
# ---------------------------------------------------------------------------
def sui_intraday_fire_bull() -> MarketSnapshot:
    return MarketSnapshot(
        symbol="SUI",
        ts=1717740000000,
        price=3.45,
        tf="1h",
        # 即時點
        oi=120_000_000.0,
        oi_delta_pct=6.0,                       # >= SUI threshold 4.0
        funding=-0.0001,                        # <= SUI funding_neg_thr -0.00005
        funding_predicted=-0.00012,
        cvd=4_500_000.0,
        cvd_slope=0.22,                         # >= SUI cvd_slope_min 0.12
        cvd_price_divergence="bull",
        ls_ratio=0.88,                          # <= SUI retail_short 0.92
        top_trader_ratio=1.18,                  # >= SUI top_trader_long 1.12
        liq_long=850_000.0,
        liq_short=2_300_000.0,
        # BTC 閘
        btc_gate_open=True,
        btc_regime="trend_up",
        # 趨勢
        above_4h_200ma=True,
        breakout_1h_high=True,
        # Hot
        is_hot=True,
        strength_score=78.0,
        # 7d 欄位（Setup A 不必要，但帶上供 leverage 用）
        atr_pct_7d=4.5,
        vol_24h_vs_30d=1.4,
        sources_used=("mock",),
    )


# ---------------------------------------------------------------------------
# Setup A — 應 FIRE BEAR（用同樣 SUI，反向訊號；above_4h_200ma 仍 True，
#   價在 MA 上但聰明錢做空 → 頂部訊號，fade-the-top）
# ---------------------------------------------------------------------------
def sui_intraday_fire_bear() -> MarketSnapshot:
    return MarketSnapshot(
        symbol="SUI", ts=1717740000000, price=3.95, tf="1h",
        oi=125_000_000.0,
        oi_delta_pct=7.5,                       # OI 上升也可能是空頭蓄勢
        funding=0.0012,                         # >= SUI funding_hot_thr 0.0008
        funding_predicted=0.0014,
        cvd=-3_100_000.0,
        cvd_slope=-0.25,                        # <= -SUI cvd_slope_min
        cvd_price_divergence="bear",
        ls_ratio=1.15,                          # >= SUI retail_long 1.09
        top_trader_ratio=0.83,                  # <= SUI top_trader_short 0.89
        btc_gate_open=True,
        btc_regime="range",
        above_4h_200ma=True,
        is_hot=True,
        strength_score=72.0,
        atr_pct_7d=4.5,
        sources_used=("mock",),
    )


# ---------------------------------------------------------------------------
# Setup A — 應 HOLD（BTC 閘關）
# ---------------------------------------------------------------------------
def hold_btc_gate_closed() -> MarketSnapshot:
    s = sui_intraday_fire_bull()
    return _replace(s, btc_gate_open=False, btc_regime="trend_down")


# ---------------------------------------------------------------------------
# Setup A — 應 HOLD（不在 Hot 名單）
# ---------------------------------------------------------------------------
def hold_not_hot() -> MarketSnapshot:
    s = sui_intraday_fire_bull()
    return _replace(s, is_hot=False, strength_score=45.0)


# ---------------------------------------------------------------------------
# Setup A — 應 HOLD（OI 蓄勢不足）
# ---------------------------------------------------------------------------
def hold_no_oi_fuel() -> MarketSnapshot:
    s = sui_intraday_fire_bull()
    return _replace(s, oi_delta_pct=1.5)  # < 4.0


# ---------------------------------------------------------------------------
# Setup A — 應 HOLD（票數不夠：bull 1 + bear 1 + neutral 1）
# ---------------------------------------------------------------------------
def hold_mixed_votes() -> MarketSnapshot:
    s = sui_intraday_fire_bull()
    return _replace(
        s,
        cvd_price_divergence="bull", cvd_slope=0.22,   # bull
        funding=0.0012,                                 # bear (>= hot_thr)
        top_trader_ratio=1.00, ls_ratio=1.00,           # neutral
    )


# ---------------------------------------------------------------------------
# Setup A — 應 FIRE BULL（缺 funding，但 cvd+large_holder 仍 BULL ≥ 2）
# ---------------------------------------------------------------------------
def fire_bull_stale_funding() -> MarketSnapshot:
    s = sui_intraday_fire_bull()
    return _replace(s, funding=None, funding_predicted=None,
                    stale_fields=("funding", "funding_predicted"))


# ---------------------------------------------------------------------------
# Setup A — 應 HOLD（BTC 閘缺料）
# ---------------------------------------------------------------------------
def hold_stale_btc_gate() -> MarketSnapshot:
    s = sui_intraday_fire_bull()
    return _replace(s, btc_gate_open=None, stale_fields=("btc_gate_open",))


# ---------------------------------------------------------------------------
# Setup B (ambush) — 應 FIRE BULL（ARB 打底情境）
# ---------------------------------------------------------------------------
def arb_ambush_fire_bull() -> MarketSnapshot:
    return MarketSnapshot(
        symbol="ARB", ts=1717740000000, price=0.812, tf="4h",
        # Setup A 欄位（不用，但避免 None 影響其他訊號）
        oi=42_000_000.0,
        funding=0.00002,                        # 中性
        cvd=180_000.0,
        cvd_slope=0.03,
        cvd_price_divergence="none",
        ls_ratio=1.02,
        top_trader_ratio=1.07,
        # Setup B 用 7d 結構
        atr_pct_7d=2.8,                         # <= 4.0 coiling
        vol_24h_vs_30d=0.62,                    # <= 0.70 drying
        cvd_slope_7d=0.18,                      # >= 0.05 silent accumulation
        top_trader_slope_7d=0.012,              # >= 0.005 creeping
        oi_delta_7d_pct=3.1,                    # in [-2, 5] steady
        higher_lows_7d=True,
        is_hot=False,                           # Setup B 不要求
        strength_score=64.0,
        sources_used=("mock",),
    )


# ---------------------------------------------------------------------------
# Setup B — 應 HOLD（沒打底結構：higher_lows=False）
# ---------------------------------------------------------------------------
def ambush_hold_no_pattern() -> MarketSnapshot:
    s = arb_ambush_fire_bull()
    return _replace(s, higher_lows_7d=False)


# ---------------------------------------------------------------------------
# Setup B — 應 HOLD（波動還太大 atr_pct_7d > 4）
# ---------------------------------------------------------------------------
def ambush_hold_high_volatility() -> MarketSnapshot:
    s = arb_ambush_fire_bull()
    return _replace(s, atr_pct_7d=6.5)


# ---------------------------------------------------------------------------
# 便利工具：frozen dataclass 不能改，用 replace 模式
# ---------------------------------------------------------------------------
def _replace(s: MarketSnapshot, **changes) -> MarketSnapshot:
    from dataclasses import replace
    return replace(s, **changes)
