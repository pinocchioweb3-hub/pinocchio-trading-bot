# -*- coding: utf-8 -*-
"""watchdog 暫停開關的**射程**與**可見性** —— 迴圈級（main()）測試。

線上實證（2026-08-03 12:09）：
    `%LOCALAPPDATA%\\TradingBot\\watchdog.disabled` 這個檔在 12:09:07 出現。
    全 repo 沒有任何程式會建立它（只有 watchdog.py 讀它）⇒ 是人手動建的。
    它一存在，main() 第一件事就是 `return 0`——於是同時發生兩件事：

      ① daemon 不再自動重啟（這是開關**寫明**要做的）
      ② memory_guard 完全不跑（這是開關**沒說**、也沒人知道的副作用）

    ②的下場當天就量得到：memguard 最後一次清掃停在 08:37（commit 67%），
    12:09 旗標出現後歸零；12:29 實測 11 個 runner 堆積（最老 34.8 小時）、
    commit 爬到 71.4%、可用實體記憶體只剩 1.16GB。而這段期間系統對外
    表現得完全正常——`[skip]` 只寫進沒人會讀的本機 watchdog.log。

    同物種（保護機制被關掉 ≠ 有人知道它被關掉）。treat：
      - 開關的語意收斂回它自己的文件：「不要自動重啟 daemon」。記憶體防衛
        清的是 Claude 排程殭屍，與 daemon 重不重啟無關 ⇒ 不得被一起關掉。
      - 「目前處於暫停狀態」必須推播出來（節流），不能只躺在本機 log。

⛔ 邊界（本測試一併釘死）：
    - watchdog **永不自己刪除**這個旗標。使用者刻意關掉的 bot 被自動拉回來，
      比旗標留著更糟。到期自動失效＝同一個錯誤換方向犯。
    - 暫停時仍然**不重啟** daemon。這條沒有變，改動不得弱化它。
    - 告警不得斷言「是你設的」——沒有任何證據指向誰建的，只能附上時間點。
"""
from __future__ import annotations

import os
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

    def localtime(self, *a):
        return _real_time.localtime(*a)

    def advance(self, sec: float) -> None:
        self.now += sec


@pytest.fixture()
def rig(tmp_path, monkeypatch):
    """把 main() 整條架在暫存目錄上。

    預設情境＝「daemon 看起來死了」（無 liveness 檔＋行程探測回 False），
    也就是**唯一**會讓暫停開關真正起作用的情境。
    """
    clock = _FakeClock(1_700_000_000.0)
    sent: list[str] = []
    calls: list[str] = []

    monkeypatch.setattr(wd, "time", clock)
    monkeypatch.setattr(wd, "DISABLED_FLAG", tmp_path / "watchdog.disabled")
    monkeypatch.setattr(wd, "STATE", tmp_path / "watchdog_state.json")
    monkeypatch.setattr(wd, "WLOG", tmp_path / "watchdog.log")
    monkeypatch.setattr(wd, "LIVENESS", tmp_path / "liveness.json")
    monkeypatch.setattr(wd, "telegram_alert", lambda t: sent.append(t))
    monkeypatch.setattr(wd, "memory_guard", lambda: calls.append("memguard"))
    monkeypatch.setattr(wd, "restart_daemon",
                        lambda: (calls.append("restart"), True)[1])
    monkeypatch.setattr(wd, "daemon_process_alive", lambda: False)
    return clock, sent, calls, tmp_path


def _pause(tmp_path, clock, age_sec: float = 7200.0) -> None:
    """建立暫停旗標，並把「它出現的時間」設在 age_sec 秒前。

    預設 2 小時＝已經超過部署窗、屬於「被留下來」那一類。
    """
    p = tmp_path / "watchdog.disabled"
    p.write_text("", encoding="utf-8")
    t = clock.now - age_sec
    os.utime(p, (t, t))


def _pause_alerts(sent: list[str]) -> list[str]:
    return [t for t in sent if "watchdog.disabled" in t]


# ── ① 射程：暫停「重啟」，不等於暫停「記憶體防衛」──────────────────────


