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


# ------------------------------------------------- v115 LLM 合成健康告警
def test_llm_synth_down_alerts_after_3_failures(monkeypatch, tmp_path):
    """連續失敗≥3 → alert；401 類錯誤附 /login 指引。"""
    import json as _json
    import botpaths as _bp
    monkeypatch.setattr(_bp, "data_dir", lambda: tmp_path)
    (tmp_path / "synth_health.json").write_text(_json.dumps(
        {"consecutive_failures": 4,
         "last_error": "claude exit=1: 401 Invalid authentication"}), encoding="utf-8")
    st = SupervisorState()
    r = _run(st, {"ok": True}, monkeypatch)   # source ok，只看 synth 檢查
    # _run 只回 source* 類——改抓全部
    res = asyncio.run(run_health_checks(st, FakeSource({"ok": True})))
    hits = [c for c in res if c.kind == "llm_synth_down"]
    assert len(hits) == 1 and hits[0].severity == "alert"
    assert "/login" in hits[0].message and "停擺" in hits[0].message


def test_llm_synth_healthy_no_alert(monkeypatch, tmp_path):
    import json as _json
    import botpaths as _bp
    monkeypatch.setattr(_bp, "data_dir", lambda: tmp_path)
    (tmp_path / "synth_health.json").write_text(_json.dumps(
        {"consecutive_failures": 2, "last_error": "timeout"}), encoding="utf-8")
    st = SupervisorState()
    monkeypatch.setattr(sup, "queue_stats", lambda: {"queued": 0, "failed": 0})
    monkeypatch.setattr(sup, "capture_health", lambda **k: {"verdict": "ok"})
    res = asyncio.run(run_health_checks(st, FakeSource({"ok": True})))
    assert not [c for c in res if c.kind == "llm_synth_down"]   # <3 不吵
