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


# ------------------------------------------------- v123 setup 開單速率突變
def _mk_tj_db(tmp_path, entries):
    """entries=[(setup, hours_ago)...] → 最小 paper_trades 表。"""
    import sqlite3 as _sq
    import time as _t
    db = tmp_path / "trade_journal.db"
    if db.exists():
        db.unlink()
    conn = _sq.connect(str(db))
    conn.execute("CREATE TABLE paper_trades (id INTEGER PRIMARY KEY, setup TEXT, "
                 "entry_at INTEGER)")
    now = _t.time() * 1000
    conn.executemany("INSERT INTO paper_trades (setup, entry_at) VALUES (?, ?)",
                     [(s, now - h * 3600_000) for s, h in entries])
    conn.commit(); conn.close()
    return tmp_path


def test_setup_rate_surge_detects_dormant_awakening(monkeypatch, tmp_path):
    """沉睡引擎甦醒（前7日0筆、24h 12筆）→ warn；穩定源（均速）不吵。"""
    import botpaths as _bp
    _mk_tj_db(tmp_path, [("intraday", i) for i in range(12)]          # 24h 內 12 筆
              + [("deepdive", 30 + i * 12) for i in range(10)])       # deepdive 均速背景
    monkeypatch.setattr(_bp, "db_path", lambda name: tmp_path / name)
    monkeypatch.setattr(sup, "queue_stats", lambda: {"queued": 0, "failed": 0})
    monkeypatch.setattr(sup, "capture_health", lambda **k: {"verdict": "ok"})
    st = SupervisorState()
    res = asyncio.run(run_health_checks(st, FakeSource({"ok": True})))
    hits = [c for c in res if c.kind == "setup_rate_surge"]
    assert len(hits) == 1 and hits[0].severity == "warn"
    assert "intraday" in hits[0].message and "突變" in hits[0].message


def test_setup_rate_surge_quiet_when_steady(monkeypatch, tmp_path):
    """均速（24h 6筆 vs 日均5）→ 不告警；且 6h 自我節流生效。"""
    import time as _t
    import botpaths as _bp
    _mk_tj_db(tmp_path, [("deepdive", i * 4) for i in range(6)]        # 24h 內 6 筆
              + [("deepdive", 25 + i * 4.5) for i in range(35)])       # 前 7 日 35 筆 ≈5/日
    monkeypatch.setattr(_bp, "db_path", lambda name: tmp_path / name)
    monkeypatch.setattr(sup, "queue_stats", lambda: {"queued": 0, "failed": 0})
    monkeypatch.setattr(sup, "capture_health", lambda **k: {"verdict": "ok"})
    st = SupervisorState()
    res = asyncio.run(run_health_checks(st, FakeSource({"ok": True})))
    assert not [c for c in res if c.kind == "setup_rate_surge"]
    # 節流：剛告警過 → 即使突變也不重複
    st2 = SupervisorState(); st2.last_alert_per_kind["setup_rate_surge"] = _t.time()
    _mk_tj_db(tmp_path, [("intraday", i) for i in range(15)])
    res2 = asyncio.run(run_health_checks(st2, FakeSource({"ok": True})))
    assert not [c for c in res2 if c.kind == "setup_rate_surge"]


# ------------------------------------------------- v128 系統資源健康
def test_sys_health_thresholds_and_throttle(monkeypatch, tmp_path):
    """低記憶體→告警(帶建議);3h 節流生效;資源充足→安靜。用假 ctypes/shutil 注入。"""
    import time as _t
    import botpaths as _bp
    monkeypatch.setattr(_bp, "data_dir", lambda: tmp_path)   # synth_health 不存在→跳過
    monkeypatch.setattr(_bp, "db_path", lambda name: tmp_path / name)
    monkeypatch.setattr(sup, "queue_stats", lambda: {"queued": 0, "failed": 0})
    monkeypatch.setattr(sup, "capture_health", lambda **k: {"verdict": "ok"})

    import ctypes as _ct
    import shutil as _shu

    def _fake_memstat(avail_gb):
        def fake_global(byref_obj):
            obj = byref_obj._obj
            obj.ullAvailPhys = int(avail_gb * 1024 ** 3)
            return 1
        return fake_global

    class _FakeK32:
        GlobalMemoryStatusEx = staticmethod(_fake_memstat(0.5))   # 0.5GB → alert
    monkeypatch.setattr(_ct, "windll",
                        type("W", (), {"kernel32": _FakeK32()})(), raising=False)
    monkeypatch.setattr(_shu, "disk_usage",
                        lambda p: type("D", (), {"free": 200 * 1024 ** 3})())  # 磁碟充足
    st = SupervisorState()
    res = asyncio.run(run_health_checks(st, FakeSource({"ok": True})))
    hits = [c for c in res if c.kind == "sys_memory_low"]
    assert len(hits) == 1 and hits[0].severity == "alert"
    assert "凍機" in hits[0].message and "Claude" in hits[0].message
    # 3h 節流：剛告警過 → 同輪再跑不重複
    st.last_alert_per_kind["sys_memory_low"] = _t.time()
    res2 = asyncio.run(run_health_checks(st, FakeSource({"ok": True})))
    assert not [c for c in res2 if c.kind == "sys_memory_low"]
    # 資源充足 → 安靜
    _FakeK32.GlobalMemoryStatusEx = staticmethod(_fake_memstat(8.0))
    st3 = SupervisorState()
    res3 = asyncio.run(run_health_checks(st3, FakeSource({"ok": True})))
    assert not [c for c in res3 if c.kind.startswith("sys_")]
