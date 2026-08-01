"""memguard 的「清了 N 個」必須是**驗證過**的數字，不是「送出過 N 次 taskkill」。

真實情境（2026-08-01 r82 稽核時發現，v188 收工後同一支檔案的下一個洞）：
    07:34 線上第一次觸發常態清掃，log 寫「常態清掃：清 4 個殭屍 runner」。
    但清理迴圈長這樣——

        try:
            subprocess.run(["taskkill", "/PID", str(pid), "/F"], ...)
            killed.append(pid)
        except Exception:
            continue

    `subprocess.run` 不帶 check=True **不會**因非零退出而拋例外，而 taskkill 的
    非零退出正是「沒殺成」的正常回報方式：
        rc=128 → 行程根本不在（可能早就自己結束了）
        rc=1   → 存取被拒（權限不足／行程受保護）
    兩種情況都會走進 `killed.append(pid)`。⇒ log 與 Telegram 宣稱「清理 4 個」，
    真實可能是 0 個；而且沒有任何一行字看得出差別。

    ⚠️ 本專案同物種第 9 次：**未驗證的結果被當成已完成的事實**。
    第 8 次（v188）是「量不到折成 0 個」，這次是反向——「沒做到折成做到了」。
    後果不只是報表難看：緊急線那條路徑會接著寫 memguard_last_ts 冷卻戳記，
    等於用一次**沒發生的清理**換來 10 分鐘不再嘗試，而記憶體仍然滿的。

本測試釘死的邊界：
    - taskkill 非零退出 ⇒ 該 PID **不得**計入 killed。
    - 一個都沒殺成 ⇒ 措辭必須與「清成功了」不同，且不得吃掉清理冷卻（下輪要重試）。
    - 部分成功 ⇒ 成功數與失敗數都要看得見（失敗不可靜音）。
    - rc=0 仍然算清掉（回歸保護：不可為了誠實把正常路徑一起改壞）。
    - ⛔ 不改任何門檻（MEM_MIN_AGE_MIN／4 個／MEM_COOLDOWN_SEC／MEM_EMERGENCY_PCT）。
"""
from __future__ import annotations

import time as _real_time

import pytest

import watchdog as wd


class _FakeClock:
    def __init__(self, start: float) -> None:
        self.now = start

    def time(self) -> float:
        return self.now

    def strftime(self, fmt: str, *a):
        return _real_time.strftime(fmt, *a) if a else _real_time.strftime(fmt)

    def advance(self, sec: float) -> None:
        self.now += sec


def _rig(tmp_path, monkeypatch, *, kill_rc, kill_err=b""):
    """把 memguard 架在暫存目錄上；taskkill 一律回 kill_rc。回 (clock, sent, kills)。"""
    clock = _FakeClock(1_700_000_000.0)
    sent: list[str] = []
    kills: list[list[str]] = []

    monkeypatch.setattr(wd, "time", clock)
    monkeypatch.setattr(wd, "STATE", tmp_path / "watchdog_state.json")
    monkeypatch.setattr(wd, "WLOG", tmp_path / "watchdog.log")
    monkeypatch.setattr(wd, "telegram_alert", lambda text: sent.append(text))
    monkeypatch.setattr(wd, "MEMGUARD_ON", True)

    def _fake_run(cmd, *a, **kw):
        kills.append(list(cmd))
        rc = kill_rc(len(kills)) if callable(kill_rc) else kill_rc

        class _R:
            returncode = rc
            stdout = b""
            stderr = kill_err

        return _R()

    monkeypatch.setattr(wd.subprocess, "run", _fake_run)
    return clock, sent, kills


def _four_zombies():
    """4 個都老於門檻的殭屍（剛好踩到常態清掃的 ≥4 個條件）。"""
    # v189：第三欄=啟動分鐘（v188 排程指紋;5=:03-:07 窗內=可清的排程殭屍）
    return [(1001, 99_999.0, 5), (1002, 99_998.0, 5),
            (1003, 99_997.0, 35), (1004, 99_996.0, 35)]


def _memguard_lines() -> list[str]:
    return [ln for ln in wd.WLOG.read_text(encoding="utf-8").splitlines()
            if "[memguard]" in ln]


# --- ① 沒殺成不得計入 -------------------------------------------------------


def test_routine_sweep_does_not_claim_kills_that_failed(tmp_path, monkeypatch):
    """常態清掃：4 個全部 taskkill 失敗，⛔ 不可寫成「清 4 個」。

    改動前的碼會寫「常態清掃：清 4 個殭屍 runner」（本測試會紅）。
    """
    _clock, _sent, _kills = _rig(tmp_path, monkeypatch, kill_rc=128,
                                 kill_err=b"ERROR: process not found")
    monkeypatch.setattr(wd, "_commit_pct", lambda: 56.0)
    monkeypatch.setattr(wd, "_stale_claude_runners", _four_zombies)

    wd.memory_guard()

    lines = _memguard_lines()
    assert lines, "常態清掃整段無聲"
    txt = "\n".join(lines)
    assert "清 4 個" not in txt, f"沒殺成卻宣稱清了 4 個：{txt!r}"
    assert "清理殭屍 runner 4 個" not in txt, f"同上：{txt!r}"


