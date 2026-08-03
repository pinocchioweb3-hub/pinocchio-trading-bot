# -*- coding: utf-8 -*-
"""v239：訊號乾旱偵測——「濾網在擋」vs「引擎瞎了」vs「我看不到引擎」不得互折。

事故：2026-07-08 CoinGlass 訂閱到期 → BTC 大盤閘讀不到 → 每檔每輪都 HOLD →
      FIRE 引擎連續 24 天零產出。此前四天每天 16/32/24/27 筆。無人發現。

改動前的碼會失敗在哪（非虛設檢定的證明）：
  * ScanSummary 沒有 hold_reasons 欄位 → test_scan_summary_* 直接 AttributeError
  * l3_dispatcher.scan_activity 模組不存在 → 其餘測試 ImportError
  * fire_queue 沒有 last_fire_ts → test_last_fire_ts_* AttributeError
"""
from __future__ import annotations

import pytest

from l3_dispatcher.scan_activity import (
    classify_holds,
    drought_verdict,
)
from l3_dispatcher.scheduler import ScanSummary, _hold_key

DAY = 86400.0
NOW = 1_754_000_000.0


def _act(**kw) -> dict:
    base = {"ts": NOW - 600, "first_seen_ts": NOW - 30 * DAY,
            "hold_reasons": {}, "btc_gate_open": None}
    base.update(kw)
    return base


# ─────────────────────────── ① reason 不得被丟掉 ───────────────────────────

def test_scan_summary_has_hold_reasons():
    """舊碼只有 summary.holds 一個數字，reason 在 scheduler 第 153 行當場蒸發。"""
    s = ScanSummary()
    assert s.hold_reasons == {}


def test_hold_key_never_merges_closed_with_stale():
    """⛔ 這兩個字尾就是「量到了」與「讀不到」的唯一分野。"""
    assert _hold_key("btc_gate_closed") != _hold_key("btc_gate_stale")
    assert _hold_key("btc_gate_closed") == "btc_gate_closed"
    assert _hold_key("btc_gate_stale") == "btc_gate_stale"


def test_hold_key_strips_varying_numbers_but_keeps_field_names():
    # 括號內數值每檔不同 → 切掉，否則 key 基數無限膨脹
    assert _hold_key("oi_fuel_insufficient(delta=0.31)") == "oi_fuel_insufficient"
    assert _hold_key("oi_fuel_insufficient(delta=0.92)") == "oi_fuel_insufficient"
    # 冒號後是欄位名 → 保留，要知道是「哪個」濾網讀不到
    assert _hold_key("filter_stale:cvd") == "filter_stale:cvd"
    assert _hold_key(None) == "unknown"
    assert _hold_key("   ") == "unknown"


# ─────────────────────────── ② 成因分類 ───────────────────────────

def test_classify_separates_blind_from_gated():
    c = classify_holds({"btc_gate_closed": 8, "btc_gate_stale": 3,
                        "filter_stale:cvd": 2, "unknown_setup:1": 1})
    assert c["blind"] == 5      # stale 類：3 + 2
    assert c["gated"] == 8      # 量到了、閘關著
    assert c["other"] == 1
    assert c["total"] == 14


def test_classify_tolerates_garbage_without_pretending_zero():
    # 壞值跳過，但不得因為有壞值就把整包當空的
    c = classify_holds({"btc_gate_closed": "x", "btc_gate_stale": 4})
    assert c["blind"] == 4
    assert c["gated"] == 0


# ─────────────────────────── ③ 三種乾旱互不折疊 ───────────────────────────

def test_no_drought_when_recent_fire():
    v = drought_verdict(_act(), NOW - 3600, now_s=NOW)
    assert v is None


def test_gated_drought_is_visible_but_not_a_fault():
    """閘關著、而且是真的量出來的——濾網在做事。要看得見，但不是故障。"""
    v = drought_verdict(
        _act(hold_reasons={"btc_gate_closed": 8}, btc_gate_open=False,
             btc_gate_source="risk_off(binance_200ma備援)"),
        NOW - 2 * DAY, now_s=NOW)
    assert v is not None
    assert v["cls"] == "gated"
    assert v["fault"] is False
    assert "48" in v["text"] or "2.0 天" in v["text"]


