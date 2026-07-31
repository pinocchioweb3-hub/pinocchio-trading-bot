# -*- coding: utf-8 -*-
"""v177 電力哨兵：斷電前要有人被告知，且不得誤報／洗版。

改動前這支測試會直接 ImportError（l3_dispatcher/power_watch.py 不存在）＝
「機器快沒電」這件事當時只以 ceo_oversight.ac_power_online() 的代理值存在、
沒有任何一條路徑會主動告訴人。
"""
import pytest

from l3_dispatcher.power_watch import build_message, next_alerts


def test_unknown_ac_is_silent_and_does_not_touch_state():
    """⛔ 量不到＝未知：不發告警，也不得推翻既有狀態（未知不是 False 也不是 True）。"""
    prev = {"ac": False, "fired": ["unplugged"]}
    kinds, new = next_alerts(prev, {"ac": None, "pct": None, "secs_left": None})
    assert kinds == []
    assert new == prev


def test_unplug_fires_once_then_stays_quiet():
    kinds, st = next_alerts({}, {"ac": False, "pct": 80})
    assert kinds == ["unplugged"]
    kinds2, st2 = next_alerts(st, {"ac": False, "pct": 79})
    assert kinds2 == []
    assert st2["fired"] == ["unplugged"]


def test_low_then_critical_each_fire_once_in_order():
    _, st = next_alerts({}, {"ac": False, "pct": 80})
    kinds, st = next_alerts(st, {"ac": False, "pct": 25})
    assert kinds == ["low"]
    kinds, st = next_alerts(st, {"ac": False, "pct": 20})
    assert kinds == []
    kinds, st = next_alerts(st, {"ac": False, "pct": 9})
    assert kinds == ["critical"]
    kinds, st = next_alerts(st, {"ac": False, "pct": 5})
    assert kinds == []


def test_straight_to_critical_does_not_backfill_low():
    """一口氣掉到危急：只發 critical，不補發已無意義的 low。"""
    kinds, st = next_alerts({}, {"ac": False, "pct": 8})
    assert kinds == ["unplugged", "critical"]
    kinds, _ = next_alerts(st, {"ac": False, "pct": 7})
    assert kinds == []


def test_replug_reports_restored_and_rearms():
    _, st = next_alerts({}, {"ac": False, "pct": 20})
    kinds, st = next_alerts(st, {"ac": True, "pct": 21})
    assert kinds == ["restored"]
    assert st["fired"] == []
    kinds, _ = next_alerts(st, {"ac": False, "pct": 21})   # 再拔電＝重新武裝
    assert "unplugged" in kinds


def test_replug_without_prior_alert_is_silent():
    kinds, _ = next_alerts({"ac": True, "fired": []}, {"ac": True, "pct": 100})
    assert kinds == []


def test_unknown_pct_only_reports_unplug_never_guesses_level():
    kinds, st = next_alerts({}, {"ac": False, "pct": None})
    assert kinds == ["unplugged"]
    assert "low" not in st["fired"] and "critical" not in st["fired"]


@pytest.mark.parametrize("kind", ["unplugged", "low", "critical"])
def test_message_states_what_actually_stops(kind):
    msg = build_message(kind, {"ac": False, "pct": 12, "secs_left": 900})
    assert "零送單" in msg          # 現在就已經送不出單，不是等關機才開始
    assert "watchdog" in msg        # 關機後 watchdog 救不回
    assert "插上電源" in msg


def test_message_never_folds_unreadable_positions_into_zero(monkeypatch):
    """讀不到部位檔要說「未知」——⛔ 折成 0 筆就是本專案重犯多次的同一物種。"""
    import l3_dispatcher.power_watch as pw

    monkeypatch.setattr(pw, "data_dir", lambda: __import__("pathlib").Path("/nonexistent-xyz"))
    msg = pw.build_message("critical", {"ac": False, "pct": 5, "secs_left": 300})
    assert "未知" in msg
    assert "0 筆" not in msg
