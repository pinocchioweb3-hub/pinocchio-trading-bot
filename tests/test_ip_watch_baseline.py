# -*- coding: utf-8 -*-
"""v180 IP 哨兵：「首輪基線」不得長得像「IP 剛換過」，且「沒變」不得長得像「已死」。

改動前這三支會全紅——v176 的狀態檔只有 {ip, changed_at}：
  1. 哨兵第一次開機也會寫一個「剛剛」的 changed_at ⇒ 讀檔的人（含監督員本人，
     r78 差點如此）會把基線讀成一次真的輪換，據此推出錯誤的 401 歸因。
  2. 只在「IP 變更時」才寫檔 ⇒「IP 穩定沒變」與「哨兵已死」在檔案上完全同形，
     判活只剩 mtime＝又一次拿代理值當事實。
⛔ 一律把 _STATE 導向 tmp_path，絕不碰真的 ip_watch_state.json。
"""
import asyncio
import json

import pytest

from l3_dispatcher import ip_watch


class _TG:
    def __init__(self):
        self.sent = []

    async def send_message(self, msg, parse_mode=None):
        self.sent.append(msg)


def _poll_once(state_path, ip, tg):
    """跑真的 run_ip_watch_loop 一輪後取消（迴圈級，非只驗純函式）。"""

    async def _run():
        async def _fake_ip():
            return ip

        ip_watch._current_ip = _fake_ip
        task = asyncio.ensure_future(ip_watch.run_ip_watch_loop(tg=tg, poll_seconds=120))
        await asyncio.sleep(0.3)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(_run())
    return json.loads(state_path.read_text(encoding="utf-8"))


@pytest.fixture
def state(tmp_path, monkeypatch):
    p = tmp_path / "ip_watch_state.json"
    monkeypatch.setattr(ip_watch, "_STATE", p)
    return p


def test_first_write_is_marked_baseline_not_a_rotation(state):
    """首輪：changed_at 有值不代表換過——rotations==0 才是判據，且不得誤發 TG。"""
    tg = _TG()
    st = _poll_once(state, "203.0.113.7", tg)
    assert st["baseline"] is True
    assert st["rotations"] == 0
    assert tg.sent == []


def test_stable_ip_still_proves_liveness_without_faking_a_change(state):
    """IP 沒變：changed_at 不准動，但 last_seen_at 要前進（判活不必再靠 mtime）。"""
    tg = _TG()
    first = _poll_once(state, "203.0.113.7", tg)
    second = _poll_once(state, "203.0.113.7", tg)
    assert second["changed_at"] == first["changed_at"]
    assert second["last_seen_at"] > first["last_seen_at"]
    assert tg.sent == []


def test_real_rotation_increments_and_alerts(state):
    tg = _TG()
    first = _poll_once(state, "203.0.113.7", tg)
    st = _poll_once(state, "198.51.100.22", tg)
    assert st["rotations"] == 1
    assert st["baseline"] is False
    assert st["changed_at"] > first["changed_at"]
    assert len(tg.sent) == 1


def test_legacy_record_stays_unknown_not_backfilled_to_zero(state):
    """⛔ v180 之前的舊紀錄沒有 rotations：一律讀作『未知』，
    不得補寫 0 冒充「已證實從未輪換」（那正是本次要治的那種假事實）。"""
    state.write_text(json.dumps({"ip": "198.51.100.22", "changed_at": 1.0}), encoding="utf-8")
    st = _poll_once(state, "198.51.100.22", _TG())
    assert "rotations" not in st
    assert isinstance(st["last_seen_at"], float)
