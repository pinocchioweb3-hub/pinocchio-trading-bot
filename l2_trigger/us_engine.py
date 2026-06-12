"""美股永續突破引擎（v17，實驗性）— 獨立於 engine.py。

不註冊進 _SETUP_SIGNALS：避免加密引擎的 BTC gate / hot 名單語意污染。
輸出沿用 TriggerDecision（CooldownStore 介面直接相容）。

風控隔離不變量（絕對遵守）：
    1. 永不 import fire_queue / dispatcher / risk_manager / trade_journal
    2. 訊號只寫 paper_trades（setup='us_breakout'），無「已下單」按鈕
    3. 美股自有上限 2 倉（worker 層執行）
    4. 紙上 ≥30 筆平倉且 avg_R>0 且人工 review 前，拒絕任何實單化
"""
from __future__ import annotations

from .configs.us_breakout import USBreakoutConfig
from .signals_us import US_DIRECTIONAL, eval_qqq_gate
from .types import MarketSnapshot, SignalResult, SignalState, TriggerAction, TriggerDecision


def evaluate_us(s: MarketSnapshot, c: USBreakoutConfig) -> TriggerDecision:
    """評估一檔美股永續。Q2 投票政策：≥2 同向且對向=0 → FIRE。"""
    # Step 0: 夜間/週末停發（永續無現貨錨定，突破訊號本質失效）
    if s.us_session == "off" or s.us_session is None:
        return TriggerDecision(TriggerAction.HOLD, SignalState.NEUTRAL,
                               c.setup_name, (), 0.0, s, "off_session")

    # Step 1: 無突破 → 不浪費評估
    if s.us_breakout_dir == "none":
        return TriggerDecision(TriggerAction.HOLD, SignalState.NEUTRAL,
                               c.setup_name, (), 0.0, s, "no_breakout")

    # Step 2: QQQ 大盤閘
    gate = eval_qqq_gate(s, c)
    if gate.state == SignalState.BLOCK:
        return TriggerDecision(TriggerAction.HOLD, SignalState.NEUTRAL,
                               c.setup_name, (gate,), 0.0, s, "qqq_gate_closed")
    if gate.state == SignalState.STALE:
        return TriggerDecision(TriggerAction.HOLD, SignalState.NEUTRAL,
                               c.setup_name, (gate,), 0.0, s, "qqq_stale")

    # Step 3: 收集方向票
    results: list[SignalResult] = [fn(s, c) for fn in US_DIRECTIONAL]
    bulls = [r for r in results if r.state == SignalState.BULL]
    bears = [r for r in results if r.state == SignalState.BEAR]

    # Step 4: Q2 投票
    if len(bulls) >= c.min_confirmations and len(bears) == 0:
        score = sum(r.score for r in bulls)
        names = ", ".join(r.name for r in bulls)
        return TriggerDecision(TriggerAction.FIRE, SignalState.BULL,
                               c.setup_name, tuple(results), round(score, 3), s,
                               f"US bull breakout: {names}")
    if len(bears) >= c.min_confirmations and len(bulls) == 0:
        score = sum(r.score for r in bears)
        names = ", ".join(r.name for r in bears)
        return TriggerDecision(TriggerAction.FIRE, SignalState.BEAR,
                               c.setup_name, tuple(results), round(score, 3), s,
                               f"US bear breakout: {names}")

    return TriggerDecision(TriggerAction.HOLD, SignalState.NEUTRAL,
                           c.setup_name, tuple(results), 0.0, s,
                           f"votes_insufficient (bull={len(bulls)}, bear={len(bears)})")