def test_emergency_does_not_claim_kills_that_failed(tmp_path, monkeypatch):
    """緊急線：taskkill 全失敗時，log 與 Telegram 都不得宣稱清掉了。"""
    _clock, sent, _kills = _rig(tmp_path, monkeypatch, kill_rc=1,
                                kill_err=b"ERROR: Access is denied")
    monkeypatch.setattr(wd, "_commit_pct", lambda: 95.0)
    monkeypatch.setattr(wd, "_stale_claude_runners", _four_zombies)

    wd.memory_guard()

    txt = "\n".join(_memguard_lines())
    assert "清理殭屍 runner 4 個" not in txt, \
        f"存取被拒卻宣稱清理 4 個：{txt!r}"
    joined = "\n".join(sent)
    assert "自動清理 4 個" not in joined, \
        f"Telegram 對使用者宣稱清了 4 個（實際 0 個）：{joined!r}"


# --- ② 失敗必須看得見 -------------------------------------------------------


def test_kill_failure_is_visible_in_log(tmp_path, monkeypatch):
    """一個都沒殺成必須留痕——否則等於靜音失敗（同物種第 9 次的本體）。"""
    _clock, _sent, _kills = _rig(tmp_path, monkeypatch, kill_rc=1,
                                 kill_err=b"Access is denied")
    monkeypatch.setattr(wd, "_commit_pct", lambda: 56.0)
    monkeypatch.setattr(wd, "_stale_claude_runners", _four_zombies)

    wd.memory_guard()

    txt = "\n".join(_memguard_lines())
    assert txt, "整段無聲"
    assert ("殺不掉" in txt or "沒殺成" in txt
            or "失敗" in txt), f"殺不掉卻沒有任何失敗字樣：{txt!r}"


def test_partial_success_shows_both_counts(tmp_path, monkeypatch):
    """4 個裡只殺成 2 個：成功數要對，失敗數也不可靜音。"""
    _clock, _sent, _kills = _rig(tmp_path, monkeypatch,
                                 kill_rc=lambda n: 0 if n <= 2 else 1)
    monkeypatch.setattr(wd, "_commit_pct", lambda: 56.0)
    monkeypatch.setattr(wd, "_stale_claude_runners", _four_zombies)

    wd.memory_guard()

    txt = "\n".join(_memguard_lines())
    assert "2" in txt, f"成功 2 個沒寫出來：{txt!r}"
    assert "清 4 個" not in txt, f"把 2 個成功寫成 4 個：{txt!r}"


# --- ③ 沒殺成不得吃掉重試機會 ----------------------------------------------


def test_zero_verified_kill_does_not_eat_cooldown(tmp_path, monkeypatch):
    """全失敗 ⇒ 不寫 memguard_last_ts，下一輪（3 分鐘後）還能再試。

    沿用 v188 的既有原則：沒發生的動作不該吃掉下一輪的機會。
    """
    clock, _sent, kills = _rig(tmp_path, monkeypatch,
                               kill_rc=lambda n: 128 if n <= 4 else 0)
    monkeypatch.setattr(wd, "_commit_pct", lambda: 56.0)
    monkeypatch.setattr(wd, "_stale_claude_runners", _four_zombies)

    wd.memory_guard()                     # 第一輪：全失敗 → 不寫冷卻
    before = len([c for c in kills if c and c[0] == "taskkill"])
    clock.advance(180)                    # 排程下一輪，短於 MEM_COOLDOWN_SEC
    wd.memory_guard()                     # 這一輪 taskkill 會成功

    after = len([c for c in kills if c and c[0] == "taskkill"])
    assert after > before, \
        "一次全失敗的清理把後續冷卻期一起凍住＝用沒發生的動作換來失明"


# --- ④ 回歸保護：正常路徑不可被改壞 ----------------------------------------


def test_successful_kill_still_counted(tmp_path, monkeypatch):
    """rc=0 仍算清掉，且照常寫冷卻戳記（不可為了誠實把正常路徑改壞）。"""
    clock, _sent, kills = _rig(tmp_path, monkeypatch, kill_rc=0)
    monkeypatch.setattr(wd, "_commit_pct", lambda: 56.0)
    monkeypatch.setattr(wd, "_stale_claude_runners", _four_zombies)

    wd.memory_guard()

    taskkills = [c for c in kills if c and c[0] == "taskkill"]
    assert len(taskkills) == 4, f"正常路徑沒殺滿 4 個：{taskkills!r}"
    txt = "\n".join(_memguard_lines())
    assert "4" in txt, f"成功清 4 個卻沒寫出數字：{txt!r}"
    st = wd.read_json(wd.STATE)
    assert st.get("memguard_last_ts"), "成功清理後未寫冷卻戳記（會變成每 3 分鐘狂殺）"


def test_taskkill_exception_is_not_counted_as_killed(tmp_path, monkeypatch):
    """taskkill 直接拋例外（逾時/找不到執行檔）同樣不得計入。"""
    clock = _FakeClock(1_700_000_000.0)
    monkeypatch.setattr(wd, "time", clock)
    monkeypatch.setattr(wd, "STATE", tmp_path / "watchdog_state.json")
    monkeypatch.setattr(wd, "WLOG", tmp_path / "watchdog.log")
    monkeypatch.setattr(wd, "telegram_alert", lambda text: None)
    monkeypatch.setattr(wd, "MEMGUARD_ON", True)
    monkeypatch.setattr(wd, "_commit_pct", lambda: 56.0)
    monkeypatch.setattr(wd, "_stale_claude_runners", _four_zombies)

    def _boom(cmd, *a, **kw):
        raise wd.subprocess.TimeoutExpired(cmd="taskkill", timeout=15)

    monkeypatch.setattr(wd.subprocess, "run", _boom)

    wd.memory_guard()

    txt = "\n".join(_memguard_lines())
    assert "清 4 個" not in txt, f"全逾時卻宣稱清了 4 個：{txt!r}"
