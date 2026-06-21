# -*- coding: utf-8 -*-
"""plan_snapshot_health 唯讀遙測測試。"""
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from l3_dispatcher.plan_snapshot_health import _classify, capture_health

NOW = 1_750_000_000.0  # 固定 epoch 秒，避免 flake
MS = 1000


def _snap(vol_trend, quadrant, oi_delta):
    return json.dumps({
        "regime_at_entry": {"vol_trend": vol_trend, "oi_price_quadrant": quadrant},
        "context_at_entry": {"oi_delta_pct": oi_delta},
    })


def _make_db(path, rows):
    """rows = list of (regime, entry_at_sec_offset, plan_snapshot)."""
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE paper_trades ("
        "id INTEGER PRIMARY KEY, symbol TEXT, regime TEXT, "
        "entry_at INTEGER, plan_snapshot TEXT)"
    )
    for i, (regime, off_sec, ps) in enumerate(rows):
        con.execute(
            "INSERT INTO paper_trades(symbol,regime,entry_at,plan_snapshot) VALUES(?,?,?,?)",
            (f"SYM{i}", regime, int((NOW + off_sec) * MS), ps),
        )
    con.commit()
    con.close()


# ---- _classify 單元 ----

def test_classify_null():
    assert _classify(None) == "null"


def test_classify_parse_err():
    assert _classify("{not valid json") == "parse_err"


def test_classify_stale_leak():
    assert _classify(_snap("deepdive", None, None)) == "stale_leak"


def test_classify_quadrant_ok():
    assert _classify(_snap("high", "price_up_oi_up", 5.0)) == "quadrant_ok"


def test_classify_none_rangebound():
    # quadrant None 但 OI 在場 = 誠實盤整
    assert _classify(_snap("low", None, 1.2)) == "none_rangebound"


def test_classify_oi_gap():
    # quadrant None 且 OI 缺 = 資料缺口
    assert _classify(_snap("low", None, None)) == "oi_gap"


# ---- capture_health 整合 ----

def test_healthy_ok(tmp_path):
    db = str(tmp_path / "j.db")
    rows = [("deepdive", -3600, _snap("high", "price_up_oi_up", 5.0)) for _ in range(8)]
    rows += [("deepdive", -3600, _snap("low", None, 1.0)) for _ in range(3)]  # 誠實盤整
    _make_db(db, rows)
    out = capture_health(db_path=db, now=NOW)
    assert out["verdict"] == "ok"
    assert out["sample"] == 11
    assert out["null_rate"] == 0.0
    assert out["stale_leak_rate"] == 0.0


def test_high_null_degraded(tmp_path):
    db = str(tmp_path / "j.db")
    rows = [("deepdive", -3600, None) for _ in range(6)]
    rows += [("deepdive", -3600, _snap("high", "price_up_oi_up", 5.0)) for _ in range(4)]
    _make_db(db, rows)
    out = capture_health(db_path=db, now=NOW)
    assert out["verdict"] == "degraded"
    assert out["null_rate"] >= 0.5
    assert any("null" in o for o in out["offenders"])


def test_stale_leak_degraded(tmp_path):
    db = str(tmp_path / "j.db")
    rows = [("deepdive", -3600, _snap("deepdive", None, None)) for _ in range(4)]
    rows += [("deepdive", -3600, _snap("high", "price_up_oi_up", 5.0)) for _ in range(6)]
    _make_db(db, rows)
    out = capture_health(db_path=db, now=NOW)
    assert out["verdict"] == "degraded"
    assert out["stale_leak_rate"] >= 0.25


def test_insufficient_sample(tmp_path):
    db = str(tmp_path / "j.db")
    rows = [("deepdive", -3600, None) for _ in range(3)]  # 全 NULL 但樣本不足
    _make_db(db, rows)
    out = capture_health(db_path=db, now=NOW)
    assert out["verdict"] == "insufficient"  # 不予判讀，不告警


def test_window_excludes_old(tmp_path):
    db = str(tmp_path / "j.db")
    # 老於 48h 的 NULL 列不該被算進來
    rows = [("deepdive", -200 * 3600, None) for _ in range(20)]
    rows += [("deepdive", -3600, _snap("high", "price_up_oi_up", 5.0)) for _ in range(10)]
    _make_db(db, rows)
    out = capture_health(db_path=db, now=NOW)
    assert out["sample"] == 10  # 只算窗內
    assert out["verdict"] == "ok"


def test_oi_gap_not_alarmed(tmp_path):
    db = str(tmp_path / "j.db")
    # OI 缺口多但非碼退化 → 不告警（reported but not alarmed）
    rows = [("deepdive", -3600, _snap("low", None, None)) for _ in range(6)]
    rows += [("deepdive", -3600, _snap("high", "price_up_oi_up", 5.0)) for _ in range(4)]
    _make_db(db, rows)
    out = capture_health(db_path=db, now=NOW)
    assert out["verdict"] == "ok"
    assert out["oi_gap_rate"] >= 0.5  # 有列出


def test_non_deepdive_excluded(tmp_path):
    db = str(tmp_path / "j.db")
    rows = [("breakout", -3600, None) for _ in range(20)]  # 非 deepdive
    rows += [("deepdive", -3600, _snap("high", "price_up_oi_up", 5.0)) for _ in range(8)]
    _make_db(db, rows)
    out = capture_health(db_path=db, now=NOW)
    assert out["sample"] == 8
    assert out["verdict"] == "ok"


def test_missing_db_unknown(tmp_path):
    out = capture_health(db_path=str(tmp_path / "nope.db"), now=NOW)
    assert out["verdict"] == "unknown"  # 例外安全，不擲出


def test_since_ts_excludes_pre_start(tmp_path):
    # 模擬：daemon 重啟前的退化列（NULL/殘留）+ 重啟後當前碼的健康列。
    # since_ts=重啟時刻 → 前化身的退化不算進來 → verdict ok。
    db = str(tmp_path / "j.db")
    restart = NOW - 3600  # 1h 前重啟
    rows = [("deepdive", -7200, None) for _ in range(15)]  # 重啟前 NULL（2h 前）
    rows += [("deepdive", -7200, _snap("deepdive", None, None)) for _ in range(15)]  # 重啟前殘留
    rows += [("deepdive", -1800, _snap("high", "price_up_oi_up", 5.0)) for _ in range(10)]  # 重啟後健康
    _make_db(db, rows)
    out = capture_health(db_path=db, now=NOW, since_ts=restart)
    assert out["sample"] == 10  # 只算重啟後
    assert out["verdict"] == "ok"


def test_since_ts_insufficient_right_after_restart(tmp_path):
    # 剛重啟、當前碼尚無足量進場 → insufficient（fail-closed，不告警）
    db = str(tmp_path / "j.db")
    restart = NOW - 60  # 剛重啟
    rows = [("deepdive", -7200, None) for _ in range(30)]  # 全在重啟前
    _make_db(db, rows)
    out = capture_health(db_path=db, now=NOW, since_ts=restart)
    assert out["sample"] == 0
    assert out["verdict"] == "insufficient"


def test_since_ts_catches_new_regression(tmp_path):
    # 重啟後載到過期碼 → 當前碼進場全是殘留 → 告警（這正是要抓的）
    db = str(tmp_path / "j.db")
    restart = NOW - 3600
    rows = [("deepdive", -1800, _snap("deepdive", None, None)) for _ in range(12)]  # 重啟後殘留
    _make_db(db, rows)
    out = capture_health(db_path=db, now=NOW, since_ts=restart)
    assert out["sample"] == 12
    assert out["verdict"] == "degraded"
    assert out["stale_leak_rate"] == 1.0
