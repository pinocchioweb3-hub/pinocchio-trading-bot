"""CEO Session — promotion_gate 計數口徑測試（紅線 3：解鎖進度不可灌水）。

`_count_closed()` 算 Phase 0 解鎖門檻的「真實已平倉」筆數與平均 R。唯讀稽核發現它
原本沒排除 exit_reason='entry_expired'（限價單掛了但價沒走到、從未成交的逾時作廢單，
realized_r 一律 0、**不是一筆真實交易**）。實測真倉 DB：含它 n=35/avg 0.377，排除後
才是真實的 n=24/avg 0.549 —— 11 筆無效掛單被灌進門檻分母，門檻提前約 1/3 達標。

本檔把該回歸鎖死：
  * paper_trades 必須排除 entry_expired（重現 24-vs-35 的灌水形狀）；
  * trades（實倉）有 exit_reason 欄但永無 entry_expired，過濾不可丟例外、計數要正確；
  * exit_reason=NULL 的正常平倉單不可被誤殺（IFNULL 守衛）；
  * 表不存在 → 安全回 (0, 0.0)。

與 l3_dispatcher/paper_journal.get_paper_stats 的排除規則同口徑。

設計：每個 case 建一個臨時 trade_journal.db，monkeypatch ceo_session._TJ_DB 指過去，
純判定、不碰真資料庫。執行（任一）：
    pytest tests/test_ceo_session.py
    python tests/test_ceo_session.py
"""
from __future__ import annotations

import contextlib
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import l3_dispatcher.ceo_session as ceo

# _count_closed 只讀 status / realized_r / exit_reason 三欄；最小表足以還原其行為。
_DDL = (
    "CREATE TABLE {t} ("
    " id INTEGER PRIMARY KEY,"
    " status TEXT,"
    " realized_r REAL,"
    " exit_reason TEXT)"
)


def _build_db(paper=(), trades=None) -> str:
    """建臨時 DB，rows = list of (status, realized_r, exit_reason)。trades=None 則不建該表。"""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    try:
        conn.execute(_DDL.format(t="paper_trades"))
        conn.executemany(
            "INSERT INTO paper_trades(status, realized_r, exit_reason) VALUES (?,?,?)",
            list(paper),
        )
        if trades is not None:
            conn.execute(_DDL.format(t="trades"))
            conn.executemany(
                "INSERT INTO trades(status, realized_r, exit_reason) VALUES (?,?,?)",
                list(trades),
            )
        conn.commit()
    finally:
        conn.close()
    return path


@contextlib.contextmanager
def _use_db(**kw):
    """建臨時 DB 並把 ceo._TJ_DB 指過去，離開時還原 + 刪檔。"""
    path = _build_db(**kw)
    old = ceo._TJ_DB
    ceo._TJ_DB = path
    try:
        yield path
    finally:
        ceo._TJ_DB = old
        with contextlib.suppress(OSError):
            os.unlink(path)


# === paper_trades：必須排除 entry_expired ====================================
def test_excludes_entry_expired_paper():
    paper = [
        ("closed", 1.0, "tp1"),
        ("closed", 2.0, "tp2"),
        ("closed", -0.5, "stop"),
        ("closed", 0.0, "entry_expired"),   # 無效掛單 — 必須排除
        ("closed", 0.0, "entry_expired"),
        ("open", None, None),               # 未平倉 — status 排除
    ]
    with _use_db(paper=paper):
        n, avg = ceo._count_closed("paper_trades")
    assert n == 3                            # 3 筆真實平倉，不含 2 筆 entry_expired、不含 open
    assert avg == 0.833                      # (1 + 2 - 0.5) / 3 = 0.8333…


def test_inflation_shape_24_vs_35():
    """重現稽核：含 entry_expired 會把真實的 24/0.549 灌成 35/偏低期望值。"""
    paper = [("closed", 0.549, "tp1") for _ in range(24)]
    paper += [("closed", 0.0, "entry_expired") for _ in range(11)]
    with _use_db(paper=paper) as path:
        n, avg = ceo._count_closed("paper_trades")
        # 對照組：拿掉過濾，分母多 11 筆、期望值被 0R 往下灌
        raw = sqlite3.connect(path)
        try:
            rn, ravg = raw.execute(
                "SELECT COUNT(*), AVG(realized_r) FROM paper_trades WHERE status='closed'"
            ).fetchone()
        finally:
            raw.close()
    assert (n, avg) == (24, 0.549)           # 修正後：只算真實交易
    assert rn == 35                          # 未過濾會多 11 筆無效掛單（灌水形狀）
    assert ravg < avg                        # 且把期望值往下拉（0R 稀釋）


def test_null_exit_reason_counts():
    """exit_reason=NULL 的正常平倉單仍算（IFNULL → '' != 'entry_expired'，不可誤殺）。"""
    with _use_db(paper=[("closed", 1.0, None)]):
        n, avg = ceo._count_closed("paper_trades")
    assert n == 1
    assert avg == 1.0


# === trades（實倉）：過濾要對兩表都安全 ======================================
def test_trades_table_filter_is_safe():
    """trades 有 exit_reason 欄但永無 entry_expired；過濾不應丟例外、計數正確。"""
    trades = [
        ("closed", 1.0, "tp1"),
        ("closed", 0.0, "stop"),
        ("open", None, None),
    ]
    with _use_db(paper=[], trades=trades):
        n, avg = ceo._count_closed("trades")
    assert n == 2                            # 兩筆已平倉
    assert avg == 0.5                        # (1.0 + 0.0) / 2


# === 防呆：表不存在 → (0, 0.0)，不丟例外 =====================================
def test_missing_table_returns_zero():
    with _use_db(paper=[]):
        assert ceo._count_closed("does_not_exist") == (0, 0.0)


# --- 直接執行（無 pytest 也能跑）---
if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    for fn in fns:
        try:
            fn()
            print(f"  PASS  {fn.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"  FAIL  {fn.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            print(f"  ERROR {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{passed}/{len(fns)} passed")
    sys.exit(0 if passed == len(fns) else 1)
