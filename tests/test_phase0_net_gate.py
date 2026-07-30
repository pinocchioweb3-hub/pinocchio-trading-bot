"""v149 Phase 0 淨值口徑閘門：_count_closed_net + phase0_status 的 fail-closed 行為。

背景：phase0_status 原本用毛 R（realized_r）判「真實小額 30 筆期望值為正」。費用與滑價
在真錢側不可迴避——紙上配對子集實測費用吃掉 0.065R，加密引擎點估計因此由正翻負。故
live 閘門加上「必須有淨值證據且淨期望值為正」，且 trades 表沒有 net_r 時要**明說原因**。
"""
import sqlite3

import pytest

from l3_dispatcher import ceo_session as cs


def _mkdb(path, *, live_rows=(), live_net=True, paper_rows=(), paper_net=True):
    """建一份最小 trade_journal.db。live_net=False → trades 表不含 net_r 欄（＝現況）。"""
    conn = sqlite3.connect(path)
    for table, rows, has_net in (("trades", live_rows, live_net),
                                 ("paper_trades", paper_rows, paper_net)):
        cols = "id INTEGER PRIMARY KEY, status TEXT, exit_reason TEXT, realized_r REAL"
        if has_net:
            cols += ", net_r REAL"
        conn.execute(f"CREATE TABLE {table} ({cols})")
        for r in rows:
            if has_net:
                conn.execute(
                    f"INSERT INTO {table} (status, exit_reason, realized_r, net_r) "
                    f"VALUES (?,?,?,?)", r)
            else:
                conn.execute(
                    f"INSERT INTO {table} (status, exit_reason, realized_r) "
                    f"VALUES (?,?,?)", r[:3])
    conn.commit()
    conn.close()


@pytest.fixture
def db(tmp_path, monkeypatch):
    def _make(**kw):
        p = tmp_path / "tj.db"
        if p.exists():
            p.unlink()
        _mkdb(p, **kw)
        monkeypatch.setattr(cs, "_TJ_DB", str(p))
        return p
    return _make


# --- _count_closed_net -----------------------------------------------------

def test_net_missing_column_returns_none_not_zero(db):
    """欄位不存在要回 (0, None)。回 0.0 會被下游誤讀成「淨期望值恰為零」。"""
    db(live_rows=[("closed", "tp1", 1.0)], live_net=False)
    assert cs._count_closed_net("trades") == (0, None)


def test_net_excludes_entry_expired_and_nulls(db):
    """與毛口徑同排除規則：entry_expired 不是真實交易；淨值為空的列不進配對子集。"""
    db(paper_rows=[("closed", "tp1", 1.0, 0.9),
                   ("closed", "entry_expired", 0.0, 0.0),   # 排除
                   ("closed", "stop", -1.0, None),          # 無淨值 → 不算
                   ("open", "", 2.0, 1.9)])                 # 未平倉 → 不算
    n, ev = cs._count_closed_net("paper_trades")
    assert (n, ev) == (1, 0.9)


def test_net_missing_table(db):
    db()
    assert cs._count_closed_net("no_such_table") == (0, None)


# --- phase0_status live 閘門 ----------------------------------------------

def _live(n, gross, net=None):
    return [("closed", "tp1", gross) + ((net,) if net is not None else (gross,))
            for _ in range(n)]


def test_live_gate_fail_closed_when_no_net_column(db):
    """現況：30 筆真錢、毛 R 為正，但 trades 無 net_r → 不得放行，且要說出原因。"""
    db(live_rows=[("closed", "tp1", 0.5)] * 30, live_net=False,
       paper_rows=[("closed", "tp1", 0.2, 0.1)] * 120)
    p = cs.phase0_status()
    assert p["paper_ok"] is True
    assert p["live_n"] == 30 and p["live_ev_r"] > 0      # 毛口徑本來會過
    assert p["live_ok"] is False                         # fail-closed
    assert p["ready"] is False
    assert p["live_gate_reason"] == "live_net_missing"
    assert p["live_ev_r_net"] is None                    # 不是 0.0


def test_live_gate_blocks_when_net_ev_negative(db):
    """毛正淨負＝最危險的一種：費用吃掉全部 edge，必須擋。"""
    db(live_rows=[("closed", "tp1", 0.05, -0.02)] * 30,
       paper_rows=[("closed", "tp1", 0.2, 0.1)] * 120)
    p = cs.phase0_status()
    assert p["live_ev_r"] > 0 > p["live_ev_r_net"]
    assert p["live_ok"] is False and p["ready"] is False
    assert p["live_gate_reason"] == "live_net_ev_not_positive"


def test_live_gate_blocks_on_partial_net_coverage(db):
    """淨值只覆蓋一部分樣本 → 不算證據充分（30 筆裡只有 10 筆有淨值）。"""
    rows = [("closed", "tp1", 0.5, 0.4)] * 10 + [("closed", "tp1", 0.5, None)] * 20
    db(live_rows=rows, paper_rows=[("closed", "tp1", 0.2, 0.1)] * 120)
    p = cs.phase0_status()
    assert p["live_net_n"] == 10 and p["live_ok"] is False
    assert p["live_gate_reason"] == "live_net_coverage_short"


def test_live_gate_passes_only_with_positive_net(db):
    """三個條件同時成立才放行——且 ready 仍只是『可由人類考慮宣告』（紅線③）。"""
    db(live_rows=[("closed", "tp1", 0.5, 0.35)] * 30,
       paper_rows=[("closed", "tp1", 0.2, 0.1)] * 120)
    p = cs.phase0_status()
    assert p["live_ok"] is True and p["ready"] is True
    assert p["live_gate_reason"] is None


def test_sample_short_reason_takes_precedence(db):
    """真錢 0 筆時原因是『樣本不足』，不是『淨值缺欄』——別把人工閘誤報成會計缺口。"""
    db(live_rows=[], live_net=False, paper_rows=[("closed", "tp1", 0.2, 0.1)] * 120)
    p = cs.phase0_status()
    assert p["live_n"] == 0
    assert p["live_gate_reason"] == "live_sample_short"


def test_paper_net_reported_alongside_gross(db):
    """毛/淨並列呈現：口徑標籤要在，數字不可互相冒充。"""
    db(paper_rows=[("closed", "tp1", 1.0, 0.5), ("closed", "stop", -1.0, -1.1)])
    p = cs.phase0_status()
    assert p["ev_basis"] == "gross"
    assert p["paper_ev_r"] == 0.0 and p["paper_ev_r_net"] == -0.3
    assert p["paper_net_n"] == 2
