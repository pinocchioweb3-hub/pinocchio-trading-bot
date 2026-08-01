"""watchdog 殭屍 runner 清單「量不到」不可折成「0 個」—— 迴圈級（非純函式）測試。

真實事故（2026-08-01 r80 量測時自己踩到）：
    `_stale_claude_runners()` 用 `subprocess.run(..., text=True)` 讀 PowerShell 輸出，
    解碼跟隨 locale。當行程被以 `-X utf8` / `PYTHONUTF8=1` 起動時，Python 會拿
    UTF-8 去解 cp950 的輸出 → UnicodeDecodeError → 被 `except Exception: return []`
    吞掉 → 對外表現成「符合指紋的殭屍 0 個」，而 exit code 仍是 0。
    當時實際在線上的殭屍是 **4 個**（Get-CimInstance 交叉驗證），量測卻回報 0。

    ⚠️ 這正是本專案重犯第 8 次的同一物種：**量不到（未知）被折成「沒有」**。
    在 memguard 這條路徑上，後果是「常態清掃永遠不觸發」而且**不會有任何人看見**——
    沒有告警、沒有 log、沒有非零 exit code，只有一個乾淨漂亮的錯誤答案。

本測試釘死的邊界：
    - 量測失敗必須是**可區分的事實**（拋 _RunnerProbeError），⛔ 不得回空表。
    - 量測失敗時 memguard **不得動手殺任何行程**（未知狀態下不動作），
      但必須在本機 watchdog.log 留痕（fail-loud，不是 fail-silent）。
    - 「量到 0 個」與「量不到」的 log 措辭必須不同——否則等於沒區分。
    - 解碼永不拋例外：locale 與實際輸出編碼不一致時要退而求其次，不可整批失明。
    - ⛔ 不改門檻（MEM_MIN_AGE_MIN／4 個／MEM_COOLDOWN_SEC）：那是使用者裁量中的項目。
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


@pytest.fixture()
def rig(tmp_path, monkeypatch):
    """把 memguard 架在暫存目錄上，回傳 (clock, sent_alerts, kill_calls)。"""
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

        class _R:
            returncode = 0
            stdout = b""
            stderr = b""

        return _R()

    monkeypatch.setattr(wd.subprocess, "run", _fake_run)
    return clock, sent, kills


# --- ① 量測失敗必須可區分，不得回空表 -------------------------------------


def test_powershell_nonzero_exit_raises_not_empty_list(monkeypatch):
    """PowerShell 非零退出＝量不到。改動前的碼回 []（本測試會紅）。"""

    def _fake_run(cmd, *a, **kw):
        class _R:
            returncode = 1
            stdout = b""
            stderr = b"boom"

        return _R()

    monkeypatch.setattr(wd.subprocess, "run", _fake_run)
    with pytest.raises(wd._RunnerProbeError):
        wd._stale_claude_runners()


def test_unparsable_output_raises_not_empty_list(monkeypatch):
    """輸出不是 JSON＝量不到（可能是被 profile 汙染或 cmdlet 換了行為）。"""

    def _fake_run(cmd, *a, **kw):
        class _R:
            returncode = 0
            stdout = b"<<not json at all>>"
            stderr = b""

        return _R()

    monkeypatch.setattr(wd.subprocess, "run", _fake_run)
    with pytest.raises(wd._RunnerProbeError):
        wd._stale_claude_runners()


def test_timeout_raises_not_empty_list(monkeypatch):
    """逾時＝量不到，⛔ 不是「機器上沒有殭屍」。"""

    def _fake_run(cmd, *a, **kw):
        raise wd.subprocess.TimeoutExpired(cmd="powershell", timeout=60)

    monkeypatch.setattr(wd.subprocess, "run", _fake_run)
    with pytest.raises(wd._RunnerProbeError):
        wd._stale_claude_runners()


def test_genuine_zero_is_still_zero(monkeypatch):
    """真的沒有 claude.exe（退出碼 0、輸出空）＝ 0 個，這一支不可被上面幾支波及。"""

    def _fake_run(cmd, *a, **kw):
        class _R:
            returncode = 0
            stdout = b"   "
            stderr = b""

        return _R()

    monkeypatch.setattr(wd.subprocess, "run", _fake_run)
    assert wd._stale_claude_runners() == []


# --- ② 編碼不一致不得讓整批失明 -------------------------------------------


def test_cp950_output_survives_utf8_locale(monkeypatch):
    """locale 被改成 UTF-8、輸出卻是 cp950：仍要量到那 1 個 runner。

    改動前的碼在這個情境下 UnicodeDecodeError → except → []（本測試會紅）。
    """
    mark = wd._RUNNER_MARK
    payload = (
        '{"ProcessId":4242,"CommandLine":"C:\\\\Users\\\\\u4f7f\u7528\u8005\\\\'
        + mark.replace("\\", "\\\\")
        + '\\\\cli.js --print","Age":7200}'
    )
    raw = payload.encode("cp950")

    def _fake_run(cmd, *a, **kw):
        class _R:
            returncode = 0
            stdout = raw
            stderr = b""

        return _R()

    monkeypatch.setattr(wd.subprocess, "run", _fake_run)
    monkeypatch.setattr(wd, "_CONSOLE_ENCODINGS", ("utf-8", "cp950"))

    got = wd._stale_claude_runners()
    # v189：第三欄=啟動分鐘（模擬輸出無 Min 欄→-1=未知）；本測試意圖=cp950 解碼存活
    assert got == [(4242, 7200.0, -1)], f"cp950 輸出在 UTF-8 locale 下被吞成 {got!r}"


def test_decode_never_raises_on_garbage():
    """⛔ 解碼這一層永不拋例外——否則失敗又會沿著舊路徑變成空表。"""
    out = wd._decode_console(b"\xff\xfe\x00\x01 plain-ascii-tail")
    assert isinstance(out, str)
    assert "plain-ascii-tail" in out


# --- ③ 未知狀態下 memguard 不動手、但要留痕 --------------------------------


def test_unknown_probe_kills_nothing_and_is_logged(rig, monkeypatch):
    """量不到時：零 taskkill、且 watchdog.log 必須看得到「量測失敗」。"""
    clock, _sent, kills = rig
    monkeypatch.setattr(wd, "_commit_pct", lambda: 95.0)

    def _boom():
        raise wd._RunnerProbeError("powershell exit=1")

    monkeypatch.setattr(wd, "_stale_claude_runners", _boom)

    wd.memory_guard()

    taskkills = [c for c in kills if c and c[0] == "taskkill"]
    assert not taskkills, f"未知狀態下仍動手殺了 {len(taskkills)} 個行程"
    txt = wd.WLOG.read_text(encoding="utf-8")
    assert "\u91cf\u6e2c\u5931\u6557" in txt, f"量不到卻無聲：{txt!r}"


def test_unknown_probe_in_routine_branch_is_logged(rig, monkeypatch):
    """常態清掃段（未達緊急線）同樣不可無聲——這正是 r80 花一輪才查出來的那個洞。"""
    clock, _sent, kills = rig
    monkeypatch.setattr(wd, "_commit_pct", lambda: 53.1)

    def _boom():
        raise wd._RunnerProbeError("UnicodeDecodeError")

    monkeypatch.setattr(wd, "_stale_claude_runners", _boom)

    wd.memory_guard()

    assert not [c for c in kills if c and c[0] == "taskkill"]
    assert "\u91cf\u6e2c\u5931\u6557" in wd.WLOG.read_text(encoding="utf-8")


def test_measured_zero_and_unmeasurable_have_different_wording(rig, monkeypatch):
    """「量到 0 個」與「量不到」的 log 措辭必須不同，否則區分了也讀不出來。"""
    clock, _sent, _kills = rig
    monkeypatch.setattr(wd, "_commit_pct", lambda: 95.0)

    monkeypatch.setattr(wd, "_stale_claude_runners", lambda: [])
    wd.memory_guard()
    clock.advance(wd.MEM_COOLDOWN_SEC + 60)

    def _boom():
        raise wd._RunnerProbeError("x")

    monkeypatch.setattr(wd, "_stale_claude_runners", _boom)
    wd.memory_guard()

    lines = [ln for ln in wd.WLOG.read_text(encoding="utf-8").splitlines()
             if "[memguard]" in ln]
    assert len(lines) >= 2
    assert lines[0] != lines[-1], "兩種完全不同的事實寫出同一句話"
    assert "\u91cf\u6e2c\u5931\u6557" not in lines[0]
    assert "\u91cf\u6e2c\u5931\u6557" in lines[-1]


def test_unknown_probe_does_not_block_next_round_retry(rig, monkeypatch):
    """量測失敗不得吃掉清理冷卻（否則一次解碼失敗換來 10 分鐘失明）。"""
    clock, _sent, kills = rig
    monkeypatch.setattr(wd, "_commit_pct", lambda: 95.0)

    calls = {"n": 0}

    def _flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise wd._RunnerProbeError("transient")
        return [(4242, 99_999.0)]

    monkeypatch.setattr(wd, "_stale_claude_runners", _flaky)

    wd.memory_guard()                 # 第一輪：量不到 → 不動作、不寫冷卻戳記
    clock.advance(180)                # 排程的下一輪（3 分鐘後，短於 600s 冷卻）
    wd.memory_guard()                 # 這一輪量到了 → 必須真的清

    taskkills = [c for c in kills if c and c[0] == "taskkill"]
    assert taskkills, "一次量測失敗把後續 10 分鐘的清理一起冷凍＝失明放大"