def test_blind_drought_is_a_system_fault():
    """讀不到資料才 HOLD＝失明。這一格就是 7/08→8/01 那 24 天。"""
    v = drought_verdict(
        _act(hold_reasons={"btc_gate_stale": 8}, btc_gate_open=None),
        NOW - 24 * DAY, now_s=NOW)
    assert v is not None
    assert v["cls"] == "blind"
    assert v["fault"] is True
    assert v["severity"] == "alert"


def test_blind_wins_over_gated_when_both_present():
    """混合時必須報失明——只要有一檔是讀不到，結論就不能講成「濾網在做事」。"""
    v = drought_verdict(
        _act(hold_reasons={"btc_gate_closed": 20, "filter_stale:cvd": 1}),
        NOW - 5 * DAY, now_s=NOW)
    assert v["cls"] == "blind"


# ─────────────── ④ 量不到 ≠ 沒有乾旱（本專案的老病：第 58 次） ───────────────

def test_missing_activity_file_is_unknown_not_healthy():
    v = drought_verdict(None, NOW - 60, now_s=NOW)
    assert v is not None, "活動檔不存在被折成「沒有乾旱」＝同物種復發"
    assert v["cls"] == "unknown"
    assert v["fault"] is True


def test_corrupt_activity_file_is_unknown_not_healthy():
    v = drought_verdict({"_read_error": "JSONDecodeError: x"}, NOW - 60, now_s=NOW)
    assert v["cls"] == "unknown"
    assert v["fault"] is True


def test_stale_activity_file_is_unknown_even_if_fire_is_recent():
    """活動檔三小時沒更新＝掃描迴圈本身可能停了。這時「剛剛有 FIRE」不算數。"""
    v = drought_verdict(_act(ts=NOW - 10 * 3600), NOW - 60, now_s=NOW)
    assert v is not None
    assert v["cls"] == "unknown"


def test_unreadable_last_fire_is_unknown_not_no_drought():
    v = drought_verdict(_act(hold_reasons={"btc_gate_closed": 5}), None, now_s=NOW)
    assert v is not None
    assert v["cls"] == "unknown"
    assert v["fault"] is True


def test_never_fired_uses_first_seen_not_epoch_zero():
    """fires 表空的時候不可拿 epoch 0 算出「乾旱 56 年」這種假事實。"""
    v = drought_verdict(_act(first_seen_ts=NOW - 3 * DAY,
                             hold_reasons={"btc_gate_closed": 5}), 0, now_s=NOW)
    assert v is not None
    assert v["hours"] == pytest.approx(72.0, abs=1.0)


# ─────────────────────────── ⑤ 檔案 I/O 誠實度 ───────────────────────────

def test_read_activity_reports_corruption_instead_of_none(tmp_path, monkeypatch):
    """壞檔回 None 就等於把「讀不到」講成「還沒開始掃」。"""
    from l3_dispatcher import scan_activity as sa

    p = tmp_path / sa.ACTIVITY_FILENAME
    p.write_text("{壞掉的 json", encoding="utf-8")
    monkeypatch.setattr(sa, "activity_path", lambda: p)
    d = sa.read_activity()
    assert d is not None
    assert "_read_error" in d


def test_write_activity_is_atomic(tmp_path, monkeypatch):
    """非原子寫會造出半截壞檔，然後自己再誤讀成「本來就沒有」(v162-v166 共同成因)。"""
    from l3_dispatcher import scan_activity as sa

    p = tmp_path / sa.ACTIVITY_FILENAME
    monkeypatch.setattr(sa, "activity_path", lambda: p)
    sa.write_activity({"ts": 1, "hold_reasons": {"btc_gate_closed": 3}})
    assert sa.read_activity()["hold_reasons"] == {"btc_gate_closed": 3}
    assert not (tmp_path / (sa.ACTIVITY_FILENAME + ".tmp")).exists()
    assert not p.with_suffix(".json.tmp").exists()


