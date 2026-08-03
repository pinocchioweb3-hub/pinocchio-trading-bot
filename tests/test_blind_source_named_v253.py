# -*- coding: utf-8 -*-
"""v253：訊號乾旱判「失明」時，必須講出**是哪一個資料源**瞎了。

事故（2026-08-03 23:0x 監督員實測）：帳本 system_faults 連日寫
「訊號引擎已 632h 零產出，最近一輪有 1 檔是因為讀不到資料才 HOLD
（btc_gate_stale／filter_stale 類）」。族群名不是源名——真正的鍵是
`filter_stale:trend_4h`，但沒有任何一層印出來。使用者看得到「有東西瞎了」，
卻沒有任何一條路徑能知道要去修哪一個源，等於報了一個無法行動的事實。

同輪的 gated/other 分佈也被這個分支整個吃掉（counts 有值、文字裡沒有），
所以「1 檔失明」讀起來像是全場只有 1 檔沒過，實際是 15 檔全 HOLD。

改動前的碼會失敗在哪（非虛設檢定的證明）：
  * `blind_detail` 函式不存在 → ImportError，本檔全紅。
  * 舊 blind 分支文字裡沒有 "filter_stale:trend_4h" 這個鍵，也沒有 gated/other
    的檔數 → test_blind_text_names_the_source / test_blind_text_keeps_the_rest 紅。
"""
from __future__ import annotations

from l3_dispatcher.scan_activity import blind_detail, drought_verdict

NOW = 1_785_769_000.0
DAY = 86400.0

# 2026-08-03 23:0x 的真實活動檔內容（scan_activity.json 原樣）
REAL_ACTIVITY = {
    "ts": NOW - 500,
    "first_seen_ts": NOW - 33000,
    "scanned": 15,
    "fires_enqueued": 0,
    "fires_in_cooldown": 0,
    "fires_blocked_check": 0,
    "check_block_reasons": {},
    "holds": 15,
    "hold_reasons": {
        "btc_gate_closed": 7,
        "filter_stale:trend_4h": 1,
        "oi_fuel_insufficient": 2,
        "votes_insufficient: bull=0 bear=0 stale=0 need>=1": 3,
        "filter_failed:trend_4h": 2,
    },
    "errors": 0,
    "btc_gate_open": False,
    "btc_gate_stale": False,
    "btc_gate_source": "risk_off(binance_200ma備援)",
}


def test_blind_detail_only_picks_blind_keys():
    d = blind_detail(REAL_ACTIVITY["hold_reasons"])
    assert "filter_stale:trend_4h" in d
    # ⛔ filter_failed 是「讀到了但沒過」，與 filter_stale「讀不到」是兩件事，
    #    不可混進失明清單。
    assert "filter_failed" not in d
    assert "btc_gate_closed" not in d


def test_blind_detail_sorts_by_count_and_survives_junk():
    d = blind_detail({"filter_stale:a": 1, "oi_fuel_stale": 9, "btc_gate_stale": "壞值"})
    assert d.startswith("oi_fuel_stale×9")
    assert "btc_gate_stale" not in d      # 數不出來的不假裝數得出來


def test_blind_detail_empty_is_empty_string_not_a_lie():
    assert blind_detail(None) == ""
    assert blind_detail({"btc_gate_closed": 7}) == ""
    assert blind_detail({}) == ""


def test_blind_text_names_the_source():
    v = drought_verdict(REAL_ACTIVITY, NOW - 26 * DAY, now_s=NOW)
    assert v is not None
    assert v["cls"] == "blind"
    assert v["fault"] is True
    # 核心斷言：文字裡要有可行動的源名，不是只有族群名
    assert "filter_stale:trend_4h" in v["text"]


def test_blind_text_keeps_the_rest_of_the_distribution():
    """「1 檔失明」不可讀起來像全場只有 1 檔沒過——同輪 15 檔全 HOLD。"""
    v = drought_verdict(REAL_ACTIVITY, NOW - 26 * DAY, now_s=NOW)
    assert "7" in v["text"] and "15" in v["text"]
    assert v["counts"] == {"blind": 1, "gated": 7, "other": 7, "total": 15}


def test_blind_without_recorded_keys_says_so_loudly():
    """活動檔沒留鍵時要明講「未記錄」，⛔ 不可靜默省略。"""
    act = dict(REAL_ACTIVITY)
    # counts 仍算得出 blind（靠 classify_holds），但鍵值是壞的 → detail 空
    act["hold_reasons"] = {"filter_stale:x": 1}
    v = drought_verdict(act, NOW - 26 * DAY, now_s=NOW)
    assert "filter_stale:x" in v["text"]

    act2 = dict(REAL_ACTIVITY)
    act2["hold_reasons"] = {"btc_gate_closed": 7}
    v2 = drought_verdict(act2, NOW - 26 * DAY, now_s=NOW)
    assert v2["cls"] == "gated"          # 沒有失明就不該走 blind 分支