def test_pause_does_not_disable_memory_guard(rig):
    """改動前：main() 在旗標檢查就 return，memguard 一次都不會被呼叫（此條必紅）。"""
    _clock, _sent, calls, tmp_path = rig
    _pause(tmp_path, _clock)

    wd.main()

    assert "memguard" in calls, (
        "暫停開關把記憶體防衛也一起關掉了——線上實證 commit 因此 67%→71.4%")


def test_pause_still_blocks_restart(rig):
    """⛔ 這條不可因為上一條而鬆動：暫停期間仍然不准重啟 daemon。"""
    _clock, _sent, calls, tmp_path = rig
    _pause(tmp_path, _clock)

    wd.main()

    assert "restart" not in calls, "暫停開關失效＝使用者刻意關掉的 bot 又被拉回來"


def test_without_flag_restart_still_happens(rig):
    """對照組：沒有旗標時，該重啟的還是要重啟（確認上面不是靠癱瘓主流程過關）。"""
    _clock, _sent, calls, _tmp = rig

    wd.main()

    assert "restart" in calls
    assert "memguard" in calls


# ── ② 可見性：暫停狀態必須推播，不能只躺在本機 log ─────────────────────


def test_pause_state_is_pushed_not_only_logged(rig):
    """改動前：只有 `log()` 一行，Telegram 零則（此條必紅）。"""
    _clock, sent, _calls, tmp_path = rig
    _pause(tmp_path, _clock)

    wd.main()

    assert _pause_alerts(sent), (
        "自動重啟被關掉這件事只寫進本機 log＝等於沒人知道；2026-08-01 那次就這樣躺了整天")


def test_pause_alert_says_when_the_flag_appeared(rig):
    """沒有證據指向誰建的，但「什麼時候出現的」是查得到的事實，必須附上。"""
    _clock, sent, _calls, tmp_path = rig
    _pause(tmp_path, _clock)

    wd.main()

    text = _pause_alerts(sent)[0]
    stamp = _real_time.strftime(
        "%Y-%m-%d %H:%M",
        _real_time.localtime((tmp_path / "watchdog.disabled").stat().st_mtime))
    assert stamp in text, f"未附旗標出現時間，無從判斷是不是自己設的：{text!r}"


def test_pause_alert_states_what_is_still_running(rig):
    """⛔ 不可讓人以為「watchdog 全停了」：要講明記憶體防衛仍在跑。"""
    _clock, sent, _calls, tmp_path = rig
    _pause(tmp_path, _clock)

    wd.main()

    text = _pause_alerts(sent)[0]
    assert "記憶體" in text, f"沒交代哪些防護還活著，讀的人會過度恐慌或過度放心：{text!r}"


def test_pause_alert_does_not_assert_who_created_it(rig):
    """沒有任何證據指向建立者 ⇒ 只能用條件句，並給出恢復方法。"""
    _clock, sent, _calls, tmp_path = rig
    _pause(tmp_path, _clock)

    wd.main()

    text = _pause_alerts(sent)[0]
    assert "若" in text, f"對成因下了斷言而非條件句：{text!r}"
    assert "刪" in text, f"沒告訴使用者怎麼恢復自動重啟：{text!r}"


# ── ③ 節流：每 3 分鐘一則會把真告警洗掉（memguard 已有的同一課）─────────


def test_pause_alert_is_throttled(rig):
    """watchdog 每 3 分觸發一次；暫停狀態不可每輪推一則。"""
    clock, sent, _calls, tmp_path = rig
    _pause(tmp_path, clock)

    wd.main()
    assert len(_pause_alerts(sent)) == 1, "第一則就該送（此條在改動前必紅）"

    for _ in range(20):               # 再跑 1 小時（每 3 分一輪）
        clock.advance(180)
        wd.main()

    assert len(_pause_alerts(sent)) == 1, (
        f"1 小時內推了 {len(_pause_alerts(sent))} 則＝洗版，會埋掉真錢路徑的告警")


