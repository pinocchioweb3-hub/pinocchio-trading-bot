# -*- coding: utf-8 -*-
"""supervisor 健康檢查口徑（v108）：暫時限流(429)≠真失聯，不該推[嚴重]。"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import l3_dispatcher.supervisor as sup
from l3_dispatcher.supervisor import run_health_checks, SupervisorState


class FakeSource:
    name = "coinglass"

    def __init__(self, health):
        self._h = health

    async def health(self):
        return self._h


def _run(state, health, monkeypatch):
    monkeypatch.setattr(sup, "queue_stats", lambda: {"queued": 0, "failed": 0})
    monkeypatch.setattr(sup, "capture_health", lambda **k: {"verdict": "ok"})
    res = asyncio.run(run_health_checks(state, FakeSource(health)))
    return [c for c in res if c.kind.startswith("source")]


def test_source_ok_resets_streak(monkeypatch):
    st = SupervisorState(); st.source_fail_streak = 2
    assert _run(st, {"ok": True}, monkeypatch) == []
    assert st.source_fail_streak == 0


def test_transient_429_is_info_not_pushed(monkeypatch):
    """單次 429（冷啟動爆量）→ info；run_supervisor_loop 只推 warn/alert，故不推[嚴重]。"""
    st = SupervisorState()
    r = _run(st, {"ok": False, "details": "Too Many Requests", "code": "API_ERROR"}, monkeypatch)
    assert len(r) == 1 and r[0].kind == "source_rate_limited"
    assert r[0].severity == "info"            # info 不在 actionable(warn/alert) → 不推
    assert st.source_fail_streak == 1


def test_persistent_429_escalates_to_warn(monkeypatch):
    """連續 ≥3 次限流 → warn[警告]（非[嚴重]），提示降頻(task#68)。"""
    st = SupervisorState(); st.source_fail_streak = 2     # 本次 +1 = 3
    r = _run(st, {"ok": False, "details": "429 Too Many Requests"}, monkeypatch)
    assert r[0].severity == "warn" and st.source_fail_streak == 3


def test_genuine_outage_stays_alert(monkeypatch):
    """非 429 的真失聯（連不上）→ 維持 alert[嚴重]，從第一次就升級。"""
    st = SupervisorState()
    r = _run(st, {"ok": False, "details": "Connection refused", "code": "API_ERROR"}, monkeypatch)
    assert r[0].kind == "source_down" and r[0].severity == "alert"
