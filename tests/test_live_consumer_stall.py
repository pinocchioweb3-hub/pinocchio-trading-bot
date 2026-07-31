# -*- coding: utf-8 -*-
"""r74/v176：真錢消費器停擺時，帳本上唯一那條真錢阻塞會**自己消失**。

實測（2026-08-01 01:13–01:40 台北）
----------------------------------
例行盤點時發現 `atk_consumer_live_health.json` 的 consecutive_fail_rounds 卡在 1260
不再增加。查排程：ATKLiveConsumer 的 LastRunTime 凍結在 01:13:01、NextRunTime 每分鐘
往前跳、NumberOfMissedRuns 持續累加（15→16→…）＝每一分鐘的觸發都被略過。
再查電源：GetSystemPowerStatus 回報未接外部電源，而該排程設了
DisallowStartIfOnBatteries=True ⇒ 機器一改吃電池，真錢消費器就整個停跑。

為什麼這比「暫時不跑」嚴重
--------------------------
`live_exec_verdict()` 有一道 900 秒新鮮度閘，舊檔一律回 None。那個理由是對的
（不可拿昨天的 streak 當今天的阻塞），但**輸出**是錯的：停擺被渲染成跟一切正常
一模一樣的空白。實測當下健康檔已 1074 秒沒更新 ⇒ 下一次帳本寫入，401 阻塞就會從
blockers 裡消失，而執行器一筆訊號都送不出去。任何人（包括下一輪的監督員）看到
「沒有真錢阻塞」都會讀成痊癒——這正是 r33 假痊癒的形狀換一個入口，也是
「未知 vs 確認沒有」在真錢路徑上的第七處。

⛔ 反方向的坑（本檔的反向護欄釘住）
----------------------------------
健康檔**不存在**不可報停擺——那是「未知」（可能根本還沒部署過執行器），憑空報
會變成慢性假警報。同理，量不到電源狀態不可折成「有接電」。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from l3_dispatcher.ceo_oversight import (  # noqa: E402
    LIVE_HEALTH_MAX_AGE_SEC, ac_power_online, assess, live_exec_verdict,
    live_stall_verdict,
)

NOW = 1_785_000_000.0
FRESH = {"consecutive_fail_rounds": 1260, "last_fail_class": "auth_ip_whitelist",
         "updated_at": NOW - 60}
STALE = {"consecutive_fail_rounds": 1260, "last_fail_class": "auth_ip_whitelist",
         "updated_at": NOW - 1800}


def _assess(**kw):
    """把不相干的參數固定住，只留本檔在測的那幾個。"""
    base = dict(now_ms=NOW * 1000, commit_age_sec=60, paper_n=360, paper_min=100,
                live_n=0, live_min=30, demo_n=31, demo_live=1, demo_active=False,
                open_decisions=0, pending_outbox=0)
    base.update(kw)
    return assess(**base)


# --------------------------------------------------------------------------
# 1. 核心：停擺不是痊癒
# --------------------------------------------------------------------------
def test_stale_health_is_reported_as_a_stopped_consumer():
    v = live_stall_verdict(STALE, now_s=NOW, ac_online=False)
    assert v is not None
    assert "沒在跑" in v["text"]
    assert v["stale_sec"] == 1800
    assert v["last_rounds"] == 1260


def test_the_defect_stale_health_used_to_leave_the_ledger_completely_silent():
    """差分回歸鎖：舊行為＝健康檔一舊，真錢阻塞從帳本上整個消失。

    live_exec_verdict 對舊檔回 None 是**刻意**的（不可拿舊 streak 當現況），本輪
    沒有動它；補的是「那一刻該說的話」。有人若把 live_stall 拿掉，這條會紅。
    """
    assert live_exec_verdict(STALE, now_s=NOW) is None          # 舊行為（保留）
    snap_old = _assess(live_exec=None, live_stall=None)
    assert snap_old["blockers"] == [] and snap_old["system_faults"] == []

    snap_new = _assess(live_exec=None,
                       live_stall=live_stall_verdict(STALE, now_s=NOW, ac_online=False))
    assert snap_new["blockers"], "停擺必須留下出口，不可跟『一切正常』長得一樣"


def test_fresh_health_does_not_double_report():
    """兩者互斥：夠新鮮由 live_exec 報，太舊由 live_stall 報，永不同時。"""
    assert live_stall_verdict(FRESH, now_s=NOW, ac_online=False) is None
    assert live_exec_verdict(FRESH, now_s=NOW) is not None


def test_boundary_is_the_same_threshold_so_there_is_no_silent_seam():
    """⛔ 兩道閘共用同一個門檻——差一秒都不可以兩邊都不報。"""
    on_edge = {**FRESH, "updated_at": NOW - LIVE_HEALTH_MAX_AGE_SEC}
    just_over = {**FRESH, "updated_at": NOW - LIVE_HEALTH_MAX_AGE_SEC - 1}
    assert live_exec_verdict(on_edge, now_s=NOW) is not None
    assert live_stall_verdict(on_edge, now_s=NOW) is None
    assert live_exec_verdict(just_over, now_s=NOW) is None
    assert live_stall_verdict(just_over, now_s=NOW) is not None


# --------------------------------------------------------------------------
# 2. 歸屬：誰的球
# --------------------------------------------------------------------------
def test_no_ac_power_is_the_users_ball():
    v = live_stall_verdict(STALE, now_s=NOW, ac_online=False)
    assert v["user_actionable"] is True
    assert "未接外部電源" in v["text"]
    assert _assess(live_stall=v)["state"] == "BLOCKED_ON_USER"


def test_on_ac_power_means_look_elsewhere_and_pushes_the_ceo():
    v = live_stall_verdict(STALE, now_s=NOW, ac_online=True)
    assert v["user_actionable"] is False
    assert "須查排程" in v["text"]
    s = _assess(live_stall=v)
    assert s["system_faults"] and s["state"] == "STALLED"


def test_unknown_power_state_is_not_folded_into_powered():
    """⛔ 量不到不可當成有電——那是拿代理值當事實的同一物種。"""
    v = live_stall_verdict(STALE, now_s=NOW, ac_online=None)
    assert v["user_actionable"] is False
    assert "量不到" in v["text"] and "不等於有接電" in v["text"]


# --------------------------------------------------------------------------
# 3. ⛔ 反向護欄：不可製造假警報
# --------------------------------------------------------------------------
def test_missing_health_file_is_unknown_not_stalled():
    for absent in (None, {}, 0, ""):
        assert live_stall_verdict(absent, now_s=NOW) is None


def test_no_timestamp_is_reported_as_unprovable_not_as_a_number():
    v = live_stall_verdict({"consecutive_fail_rounds": 5}, now_s=NOW)
    assert v is not None and v["stale_sec"] is None
    assert "證明不了" in v["text"]


def test_junk_values_never_raise():
    for junk in ({"updated_at": "abc"}, {"updated_at": None, "consecutive_fail_rounds": "x"},
                 {"updated_at": NOW - 9999, "last_fail_class": None}):
        assert live_stall_verdict(junk, now_s=NOW) is not None


def test_ac_power_online_returns_a_tri_state_and_never_raises():
    assert ac_power_online() in (True, False, None)


# --------------------------------------------------------------------------
# 4. 不可弄壞的既有行為
# --------------------------------------------------------------------------
def test_live_exec_verdict_behaviour_unchanged():
    v = live_exec_verdict(FRESH, now_s=NOW)
    assert v["rounds"] == 1260 and v["user_actionable"] is True
    assert live_exec_verdict({**FRESH, "consecutive_fail_rounds": 1}, now_s=NOW) is None


def test_field_always_present_in_verdict():
    """r71 的口徑沿用：欄位恆存在，Layer 2 才分得出『沒停擺』與『這版沒這功能』。"""
    assert "live_stall" in _assess()
    assert _assess()["live_stall"] is None
