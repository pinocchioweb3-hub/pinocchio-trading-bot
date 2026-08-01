"""v195（監督員 r89）：watchdog 自己的狀態檔「存在但讀不出來」不再折成「第一次啟動」。

同物種第 15 次。這一處特別要緊的理由：watchdog_state.json 一旦壞掉，舊碼的
read_json() 回 {}，於是

    last_restart = state.get("last_restart_ts", 0)  → 0    ⇒ 暖機冷卻窗直接跳過
    restarts     = state.get("restart_times", [])   → []   ⇒ 1 小時 5 次的煞車失效

也就是「防無限重啟」這道**最後的人工介入閘**會在無人知曉的情況下被解除，
而 watchdog 自己的 write_state() 是非原子寫（write_text）＝它有能力親手做出
那個半截檔（斷電事故在本機是有前例的）——自製壞檔再自誤讀，與 v157/v162-v166
同一根因。

⛔ 反向護欄同樣重要：檔案**不存在**是合法的第一次啟動，必須維持安靜，
否則每台新機器第一次跑都會收到一則假告警。
"""
from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture()
def wd(tmp_path, monkeypatch):
    """載入 watchdog 並把資料目錄指到 tmp（絕不碰真的 %LOCALAPPDATA%\\TradingBot）。"""
    monkeypatch.setenv("TRADINGBOT_DATA_DIR", str(tmp_path))
    sys.modules.pop("watchdog", None)
    mod = importlib.import_module("watchdog")
    importlib.reload(mod)
    assert mod.DATA_DIR == tmp_path, "測試必須跑在 tmp 目錄上"
    return mod


# --- 反向護欄：檔不存在＝真的第一次，不可報成故障 ---

def test_missing_state_is_first_launch_not_fault(wd):
    state, err = wd.read_state()
    assert state == {}
    assert err == "missing", "檔不存在必須是可辨識的『未知/第一次』，不是故障"


def test_valid_state_reads_clean(wd):
    wd.STATE.write_text(json.dumps({"last_restart_ts": 123.0}), encoding="utf-8")
    state, err = wd.read_state()
    assert err is None
    assert state["last_restart_ts"] == 123.0


# --- 本體：檔在、卻讀不出來 ---

def test_corrupt_state_is_not_folded_into_empty(wd):
    wd.STATE.write_text('{"last_restart_ts": 12', encoding="utf-8")   # 半截檔（斷電典型）
    state, err = wd.read_state()
    assert err not in (None, "missing"), "半截 JSON 必須被辨識為故障，⛔ 不可折成 {}"
    assert state == {}


def test_state_written_as_list_is_flagged(wd):
    wd.STATE.write_text("[1, 2, 3]", encoding="utf-8")               # 合法 JSON、非 dict
    state, err = wd.read_state()
    assert err == "NotADict"
    assert state == {}


# --- 煞車：狀態檔壞掉時，重啟預算不可被讀成「這小時零次」 ---

def test_corrupt_state_recovery_quarantines_and_rebuilds(wd, monkeypatch):
    alerts: list[str] = []
    monkeypatch.setattr(wd, "telegram_alert", lambda t: alerts.append(t))
    wd.STATE.write_text('{"restart_times": [1, 2, 3', encoding="utf-8")

    out = wd.recover_corrupt_state("JSONDecodeError", now=1_000_000.0)

    assert out == {}, "重建成功應回可用的空狀態"
    assert alerts, "⛔ 靜默重建等於把煞車失效藏起來：必須出聲"
    quarantined = list(wd.DATA_DIR.glob("watchdog_state.corrupt-*.json"))
    assert len(quarantined) == 1, "壞檔要留存供事後查證，不可直接覆蓋掉"
    assert wd.STATE.exists() and wd.read_state()[1] is None, "重建後狀態檔必須可讀"


def test_recovery_failure_is_fail_closed(wd, monkeypatch):
    """連重建都失敗＝重啟次數永遠無法累計＝煞車永久失效 ⇒ 本輪不可自動重啟。"""
    monkeypatch.setattr(wd, "telegram_alert", lambda t: None)
    monkeypatch.setattr(wd, "write_state", lambda state: False)      # 寫入持續失敗
    wd.STATE.write_text("{{{", encoding="utf-8")

    out = wd.recover_corrupt_state("JSONDecodeError", now=1_000_000.0)

    assert out is None, "寫不回去時必須回 None（呼叫端據此放棄本輪自動重啟）"


def test_main_skips_restart_when_state_unrecoverable(wd, monkeypatch):
    """整合：狀態檔壞掉且修不好時，main() 不得呼叫 restart_daemon。"""
    called: list[int] = []
    monkeypatch.setattr(wd, "telegram_alert", lambda t: None)
    monkeypatch.setattr(wd, "memory_guard", lambda: None)
    monkeypatch.setattr(wd, "restart_daemon", lambda: called.append(1) or True)
    monkeypatch.setattr(wd, "daemon_process_alive", lambda: False)   # 行程已死＝平常必重啟
    monkeypatch.setattr(wd, "write_state", lambda state: False)
    wd.STATE.write_text("{{{", encoding="utf-8")
    wd.LIVENESS.write_text(json.dumps({"ts": 0}), encoding="utf-8")

    rc = wd.main()

    assert called == [], "⛔ 煞車狀態未知時自動重啟＝可能無限重啟且無人得知"
    assert rc != 0, "應以非零碼收場，讓排程紀錄看得見這輪沒做事"


# --- 根因：非原子寫（自製壞檔的來源） ---

def test_write_state_is_atomic_and_reports_success(wd):
    ok = wd.write_state({"last_restart_ts": 5.0})
    assert ok is True, "write_state 必須回報成敗，呼叫端才可能 fail-closed"
    assert json.loads(wd.STATE.read_text(encoding="utf-8"))["last_restart_ts"] == 5.0
    assert not list(wd.DATA_DIR.glob("*.tmp")), "暫存檔必須已被 rename 掉，不可留下"