# ─────────────────────────── ⑥ last_fire_ts ───────────────────────────

def test_last_fire_ts_covers_history_table(tmp_path, monkeypatch):
    """archive_and_clean 會把 7 天前的搬進 fires_history；只看 active 表的話，
    歸檔隔天就會誤判成「從來沒 FIRE 過」。"""
    import sqlite3

    from l3_dispatcher import fire_queue as fq

    db = tmp_path / "fire_queue.db"
    monkeypatch.setattr(fq, "DB_PATH", db)
    con = sqlite3.connect(db)
    fq._init(con)
    con.execute("CREATE TABLE fires_history (original_id INTEGER, "
                "enqueued_at INTEGER)")
    con.execute("INSERT INTO fires_history VALUES (1, 1700000000)")
    con.commit()
    con.close()
    assert fq.last_fire_ts() == 1700000000


def test_last_fire_ts_zero_when_truly_empty(tmp_path, monkeypatch):
    from l3_dispatcher import fire_queue as fq

    monkeypatch.setattr(fq, "DB_PATH", tmp_path / "empty.db")
    assert fq.last_fire_ts() == 0


# ─────────────────── ⑦ 帳本整合：只推 TG 不算有出口（v170 鐵則） ───────────────────

def _assess(**kw):
    from l3_dispatcher.ceo_oversight import assess
    base = dict(now_ms=int(NOW * 1000), commit_age_sec=60,
                paper_n=0, paper_min=30, live_n=0, live_min=30,
                demo_n=0, demo_live=0, demo_active=True,
                open_decisions=0, pending_outbox=0)
    base.update(kw)
    return assess(**base)


def test_blind_drought_lands_in_system_faults():
    v = _assess(signal_drought={"cls": "blind", "fault": True,
                                "text": "訊號引擎已 576h 零產出（失明）"})
    assert any("576h" in f for f in v["system_faults"]), \
        "乾旱沒有進帳本 system_faults＝只有 Telegram 看得到＝v170 說的假出口"
    assert not any("576h" in b for b in v["blockers"]), "失明是工程要修的，不是球在使用者"
    assert v["state"] != "ADVANCING", "引擎瞎了還回報推進中"


def test_gated_drought_is_a_blocker_not_a_fault():
    """濾網盡責擋單不是故障，但零產出仍要每天看得到（要不要放寬閘是使用者的決定）。"""
    v = _assess(signal_drought={"cls": "gated", "fault": False,
                                "text": "訊號引擎已 48h 零產出：BTC 大盤閘"})
    assert any("48h" in b for b in v["blockers"])
    assert v["state"] == "BLOCKED_ON_USER"


def test_no_drought_changes_nothing():
    a = _assess(signal_drought=None)
    assert not any("零產出" in b for b in a["blockers"])


# ────────── ⑧ 「一檔都沒掃到」不得偽裝成「市場很安靜」（實測抓到的） ──────────

def test_zero_scanned_is_named_explicitly_not_vague_no_holds():
    """探針實測：掃 0 檔時舊訊息寫「最大宗 HOLD 理由：（無）」，讀起來像沒事。"""
    v = drought_verdict(_act(scanned=0, hold_reasons={}), NOW - 26 * DAY, now_s=NOW)
    assert v["cls"] == "unknown"
    assert v["fault"] is True
    assert "一檔都沒掃到" in v["text"]


def test_scanned_but_no_holds_is_still_unknown_not_gated():
    """掃到了、零 FIRE、卻一筆 HOLD 都沒記——成因不明，不可講成濾網在做事。"""
    v = drought_verdict(_act(scanned=44, hold_reasons={}), NOW - 2 * DAY, now_s=NOW)
    assert v["cls"] == "unknown"
    assert "44 檔" in v["text"]


def test_scanned_unreadable_is_not_treated_as_fine():
    v = drought_verdict(_act(scanned="壞值", hold_reasons={}), NOW - 2 * DAY, now_s=NOW)
    assert v["cls"] == "unknown"
    assert "讀不出" in v["text"]
