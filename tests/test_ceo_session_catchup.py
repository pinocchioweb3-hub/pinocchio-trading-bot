"""task#79 每日 CEO 簡報啟動補跑：run_ceo_loop 的補跑/排程控制流。

與 task#78 auto_tuner 同 bug-class：舊 loop 只在每日固定 01:00 UTC 觸發、無補跑、無
run_on_startup；daemon 因開發迭代＋watchdog 頻繁重啟，幾乎從不存活滿一天剛好跨過該秒 →
每日 CEO 彙整簡報幾乎從不可靠送達。

治本＝以 UTC 日期戳記持久化「今日是否已送」，啟動暖機後若今日尚未送且已過觸發點 → 立即
補送一次，之後回正常每日節奏；至多每 UTC 日一次（不像 daily_macro 每次重啟都推，避免洗版）。
本檔以 asyncio shim 攔截 await asyncio.sleep（只攔 ceo_session 模組看到的 asyncio.sleep），
並注入 _now_utc／_send_ceo_brief／狀態讀寫／seed_known_decisions，驗證補送與排程的控制流＝
是否如設計。全離線、零網路、零真錢、零訊號數學。
"""
import asyncio
import datetime as dt
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import l3_dispatcher.ceo_session as ceo


class _StopLoop(Exception):
    """哨兵：在第 N 次 sleep 拋出以終止 run_ceo_loop 的 while True。"""


class _AsyncioShim:
    """只提供 sleep 的 asyncio 替身（loop 內僅用到 asyncio.sleep）。"""
    def __init__(self, sleep_fn):
        self.sleep = sleep_fn


def _utc(y, m, d, h):
    return dt.datetime(y, m, d, h, 0, 0, tzinfo=dt.timezone.utc)


def _drive(monkeypatch, *, now_dt, last_date, max_sleeps,
           target_hour_utc=1, warmup_seconds=0):
    """驅動 run_ceo_loop，回傳 (sends, stamped, sleeps)。

    now_dt 固定；last_date＝持久化的上次送出 UTC 日期（None＝讀失敗/從未送）；
    max_sleeps＝第幾次 sleep 拋 _StopLoop 終止。
    """
    state = {"last": last_date}
    sends = []
    stamped = []
    sleeps = []

    async def fake_send(tg):
        sends.append(True)

    async def fake_sleep(secs):
        sleeps.append(secs)
        if len(sleeps) >= max_sleeps:
            raise _StopLoop

    monkeypatch.setattr(ceo, "_send_ceo_brief", fake_send)
    monkeypatch.setattr(ceo, "_load_last_brief_date", lambda: state["last"])
    monkeypatch.setattr(ceo, "_stamp_brief_date",
                        lambda d: (state.__setitem__("last", d), stamped.append(d)))
    monkeypatch.setattr(ceo, "_now_utc", lambda: now_dt)
    monkeypatch.setattr(ceo, "asyncio", _AsyncioShim(fake_sleep))
    # seed_known_decisions 在 loop 啟動時被呼叫；測試中設為 no-op，避免碰真 DB
    monkeypatch.setattr(ceo._dec, "seed_known_decisions", lambda: None)

    with pytest.raises(_StopLoop):
        asyncio.run(ceo.run_ceo_loop(
            None, target_hour_utc=target_hour_utc, warmup_seconds=warmup_seconds))
    return sends, stamped, sleeps


def test_catchup_fires_when_past_fire_and_not_sent_today(monkeypatch):
    # daemon 在 05:00 UTC 起、上次送是昨天 → 立即補送一次並戳記今日
    sends, stamped, _ = _drive(
        monkeypatch, now_dt=_utc(2026, 6, 21, 5), last_date="2026-06-20",
        max_sleeps=1)
    assert len(sends) == 1                   # 補送剛好一次
    assert stamped == ["2026-06-21"]         # 戳記今日（UTC）


def test_no_catchup_when_already_sent_today(monkeypatch):
    # 今日已送（last==today）→ 不補送，直接睡到明日觸發點
    sends, stamped, sleeps = _drive(
        monkeypatch, now_dt=_utc(2026, 6, 21, 5), last_date="2026-06-21",
        max_sleeps=1)
    assert sends == []                       # 零補送（去重生效）
    assert stamped == []
    assert len(sleeps) == 1                  # 只排程睡眠（到明日 01:00）


def test_no_catchup_before_fire_time(monkeypatch):
    # 00:00 UTC（未過 01:00 觸發點）+ 昨天才送 → 不補送，睡到今日 01:00
    sends, stamped, _ = _drive(
        monkeypatch, now_dt=_utc(2026, 6, 21, 0), last_date="2026-06-20",
        max_sleeps=1)
    assert sends == []                       # 未過觸發點 → 不補送
    assert stamped == []


def test_scheduled_run_fires_after_sleep(monkeypatch):
    # 00:00 起、昨天才送：第 1 次 sleep（睡到今日 01:00）醒來 → 送＋戳記；
    # 第 2 次 sleep（睡到明日）拋停。驗證排程路徑也會送＋戳記。
    sends, stamped, sleeps = _drive(
        monkeypatch, now_dt=_utc(2026, 6, 21, 0), last_date="2026-06-20",
        max_sleeps=2)
    assert len(sends) == 1
    assert stamped == ["2026-06-21"]
    assert len(sleeps) == 2


def test_load_failure_is_conservative_catchup(monkeypatch):
    # 狀態讀失敗（None）＋已過觸發點 → 保守補送（寧可多送一次，純內部簡報安全）
    sends, stamped, _ = _drive(
        monkeypatch, now_dt=_utc(2026, 6, 21, 5), last_date=None,
        max_sleeps=1)
    assert len(sends) == 1
    assert stamped == ["2026-06-21"]


def test_warmup_applied_as_first_sleep(monkeypatch):
    # warmup_seconds>0 → 第一個 sleep＝暖機秒數（避開開機洗版）
    _, _, sleeps = _drive(
        monkeypatch, now_dt=_utc(2026, 6, 21, 0), last_date="2026-06-20",
        max_sleeps=1, warmup_seconds=77)
    assert sleeps[0] == 77


def test_no_double_send_after_catchup(monkeypatch):
    # 補送後（同一 now）再迴圈：sent_today 已 True → 不二度補送，只排程睡眠
    sends, stamped, sleeps = _drive(
        monkeypatch, now_dt=_utc(2026, 6, 21, 5), last_date="2026-06-20",
        max_sleeps=1)
    assert len(sends) == 1                    # 嚴格一次，無重複
    assert len(stamped) == 1
    assert len(sleeps) == 1                   # 補送後即進排程睡眠（被 StopLoop 攔）
