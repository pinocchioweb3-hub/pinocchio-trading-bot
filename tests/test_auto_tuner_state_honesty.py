"""v200：每日復盤戳記檔「壞掉／讀不到」不再折成「今天還沒跑過」，且戳記寫不進去不再變成熱迴圈。

破口（l3_dispatcher/auto_tuner.py）：
  ① _load_last_review_date() 把「沒有檔（真·從未復盤）」與「檔在但讀不出／壞檔／形狀不對」
     折成同一個 None；迴圈把 None 讀成「今日尚未跑」。
  ② 更致命的是控制流：補跑分支結尾是 `continue`，**中間沒有任何 sleep**，唯一擋住熱迴圈的
     是 _stamp_review_date() 有寫成功。而該函式明文吞掉寫入失敗（只印警告）。
     ⇒ 只要「戳記寫不進去」（唯讀檔／ACL／磁碟滿／路徑被佔），或「戳記讀不出來」而寫入也失敗，
       迴圈就會以最快速度無限重跑整份每日復盤：DB 全掃 + lessons rebuild + 優化器 + 每輪一則
       Telegram。這不需要壞檔就成立——單純寫入失敗即可觸發。

治法：讀取三態（missing/unreadable/ok，只有「不存在」算真沒有歷史）＋**行程內記憶**
（本行程今天確實跑過就算跑過，與持久化成敗脫鉤）＋原子寫（temp+fsync+os.replace）＋壞檔留證＋出聲。
⚠️ 不可用「跳過復盤」來擋熱迴圈：復盤是模型變強的唯一回路，跳掉比多跑一次糟得多。

全離線、零網路、零真錢、不碰 strength.py／eval_cvd_divergence。
"""
import asyncio
import datetime as dt
import json
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import l3_dispatcher.auto_tuner as at


class _StopLoop(Exception):
    """哨兵：終止 run_auto_tuner_loop 的 while True。"""


class _AsyncioShim:
    def __init__(self, sleep_fn):
        self.sleep = sleep_fn


def _utc(y, m, d, h):
    return dt.datetime(y, m, d, h, 0, 0, tzinfo=dt.timezone.utc)


# ────────────────────────── 讀取三態（單元層） ──────────────────────────

def _point_state_at(monkeypatch, tmp_path):
    monkeypatch.setattr(at, "_review_state_path", lambda: tmp_path / "auto_tuner_state.json")
    return tmp_path / "auto_tuner_state.json"


def test_missing_file_is_missing_not_unreadable(monkeypatch, tmp_path):
    """反向護欄：真的沒有檔＝真·從未復盤，必須維持『補跑』語意。"""
    _point_state_at(monkeypatch, tmp_path)
    assert at._load_last_review_status() == (None, at.LOAD_MISSING)


def test_good_file_returns_date(monkeypatch, tmp_path):
    p = _point_state_at(monkeypatch, tmp_path)
    p.write_text(json.dumps({"last_review_date": "2026-06-21"}), encoding="utf-8")
    assert at._load_last_review_status() == ("2026-06-21", at.LOAD_OK)


def test_corrupt_json_is_unreadable_not_missing(monkeypatch, tmp_path):
    """半截 JSON（斷電寫到一半的典型殘骸）＝內容未知，⛔ 不可折成『從未復盤』。"""
    p = _point_state_at(monkeypatch, tmp_path)
    p.write_text('{"last_review_date": "2026-06-2', encoding="utf-8")
    val, status = at._load_last_review_status()
    assert (val, status) == (None, at.LOAD_UNREADABLE)


def test_corrupt_file_keeps_forensic_copy(monkeypatch, tmp_path):
    """壞檔要留鑑識副本（原檔下一輪就會被蓋掉），且原檔逐位元不動。"""
    p = _point_state_at(monkeypatch, tmp_path)
    raw = '{"last_review_date": "2026-06-2'
    p.write_text(raw, encoding="utf-8")
    at._load_last_review_status()
    assert p.read_text(encoding="utf-8") == raw          # 原檔不動
    assert (tmp_path / "auto_tuner_state.bad").exists()  # 留證


def test_wrong_shape_is_unreadable(monkeypatch, tmp_path):
    """合法 JSON 但形狀不對（非 dict／缺鍵／型別錯）一律＝未知，不可當成沒跑過。"""
    p = _point_state_at(monkeypatch, tmp_path)
    for raw in ('["2026-06-21"]', '{}', '{"last_review_date": 20260621}'):
        p.write_text(raw, encoding="utf-8")
        assert at._load_last_review_status() == (None, at.LOAD_UNREADABLE), raw


def test_unreadable_speaks_up(monkeypatch, tmp_path, capsys):
    """讀不出來必須出聲——靜默折疊正是本族群 19 次的共同病灶。"""
    p = _point_state_at(monkeypatch, tmp_path)
    p.write_text("{bad", encoding="utf-8")
    at._load_last_review_status()
    out = capsys.readouterr().out
    assert "讀不出來" in out


def test_missing_stays_quiet(monkeypatch, tmp_path, capsys):
    """反向護欄：真的沒有檔是誠實的預期值，不可製造慢性假警報。"""
    _point_state_at(monkeypatch, tmp_path)
    at._load_last_review_status()
    assert "讀不出來" not in capsys.readouterr().out


# ────────────────────────── 原子寫（單元層） ──────────────────────────