def test_pause_alert_resumes_after_cooldown(rig):
    """問題持續存在就要持續被看見——只是別每 3 分一次。"""
    clock, sent, _calls, tmp_path = rig
    _pause(tmp_path, clock)

    wd.main()
    clock.advance(wd.PAUSE_ALERT_COOLDOWN_SEC + 60)
    wd.main()

    assert len(_pause_alerts(sent)) == 2, "冷卻到期仍不出聲＝暫停狀態永久靜音"


def test_no_pause_alert_when_flag_absent(rig):
    """沒暫停就別吵。"""
    _clock, sent, _calls, _tmp = rig

    wd.main()

    assert not _pause_alerts(sent)


# ── ③b 寬限期：部署窗是正常流程，每次都推＝把人訓練成忽略它 ─────────────


def test_fresh_flag_during_deploy_window_does_not_alert(rig):
    """部署窗（建旗標→重啟→驗證→移除）本來就會讓旗標存在數分鐘。

    每次部署推一則＝使用者學會略過這則告警，那正是我們要治的「失明」本身。
    r132 的部署窗實測 12:09:07→12:24 之間，約 15 分鐘。
    """
    _clock, sent, _calls, tmp_path = rig
    _pause(tmp_path, _clock, age_sec=300.0)      # 出現 5 分鐘＝部署進行中

    wd.main()

    assert not _pause_alerts(sent), (
        f"部署窗內就推播＝每次部署都吵一次，告警很快會被無視：{sent!r}")


def test_flag_left_behind_past_grace_does_alert(rig):
    """真正要治的是「留下來了」：08-01 11:13→08-02 02:13 那次留了約 15 小時。"""
    _clock, sent, _calls, tmp_path = rig
    _pause(tmp_path, _clock, age_sec=wd.PAUSE_ALERT_GRACE_SEC + 60)

    wd.main()

    assert _pause_alerts(sent), "超過寬限期仍靜音＝回到「事後才被日報回溯發現」的老路"


def test_memory_guard_runs_even_during_short_deploy_window(rig):
    """⛔ 寬限期只管「推不推播」，不得順手把記憶體防衛也一起延後。"""
    _clock, _sent, calls, tmp_path = rig
    _pause(tmp_path, _clock, age_sec=60.0)

    wd.main()

    assert "memguard" in calls


def test_unknown_flag_age_still_alerts(rig, monkeypatch):
    """⛔ 「讀不到出現時間」不得折成「剛建的，別吵」——那正是本專案的慣犯物種。"""
    _clock, sent, _calls, tmp_path = rig
    _pause(tmp_path, _clock, age_sec=60.0)       # 若讀得到 mtime，這個年齡會被靜音

    real_stat = type(wd.DISABLED_FLAG).stat

    def _boom(self, *a, **kw):
        if self == wd.DISABLED_FLAG:
            raise OSError("stat 失敗")
        return real_stat(self, *a, **kw)

    monkeypatch.setattr(type(wd.DISABLED_FLAG), "stat", _boom)

    wd.main()

    text = _pause_alerts(sent)
    assert text, "旗標年齡未知就當成剛建的＝把「量不到」折成「沒問題」"
    assert "未知" in text[0], f"未如實標示時間讀不到：{text[0]!r}"


# ── ④ 邊界：watchdog 永不自己動這個旗標 ────────────────────────────────


def test_watchdog_never_deletes_the_flag(rig):
    """⛔ 自動失效＝把「使用者刻意關掉的 bot」自動拉回來，比旗標留著更糟。"""
    clock, _sent, _calls, tmp_path = rig
    _pause(tmp_path, clock)

    for _ in range(30):
        wd.main()
        clock.advance(180)

    assert (tmp_path / "watchdog.disabled").exists(), "watchdog 自己刪掉了使用者的開關"


def test_local_log_still_records_every_round(rig):
    """節流的是推播，不是鑑識軌跡：本機 log 每輪照寫。"""
    clock, _sent, _calls, tmp_path = rig
    _pause(tmp_path, clock)

    for _ in range(10):
        wd.main()
        clock.advance(180)

    lines = [ln for ln in wd.WLOG.read_text(encoding="utf-8").splitlines()
             if "[skip]" in ln]
    assert len(lines) == 10, f"本機 log 只剩 {len(lines)}/10 行＝證據被節流砍掉了"
