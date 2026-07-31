"""watchdog memguard 的 Telegram 節流 —— 迴圈級（非純函式）測試。

真實事故（2026-07-31 量測）：
    watchdog 每 3 分觸發一次、memguard 動作冷卻 600s，實際約每 12 分產生一則
    Telegram。當天 07-31 單日推播 101 則（57 則「無可清」＋ 44 則「已清理」），
    全部送達成功。同一個 Telegram 頻道正是「真錢執行器 401 恢復」告警的出口——
    我們現在唯一在等的訊號會被埋在這 101 則例行噪音裡。

    ⚠️ 這不是「告警太吵」的美觀問題：memguard 的兩則訊息在同一小時內重複時
    完全不帶新資訊（無可清＝請人工重啟 App；已清理＝自癒成功），而被它稀釋掉的
    那一則是**唯一需要人立刻行動**的。

修法邊界（本測試同時把邊界釘死）：
    - 只節流 **Telegram 推播**；本機 watchdog.log 每一輪照舊全寫（鑑識軌跡不可少）。
    - **不得**改變 memguard 真正的清理節奏（MEM_COOLDOWN_SEC）——那是防衛本體，
      弱化它等於讓機器回到 94% commit 的無聲當機風險。
    - 被壓下的則數必須在下次真正送出時揭露，永不無聲吞掉。
    - 情況惡化（commit% 比上次告警再高 ESCALATE_PCT 以上）要能穿透冷卻。
"""
from __future__ import annotations

import time as _real_time

import pytest

import watchdog as wd


class _FakeClock:
    """可控時鐘：memory_guard 用 time.time()，log() 用 time.strftime()。"""

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
            stdout = ""
            stderr = ""

        return _R()

    monkeypatch.setattr(wd.subprocess, "run", _fake_run)
    return clock, sent, kills


def _run_rounds(clock, n: int, spacing_sec: float = 720.0) -> None:
    """模擬 watchdog 每 3 分觸發、memguard 每約 12 分真正動一次的實際節奏。"""
    for _ in range(n):
        wd.memory_guard()
        clock.advance(spacing_sec)


def test_noop_branch_does_not_flood_telegram(rig, monkeypatch):
    """「無可清」連續 24 輪（≈4.8 小時）不得變成 24 則 Telegram。

    改動前的碼：每輪無條件推播 → 24 則（本測試會紅）。
    """
    clock, sent, _ = rig
    monkeypatch.setattr(wd, "_commit_pct", lambda: 95.0)
    monkeypatch.setattr(wd, "_stale_claude_runners", lambda: [])

    _run_rounds(clock, 24)

    assert len(sent) >= 1, "完全不告警＝把真問題吞掉，也不可接受"
    assert len(sent) <= 2, f"4.8 小時內推播 {len(sent)} 則＝仍在洗版（會埋掉 401 恢復告警）"


def test_cleanup_branch_does_not_flood_telegram(rig, monkeypatch):
    """「已清理」是自癒成功的例行事件，同樣不該每 12 分報一次。"""
    clock, sent, _ = rig
    monkeypatch.setattr(wd, "_commit_pct", lambda: 95.0)
    monkeypatch.setattr(wd, "_stale_claude_runners", lambda: [(4242, 99_999.0)])

    _run_rounds(clock, 24)

    assert len(sent) <= 2, f"4.8 小時內推播 {len(sent)} 則例行自癒通知＝洗版"


def test_local_log_still_records_every_round(rig, monkeypatch):
    """節流的是推播，不是鑑識軌跡：本機 log 每一輪都要留。"""
    clock, _sent, _ = rig
    monkeypatch.setattr(wd, "_commit_pct", lambda: 95.0)
    monkeypatch.setattr(wd, "_stale_claude_runners", lambda: [])

    _run_rounds(clock, 10)

    lines = [ln for ln in wd.WLOG.read_text(encoding="utf-8").splitlines()
             if "[memguard]" in ln]
    assert len(lines) == 10, f"本機 log 只剩 {len(lines)}/10 行＝把證據也一起砍了"


def test_escalation_pierces_cooldown(rig, monkeypatch):
    """情況惡化要能穿透冷卻——否則節流會變成新的失明。"""
    clock, sent, _ = rig
    pct = {"v": 89.0}
    monkeypatch.setattr(wd, "_commit_pct", lambda: pct["v"])
    monkeypatch.setattr(wd, "_stale_claude_runners", lambda: [])

    wd.memory_guard()                 # 首則：一定送
    assert len(sent) == 1
    clock.advance(720)

    wd.memory_guard()                 # 同水位、冷卻內：不送
    assert len(sent) == 1
    clock.advance(720)

    pct["v"] = 97.0                   # 惡化 +8 個百分點：必須送
    wd.memory_guard()
    assert len(sent) == 2, "commit 從 89% 惡化到 97% 仍被冷卻吃掉＝節流做成了失明"


def test_suppressed_count_is_disclosed(rig, monkeypatch):
    """被壓下的則數必須在下次送出時揭露，永不無聲吞掉。"""
    clock, sent, _ = rig
    pct = {"v": 89.0}
    monkeypatch.setattr(wd, "_commit_pct", lambda: pct["v"])
    monkeypatch.setattr(wd, "_stale_claude_runners", lambda: [])

    wd.memory_guard()
    clock.advance(720)
    for _ in range(5):                # 5 輪被壓下
        wd.memory_guard()
        clock.advance(720)
    pct["v"] = 97.0                   # 惡化穿透 → 這一則要帶出「另有 5 則」
    wd.memory_guard()

    assert len(sent) == 2
    assert "5" in sent[-1], f"未揭露被壓下的則數：{sent[-1]!r}"


def test_kill_cadence_is_not_weakened(rig, monkeypatch):
    """⛔ 防衛本體不可被節流波及：清理仍照 MEM_COOLDOWN_SEC(600s) 每輪動手。"""
    clock, _sent, kills = rig
    monkeypatch.setattr(wd, "_commit_pct", lambda: 95.0)
    monkeypatch.setattr(wd, "_stale_claude_runners", lambda: [(4242, 99_999.0)])

    _run_rounds(clock, 6, spacing_sec=720.0)   # 720s > 600s 冷卻 → 每輪都該清

    taskkills = [c for c in kills if c and c[0] == "taskkill"]
    assert len(taskkills) == 6, (
        f"6 輪只清了 {len(taskkills)} 次＝告警節流誤傷了記憶體防衛本體")


def test_cooldown_expiry_resumes_alerting(rig, monkeypatch):
    """冷卻到期後要恢復告警——問題持續存在就得持續被看見（只是別每 12 分一次）。"""
    clock, sent, _ = rig
    monkeypatch.setattr(wd, "_commit_pct", lambda: 95.0)
    monkeypatch.setattr(wd, "_stale_claude_runners", lambda: [])

    wd.memory_guard()
    assert len(sent) == 1
    clock.advance(wd.MEM_ALERT_COOLDOWN_SEC + 60)
    wd.memory_guard()
    assert len(sent) == 2, "冷卻到期後仍不出聲＝問題永久靜音"