def test_stamp_write_is_atomic_and_leaves_no_debris(monkeypatch, tmp_path):
    """os.replace 失敗時：原檔逐位元不動、不留 .tmp 殘骸、回報失敗（不可靜默宣稱成功）。"""
    import os as _os
    p = _point_state_at(monkeypatch, tmp_path)
    original = json.dumps({"last_review_date": "2026-06-20"})
    p.write_text(original, encoding="utf-8")

    def boom(src, dst):
        raise OSError("replace failed")

    monkeypatch.setattr(_os, "replace", boom)
    ok = at._stamp_review_date("2026-06-21")
    assert ok is False                                     # 明確回報失敗
    assert p.read_text(encoding="utf-8") == original       # 原檔不動
    assert [q.name for q in tmp_path.glob("*.tmp")] == []  # 無殘骸


def test_stamp_write_success_reports_true(monkeypatch, tmp_path):
    p = _point_state_at(monkeypatch, tmp_path)
    assert at._stamp_review_date("2026-06-21") is True
    assert json.loads(p.read_text(encoding="utf-8"))["last_review_date"] == "2026-06-21"


# ────────────────────────── 控制流（迴圈層，才是致命處） ──────────────────────────

def _drive(monkeypatch, *, now_dt, load_result, stamp_ok, max_reviews=5, max_sleeps=1,
           target_hour_utc=2, warmup_seconds=0):
    """驅動 run_auto_tuner_loop。

    load_result＝每次讀狀態回傳的 (date, status)（可為 callable，供持久化成功時演進）；
    stamp_ok＝戳記寫入是否成功（False＝模擬唯讀檔／磁碟滿，狀態永不前進）；
    max_reviews＝第幾次復盤拋 _StopLoop（用來抓熱迴圈：舊碼會一路撞到上限）。
    """
    reviews, stamped, sleeps = [], [], []

    async def fake_review(tg):
        reviews.append(True)
        if len(reviews) >= max_reviews:
            raise _StopLoop            # 熱迴圈保險絲

    async def fake_sleep(secs):
        sleeps.append(secs)
        if len(sleeps) >= max_sleeps:
            raise _StopLoop

    def fake_stamp(d):
        stamped.append(d)
        return stamp_ok

    loader = load_result if callable(load_result) else (lambda: load_result)
    monkeypatch.setattr(at, "_run_daily_review", fake_review)
    monkeypatch.setattr(at, "_load_last_review_status", loader)
    monkeypatch.setattr(at, "_stamp_review_date", fake_stamp)
    monkeypatch.setattr(at, "_now_utc", lambda: now_dt)
    monkeypatch.setattr(at, "asyncio", _AsyncioShim(fake_sleep))

    with pytest.raises(_StopLoop):
        asyncio.run(at.run_auto_tuner_loop(
            None, target_hour_utc=target_hour_utc, warmup_seconds=warmup_seconds))
    return reviews, stamped, sleeps


def test_stamp_failure_does_not_hot_loop(monkeypatch):
    """戳記寫不進去（唯讀檔／磁碟滿）→ 仍只跑一次，然後進排程睡眠。

    舊碼：state 永不前進 → ran_today 永遠 False → `continue` 立刻再跑一次整份復盤，
    無限熱迴圈（DB 全掃 + 優化器 + 每輪一則 Telegram）。這不需要壞檔就會發生。
    """
    reviews, _, sleeps = _drive(
        monkeypatch, now_dt=_utc(2026, 6, 21, 5),
        load_result=("2026-06-20", at.LOAD_OK), stamp_ok=False)
    assert len(reviews) == 1     # 嚴格一次；舊碼會撞到 max_reviews=5
    assert len(sleeps) == 1      # 有進入排程睡眠＝迴圈沒有空轉


def test_unreadable_state_runs_once_then_sleeps(monkeypatch):
    """狀態檔壞掉且寫入也失敗 → 復盤仍跑（回路不可斷），但只跑一次。"""
    reviews, _, sleeps = _drive(
        monkeypatch, now_dt=_utc(2026, 6, 21, 5),
        load_result=(None, at.LOAD_UNREADABLE), stamp_ok=False)
    assert len(reviews) == 1
    assert len(sleeps) == 1


def test_unreadable_state_is_announced_in_loop(monkeypatch, capsys):
    """迴圈層也要出聲：⛔ 不可把『不知道今天跑過沒有』當成『今天沒跑過』默默處理。"""
    _drive(monkeypatch, now_dt=_utc(2026, 6, 21, 5),
           load_result=(None, at.LOAD_UNREADABLE), stamp_ok=False)
    out = capsys.readouterr().out
    assert "讀不出來" in out or "未知" in out


def test_missing_state_still_catches_up_once(monkeypatch):
    """反向護欄：真·從未復盤（沒有檔）＋已過觸發點 → 補跑一次（原設計不可被弱化）。"""
    reviews, stamped, _ = _drive(
        monkeypatch, now_dt=_utc(2026, 6, 21, 5),
        load_result=(None, at.LOAD_MISSING), stamp_ok=True)
    assert len(reviews) == 1
    assert stamped == ["2026-06-21"]


def test_already_ran_today_still_skips(monkeypatch):
    """反向護欄：持久化說今天跑過 → 零補跑，直接睡（去重不可失效）。"""
    reviews, stamped, sleeps = _drive(
        monkeypatch, now_dt=_utc(2026, 6, 21, 5),
        load_result=("2026-06-21", at.LOAD_OK), stamp_ok=True)
    assert reviews == []
    assert stamped == []
    assert len(sleeps) == 1


def test_before_fire_time_still_no_catchup(monkeypatch):
    """反向護欄：未過觸發點 → 不補跑（即使狀態讀不出來也不提前開跑）。"""
    reviews, _, _ = _drive(
        monkeypatch, now_dt=_utc(2026, 6, 21, 1),
        load_result=(None, at.LOAD_UNREADABLE), stamp_ok=False)
    assert reviews == []
