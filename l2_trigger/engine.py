"""L2 觸發引擎核心：evaluate() 純函式。

流程：
    1. BTC 閘檢查（若 require_gate_open）→ BLOCK/STALE 整包 HOLD
    2. 過濾型訊號掃描 → 任一 BEAR/STALE → HOLD（filter_failed）
    3. 方向型訊號評估 → 收集 BULL/BEAR/NEUTRAL/STALE
    4. OI fuel 檢查（若 require_oi_fuel；Setup A 用、Setup B 設 False）
    5. 投票政策（Q2）：BULL ≥ min_confirmations AND BEAR = 0 → FIRE BULL
                       BEAR ≥ min_confirmations AND BULL = 0 → FIRE BEAR
                       否則 HOLD（含計票細節在 reason）
"""
from __future__ import annotations

from .signals import (
    AMBUSH_DIRECTIONAL,
    AMBUSH_FILTERS,
    INTRADAY_DIRECTIONAL,
    INTRADAY_FILTERS,
    MA_CROSSOVER_DIRECTIONAL,
    MA_CROSSOVER_FILTERS,
    eval_btc_gate,
    eval_oi_trajectory,
)
from .types import (
    MarketSnapshot,
    SignalResult,
    SignalState,
    TriggerAction,
    TriggerConfig,
    TriggerDecision,
)


# 每個 setup 對應的訊號集（在 signals.py 集中定義）
_SETUP_SIGNALS = {
    "intraday":     {"directional": INTRADAY_DIRECTIONAL,     "filters": INTRADAY_FILTERS},
    "ambush":       {"directional": AMBUSH_DIRECTIONAL,       "filters": AMBUSH_FILTERS},
    "ma_crossover": {"directional": MA_CROSSOVER_DIRECTIONAL, "filters": MA_CROSSOVER_FILTERS},
}


def _hold(reason: str, sigs: tuple[SignalResult, ...],
          snapshot: MarketSnapshot, setup_name: str) -> TriggerDecision:
    return TriggerDecision(
        action=TriggerAction.HOLD,
        direction=SignalState.NEUTRAL,
        setup_name=setup_name,
        confirmed=sigs,
        composite_score=0.0,
        snapshot=snapshot,
        reason=reason,
    )


def evaluate(s: MarketSnapshot, c: TriggerConfig) -> TriggerDecision:
    """L2 主入口。純函式、無副作用。"""
    setup = c.setup_name
    if setup not in _SETUP_SIGNALS:
        return _hold(f"unknown_setup:{setup}", (), s, setup)

    sigs: list[SignalResult] = []

    # --- Step 1: BTC 閘 -------------------------------------------------
    if c.require_gate_open:
        gate = eval_btc_gate(s, c)
        sigs.append(gate)
        if gate.state == SignalState.BLOCK:
            return _hold("btc_gate_closed", tuple(sigs), s, setup)
        if gate.state == SignalState.STALE:
            return _hold("btc_gate_stale", tuple(sigs), s, setup)

    # --- Step 2: 過濾型訊號（任一不通過 → HOLD）-------------------------
    for f in _SETUP_SIGNALS[setup]["filters"]:
        r = f(s, c)
        sigs.append(r)
        if r.state == SignalState.BEAR:
            return _hold(f"filter_failed:{r.name}", tuple(sigs), s, setup)
        if r.state == SignalState.STALE:
            return _hold(f"filter_stale:{r.name}", tuple(sigs), s, setup)

    # --- Step 3: 方向型訊號評估 -----------------------------------------
    directional: list[SignalResult] = []
    for f in _SETUP_SIGNALS[setup]["directional"]:
        r = f(s, c)
        directional.append(r)
        sigs.append(r)

    # --- Step 4: OI fuel（Setup A 用；B 不要求，設 False 即跳過）-------
    fuel_ok = True
    if c.require_oi_fuel:
        oi = eval_oi_trajectory(s, c)
        sigs.append(oi)
        if oi.state == SignalState.STALE:
            return _hold("oi_fuel_stale", tuple(sigs), s, setup)
        fuel_ok = bool(oi.evidence.get("fuel", False))
        if not fuel_ok:
            delta = oi.evidence.get("oi_delta_pct", 0.0)
            return _hold(f"oi_fuel_insufficient(delta={delta:.2f}%)",
                         tuple(sigs), s, setup)

    # --- Step 5: 投票（Q2 政策）-----------------------------------------
    bulls = [r for r in directional if r.state == SignalState.BULL]
    bears = [r for r in directional if r.state == SignalState.BEAR]
    stales = [r for r in directional if r.state == SignalState.STALE]

    if len(bulls) >= c.min_confirmations and len(bears) == 0:
        direction, hits = SignalState.BULL, bulls
    elif len(bears) >= c.min_confirmations and len(bulls) == 0:
        direction, hits = SignalState.BEAR, bears
    else:
        return _hold(
            f"votes_insufficient: bull={len(bulls)} bear={len(bears)} "
            f"stale={len(stales)} need>={c.min_confirmations}",
            tuple(sigs), s, setup,
        )

    composite_score = sum(r.score for r in hits)
    names = "+".join(r.name for r in hits)

    return TriggerDecision(
        action=TriggerAction.FIRE,
        direction=direction,
        setup_name=setup,
        confirmed=tuple(sigs),
        composite_score=composite_score,
        snapshot=s,
        reason=f"{setup}/{direction.value}: {names} | oi_fuel={fuel_ok}",
    )
