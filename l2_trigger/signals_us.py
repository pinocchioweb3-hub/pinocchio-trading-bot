"""美股永續突破訊號（v17，純函式，契約同 signals.py：缺料 → STALE）。

設計重點：
- 主訊號 = 1h 收盤突破 24h 高/低（由 snapshot 建構期算好 us_breakout_dir）
- 確認票（量能/funding/taker）只在突破存在時投票
- 量能不足永不投反對票（不確認 ≠ 反向證據）
- funding 極值在「對向=0」政策下自動否決擁擠方向的突破
"""
from __future__ import annotations

from .types import MarketSnapshot, SignalResult, SignalState


def _clip(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def eval_us_breakout(s: MarketSnapshot, c) -> SignalResult:
    """主訊號：1h 收盤突破 24h 高/低。score 與突破距離（以 ATR 計）成正比。"""
    if s.is_stale("us_break_level", "atr_1h_pct"):
        return SignalResult("us_breakout", SignalState.STALE, 0.0, {})
    if s.us_breakout_dir == "none":
        return SignalResult("us_breakout", SignalState.NEUTRAL, 0.0, {"dir": "none"})

    dist_atr = abs(s.price - s.us_break_level) / s.price * 100 / max(s.atr_1h_pct, 0.01)
    score = max(0.5, _clip(dist_atr, 0.0, 1.0))
    ev = {"dir": s.us_breakout_dir, "break_level": s.us_break_level,
          "distance_atr": round(dist_atr, 2)}
    if s.us_breakout_dir == "bull":
        return SignalResult("us_breakout", SignalState.BULL, score, ev)
    return SignalResult("us_breakout", SignalState.BEAR, -score, ev)


def eval_us_volume_surge(s: MarketSnapshot, c) -> SignalResult:
    """確認票：突破 K 量能 ≥ 門檻 → 投突破同向票。量不足 = NEUTRAL（永不反對）。"""
    if s.is_stale("us_vol_mult"):
        return SignalResult("us_volume_surge", SignalState.STALE, 0.0, {})
    if s.us_breakout_dir == "none":
        return SignalResult("us_volume_surge", SignalState.NEUTRAL, 0.0, {})

    thr = c.vol_mult_ext if s.us_session == "ext" else c.vol_mult_rth
    ev = {"vol_mult": round(s.us_vol_mult, 2), "thr": thr, "session": s.us_session}
    if s.us_vol_mult >= thr:
        score = _clip(s.us_vol_mult / 4.0, 0.0, 1.0)
        if s.us_breakout_dir == "bull":
            return SignalResult("us_volume_surge", SignalState.BULL, score, ev)
        return SignalResult("us_volume_surge", SignalState.BEAR, -score, ev)
    return SignalResult("us_volume_surge", SignalState.NEUTRAL, 0.0, ev)


def eval_us_funding_extreme(s: MarketSnapshot, c) -> SignalResult:
    """確認票：funding 極值。負極值=空方擁擠(BULL)；過熱=BEAR。
    在「對向=0」政策下，負 funding 自動否決向下突破（不追擁擠空頭）。"""
    if s.is_stale("funding"):
        return SignalResult("us_funding", SignalState.STALE, 0.0, {})
    if s.us_breakout_dir == "none":
        return SignalResult("us_funding", SignalState.NEUTRAL, 0.0, {})

    ev = {"funding": s.funding}
    if s.funding <= c.funding_neg_thr:
        return SignalResult("us_funding", SignalState.BULL, 0.6, ev)
    if s.funding >= c.funding_hot_thr:
        return SignalResult("us_funding", SignalState.BEAR, -0.6, ev)
    return SignalResult("us_funding", SignalState.NEUTRAL, 0.0, ev)


def eval_us_taker_skew(s: MarketSnapshot, c) -> SignalResult:
    """確認票：近 4h taker 買賣偏向。"""
    if s.is_stale("us_taker_ratio"):
        return SignalResult("us_taker", SignalState.STALE, 0.0, {})
    if s.us_breakout_dir == "none":
        return SignalResult("us_taker", SignalState.NEUTRAL, 0.0, {})

    ev = {"taker_ratio": round(s.us_taker_ratio, 3)}
    if s.us_taker_ratio >= c.taker_bull_thr:
        return SignalResult("us_taker", SignalState.BULL, 0.5, ev)
    if s.us_taker_ratio <= c.taker_bear_thr:
        return SignalResult("us_taker", SignalState.BEAR, -0.5, ev)
    return SignalResult("us_taker", SignalState.NEUTRAL, 0.0, ev)


def eval_qqq_gate(s: MarketSnapshot, c) -> SignalResult:
    """大盤閘（取代加密引擎的 BTC 閘）：大盤逆風時擋對應方向的突破。"""
    if s.is_stale("qqq_chg_24h_pct"):
        return SignalResult("qqq_gate", SignalState.STALE, 0.0, {})

    ev = {"qqq_chg_24h_pct": s.qqq_chg_24h_pct}
    if s.us_breakout_dir == "bull" and s.qqq_chg_24h_pct < c.qqq_block_long_below:
        return SignalResult("qqq_gate", SignalState.BLOCK, 0.0, ev)
    if s.us_breakout_dir == "bear" and s.qqq_chg_24h_pct > c.qqq_block_short_above:
        return SignalResult("qqq_gate", SignalState.BLOCK, 0.0, ev)
    return SignalResult("qqq_gate", SignalState.NEUTRAL, 0.0, ev)


US_DIRECTIONAL = (eval_us_breakout, eval_us_volume_surge,
                  eval_us_funding_extreme, eval_us_taker_skew)
