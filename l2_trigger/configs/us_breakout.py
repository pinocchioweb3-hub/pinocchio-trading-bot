"""Setup US: OKX 美股永續突破引擎設定（v17，實驗性）。

閾值全為先驗拍板（無回測證據），靠紙上帳前瞻驗證後再調。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class USBreakoutConfig:
    setup_name: str = "us_breakout"
    lookback_bars: int = 24            # 24 根 1h = 24h 高低窗
    vol_mult_rth: float = 2.0          # RTH 量能門檻（倍均量）
    vol_mult_ext: float = 3.0          # 延長時段量能門檻（流動性差，更嚴）
    funding_neg_thr: float = -0.0003   # ≤ → BULL（空方擁擠；股票永續基線 ~+0.0001）
    funding_hot_thr: float = 0.0015    # ≥ → BEAR（多頭過熱）
    taker_bull_thr: float = 1.6        # 近4h buy/sell
    taker_bear_thr: float = 0.625      # = 1/1.6
    qqq_block_long_below: float = -1.5  # QQQ 24h% < 此值 → 禁多
    qqq_block_short_above: float = 1.5  # QQQ 24h% > 此值 → 禁空
    min_confirmations: int = 2         # 突破票 + ≥1 確認票，且對向 = 0
    sl_atr_mult: float = 1.5
    sl_min_pct: float = 1.0
    sl_max_pct: float = 3.5
    tp_r_multiples: tuple[float, ...] = (1.0, 1.5, 2.0)
    hold_max_hours: int = 24
    max_us_open: int = 2               # 美股自有上限（紙上倉）
    cooldown_seconds: int = 14400      # 4h / (sym, dir, setup)


US_BREAKOUT_DEFAULT = USBreakoutConfig()
