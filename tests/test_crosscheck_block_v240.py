# -*- coding: utf-8 -*-
"""v240：「被 cross-check 擋下」不得與「還在冷卻中」共用一個計數器。

承 v239。修完訊號乾旱的分類器之後，回頭看它的輸入端才發現隔壁還有一格折疊：

    scheduler.py  if not chk.pass_:  ...  summary.fires_in_cooldown += 1   # 視同被擋

「規則量到東西、把這筆單否決了」和「這檔剛出過單、還在冷卻」是完全不同的兩件事，
但它們加在同一個數字上。下場：真的有 FIRE、卻每一筆都被 cross-check 擋掉時，
v239 的乾旱偵測器只看得到 hold_reasons，會指著一個**不相干**的濾網說
「gated、濾網在做事」——成因歸錯人，而且錯得很有說服力。

改動前的碼會失敗在哪（非虛設檢定的證明）：
  * ScanSummary 沒有 fires_blocked_check / check_block_reasons → AttributeError
  * drought_verdict 看不到這兩個欄位 → 零 hold 那格仍回 unknown「一筆 HOLD 都沒記到」，
    而不是指名「N 筆 FIRE 被 cross-check 擋下」
"""
from __future__ import annotations

from l3_dispatcher.scan_activity import drought_verdict
from l3_dispatcher.scheduler import ScanSummary

DAY = 86400.0
NOW = 1_754_000_000.0


def _act(**kw) -> dict:
    base = {"ts": NOW - 600, "first_seen_ts": NOW - 30 * DAY,
            "hold_reasons": {}, "btc_gate_open": None}
    base.update(kw)
    return base


# ─────────────── ① 兩個計數器不得共用 ───────────────

def test_scan_summary_separates_check_block_from_cooldown():
    s = ScanSummary()
    assert s.fires_blocked_check == 0
    assert s.check_block_reasons == {}
    # 冷卻計數器必須還在（語意收窄成「只有真冷卻」，不是拿掉）
    assert s.fires_in_cooldown == 0


# ─────────────── ② 全被擋時要指名，不得歸錯給別的濾網 ───────────────

def test_all_fires_blocked_is_named_not_mislabelled_as_quiet():
    """掃到了、有 FIRE、但每筆都被 cross-check 擋掉 → 零 hold。

    舊碼這格會回 unknown「一筆 HOLD 都沒記到」（成因不明），
    但成因其實一清二楚，只是被 fires_in_cooldown 吃掉了。
    """
    v = drought_verdict(
        _act(scanned=44, hold_reasons={}, fires_blocked_check=6,
             check_block_reasons={"liquidation_check": 6}),
        NOW - 3 * DAY, now_s=NOW)
    assert v is not None
    assert v["cls"] == "gated", "規則量到東西否決＝濾網在做事，不是成因不明"
    assert v["fault"] is False
    assert "cross-check" in v["text"]
    assert "6" in v["text"]
    assert "liquidation_check" in v["text"]


def test_blocked_cause_is_named_even_when_holds_exist():
    """有 hold、也有被擋的 FIRE：兩個成因都要講，不能只講最大宗 hold。"""
    v = drought_verdict(
        _act(scanned=44, hold_reasons={"oi_fuel_insufficient": 30},
             fires_blocked_check=3, check_block_reasons={"etf_check": 3}),
        NOW - 3 * DAY, now_s=NOW)
    assert v["cls"] == "gated"
    assert "cross-check" in v["text"], "被擋的 3 筆消失在訊息裡＝又一次折平"
    assert "oi_fuel_insufficient" in v["text"]


def test_zero_blocked_still_reads_as_cause_unknown():
    """確認過是 0 筆被擋、又一筆 hold 都沒有 → 成因真的不明，不准講成濾網在做事。"""
    v = drought_verdict(
        _act(scanned=44, hold_reasons={}, fires_blocked_check=0),
        NOW - 3 * DAY, now_s=NOW)
    assert v["cls"] == "unknown"


def test_missing_field_is_not_read_as_zero_blocked():
    """舊版活動檔沒有這個欄位＝「這一版量不到」，不是「確認 0 筆被擋」。

    ⛔ 不得因為欄位不存在就宣稱沒有 cross-check 擋單。
    """
    v = drought_verdict(_act(scanned=44, hold_reasons={}), NOW - 3 * DAY, now_s=NOW)
    assert v["cls"] == "unknown"
    assert "0 筆被 cross-check 擋下" not in v["text"]


def test_unparseable_blocked_count_is_flagged_not_silently_zero():
    v = drought_verdict(
        _act(scanned=44, hold_reasons={"btc_gate_closed": 44},
             fires_blocked_check="壞值"),
        NOW - 3 * DAY, now_s=NOW)
    assert "讀不出" in v["text"]


# ─────────────── ③ 失明仍然壓過一切 ───────────────

def test_blind_still_wins_over_blocked():
    """就算有被擋的 FIRE，只要有一筆 hold 是 stale 折出來的，結論就是失明。"""
    v = drought_verdict(
        _act(scanned=44, hold_reasons={"btc_gate_stale": 2},
             fires_blocked_check=5, check_block_reasons={"etf_check": 5}),
        NOW - 3 * DAY, now_s=NOW)
    assert v["cls"] == "blind"
    assert v["fault"] is True
