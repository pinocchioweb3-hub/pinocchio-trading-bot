"""task#78 每日復盤啟動補跑：run_auto_tuner_loop 的補跑/排程控制流。

根因：舊 loop 只在每日固定 02:00 UTC 觸發、無補跑機制；daemon 因開發迭代＋watchdog 頻繁
重啟，幾乎從不存活滿 24h 剛好跨過該秒 → lessons rebuild／auto_optimizer／
entry_policy_optimizer／調參報告「全自動復盤引擎」實際上幾乎從不自動執行
（entry_policy_audit.jsonl 從不存在為證）。

治本＝以 UTC 日期戳記持久化「今日是否已跑」，啟動後若今日尚未跑且已過觸發點 → 立即補跑一次，
之後回正常每日節奏；至多每 UTC 日一次。本檔以 asyncio shim 攔截 await asyncio.sleep（只攔
auto_tuner 模組看到的 asyncio.sleep），並注入 _now_utc／_run_daily_review／狀態讀寫，
驗證補跑與排程的控制流＝是否如設計。全離線、零網路、零真錢、零訊號數學。
"""
import asyncio
import datetime as dt
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import l3_dispatcher.auto_tuner as at


class _StopLoop(Exception):
    """哨兵：在第 N 次 sleep 拋出以終止 run_auto_tuner_loop 的 while True。"""


class _AsyncioShim:
    """只提供 sleep 的 asyncio 替身（loop 內僅用到 asyncio.sleep）。"""
    def __init__(self, sleep_fn):
        self.sleep = sleep_fn


def _utc(y, m, d, h):
    return dt.datetime(y, m, d, h, 0, 0, tzinfo=dt.timezone.utc)


def _drive(monkeypatch, *, now_dt, last_date, max_sleeps,
           target_hour_utc=2, warmup_seconds=0):
    """驅動 run_auto_tuner_loop，回傳 (reviews, stamped, sleeps)。

    now_dt 固定；last_date＝持久化的上次執行 UTC 日期（None＝沒有狀態檔＝真·從未跑）；
    max_sleeps＝第幾次 sleep 拋 _StopLoop 終止。

    v200 起讀取改三態（見 test_auto_tuner_state_honesty.py）：本檔只驅動「檔案讀得出來」
    的正常路徑（None→missing、有值→ok）；「壞檔讀不出來」與「戳記寫不進去」的熱迴圈防護
    在該新檔驗。
    """
    state = {"last": last_date}
    reviews = []
    stamped = []
    sleeps = []

    async def fake_review(tg):
        reviews.append(True)

    async def fake_sleep(secs):
        sleeps.append(secs)
        if len(sleeps) >= max_sleeps:
            raise _StopLoop

    def fake_load():
        v = state["last"]
        return (v, at.LOAD_OK) if v is not None else (None, at.LOAD_MISSING)

    def fake_stamp(d):
        state["last"] = d
        stamped.append(d)
        return True

    monkeypatch.setattr(at, "_run_daily_review", fake_review)
    monkeypatch.setattr(at, "_load_last_review_status", fake_load)
    monkeypatch.setattr(at, "_stamp_review_date", fake_stamp)
    monkeypatch.setattr(at, "_now_utc", lambda: now_dt)
    monkeypatch.setattr(at, "asyncio", _AsyncioShim(fake_sleep))

    with pytest.raises(_StopLoop):
        asyncio.run(at.run_auto_tuner_loop(
            None, target_hour_utc=target_hour_utc, warmup_seconds=warmup_seconds))
    return reviews, stamped, sleeps


def test_catchup_fires_when_past_fire_and_not_run_today(monkeypatch):
    # daemon 在 05:00 UTC 起、上次跑是昨天 → 立即補跑一次並戳記今日
    reviews, stamped, _ = _drive(
        monkeypatch, now_dt=_utc(2026, 6, 21, 5), last_date="2026-06-20",
        max_sleeps=1)
    assert len(reviews) == 1                 # 補跑剛好一次
    assert stamped == ["2026-06-21"]         # 戳記今日（UTC）


def test_no_catchup_when_already_ran_today(monkeypatch):
    # 今日已跑（last==today）→ 不補跑，直接睡到明日觸發點
    reviews, stamped, sleeps = _drive(
        monkeypatch, now_dt=_utc(2026, 6, 21, 5), last_date="2026-06-21",
        max_sleeps=1)
    assert reviews == []                     # 零補跑（去重生效）
    assert stamped == []
    assert len(sleeps) == 1                  # 只排程睡眠（到明日 02:00）


def test_no_catchup_before_fire_time(monkeypatch):
    # 01:00 UTC（未過 02:00 觸發點）+ 昨天才跑 → 不補跑，睡到今日 02:00
    reviews, stamped, _ = _drive(
        monkeypatch, now_dt=_utc(2026, 6, 21, 1), last_date="2026-06-20",
        max_sleeps=1)
    assert reviews == []                     # 未過觸發點 → 不補跑
    assert stamped == []


def test_scheduled_run_fires_after_sleep(monkeypatch):
    # 01:00 起、昨天才跑：第 1 次 sleep（睡到今日 02:00）醒來 → 執行＋戳記；
    # 第 2 次 sleep（睡到明日）拋停。驗證排程路徑也會執行＋戳記。
    reviews, stamped, sleeps = _drive(
        monkeypatch, now_dt=_utc(2026, 6, 21, 1), last_date="2026-06-20",
        max_sleeps=2)
    assert len(reviews) == 1
    assert stamped == ["2026-06-21"]
    assert len(sleeps) == 2


def test_no_state_file_is_conservative_catchup(monkeypatch):
    # 沒有狀態檔（真·從未復盤）＋已過觸發點 → 保守補跑（寧可多跑一次，冪等安全）
    # ⚠️ v200 起「讀失敗」不再走這條：那是 unreadable，另在 test_auto_tuner_state_honesty.py 驗
    reviews, stamped, _ = _drive(
        monkeypatch, now_dt=_utc(2026, 6, 21, 5), last_date=None,
        max_sleeps=1)
    assert len(reviews) == 1
    assert stamped == ["2026-06-21"]


def test_warmup_applied_as_first_sleep(monkeypatch):
    # warmup_seconds>0 → 第一個 sleep＝暖機秒數（避開開機尖峰）
    _, _, sleeps = _drive(
        monkeypatch, now_dt=_utc(2026, 6, 21, 1), last_date="2026-06-20",
        max_sleeps=1, warmup_seconds=99)
    assert sleeps[0] == 99


def test_no_double_run_after_catchup(monkeypatch):
    # 補跑後（同一 now）再迴圈：ran_today 已 True → 不二度補跑，只排程睡眠
    reviews, stamped, sleeps = _drive(
        monkeypatch, now_dt=_utc(2026, 6, 21, 5), last_date="2026-06-20",
        max_sleeps=1)
    assert len(reviews) == 1                  # 嚴格一次，無重複
    assert len(stamped) == 1
    assert len(sleeps) == 1                   # 補跑後即進排程睡眠（被 StopLoop 攔）
