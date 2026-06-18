"""checks.cross_check_fire：BTC 閘一致性的 fail-closed 行為。

對應稽核發現 #2（2026-06-18）：原本 `btc_gate_open is None` 的 BULL FIRE 被
歸到 else 分支當成「對齊、delta +0」，過於樂觀——閘資料缺失/stale 時無法確認
個股與大盤對齊，應保守降分。修正後改為 pass=False、delta=-10。

此檔鎖住三種閘狀態的計分，防回歸：
    True  → +0（對齊）
    None  → -10（資料缺失/stale，保守）   ← 本次修正
    False → -25（明確衝突，強罰）

執行方式：
    pytest tests/test_checks_btc_gate.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from l2_trigger.types import TriggerDecision, TriggerAction, SignalState
from l3_dispatcher.checks import cross_check_fire
from tests import fixtures as F
from tests.fixtures import _replace


def _fire_bull(snap) -> TriggerDecision:
    return TriggerDecision(
        action=TriggerAction.FIRE,
        direction=SignalState.BULL,
        setup_name="test_setup",
        confirmed=(),
        composite_score=0.0,
        snapshot=snap,
        reason="test",
    )


def _btc_check(result) -> dict:
    return next(c for c in result.checks if c["name"] == "btc_gate_alignment")


def test_btc_gate_open_true_is_aligned():
    snap = _replace(F.sui_intraday_fire_bull(), btc_gate_open=True)
    res = asyncio.run(cross_check_fire(_fire_bull(snap)))
    chk = _btc_check(res)
    assert chk["pass"] is True
    assert chk["delta"] == 0


def test_btc_gate_none_is_conservative_not_aligned():
    """資料缺失（None，非明確 False）→ 保守降分 -10，不再當成對齊。"""
    snap = _replace(F.sui_intraday_fire_bull(), btc_gate_open=None)
    res = asyncio.run(cross_check_fire(_fire_bull(snap)))
    chk = _btc_check(res)
    assert chk["pass"] is False
    assert chk["delta"] == -10


def test_btc_gate_open_false_is_strong_penalty():
    snap = _replace(F.sui_intraday_fire_bull(), btc_gate_open=False)
    res = asyncio.run(cross_check_fire(_fire_bull(snap)))
    chk = _btc_check(res)
    assert chk["pass"] is False
    assert chk["delta"] == -25


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
