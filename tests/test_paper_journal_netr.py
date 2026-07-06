# -*- coding: utf-8 -*-
"""v118 淨值口徑：compute_net_r 純函式 + net_r 欄遷移。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from l3_dispatcher.paper_journal import compute_net_r


def test_fee_math_r_units():
    """entry=100, stop=96(R距=4)：費用R = 2×0.0005×100/4 = 0.025R。tp3 無滑價。"""
    net = compute_net_r(2.0, 100, 96, "tp3", fee_rate=0.0005, stop_slip_r=0.05)
    assert net == round(2.0 - 0.025, 4)


def test_stop_adds_slippage():
    """stop 出場加滑價 0.05R：-1.0 − 0.025 − 0.05 = -1.075。"""
    net = compute_net_r(-1.0, 100, 96, "stop", fee_rate=0.0005, stop_slip_r=0.05)
    assert net == -1.075


def test_timeout_no_slip():
    net = compute_net_r(0.5, 100, 96, "timeout", fee_rate=0.0005, stop_slip_r=0.05)
    assert net == round(0.5 - 0.025, 4)


def test_tight_stop_costs_more_r():
    """止損越近，同樣費用吃掉越多 R（entry=100, stop=99.5 → 費用R=0.2）——誠實反映。"""
    net = compute_net_r(1.0, 100, 99.5, "tp1", fee_rate=0.0005, stop_slip_r=0.05)
    assert net == round(1.0 - 0.2, 4)


def test_degenerate_honest_none():
    assert compute_net_r(1.0, 100, 100, "tp1") is None      # 零風險距離
    assert compute_net_r(1.0, None, 96, "tp1") is None      # 缺進場價
    assert compute_net_r(1.0, 0, 96, "tp1") is None         # 非法價


# ------------------------------------------------- v121 stack_depth 落帳
def test_stack_depth_recorded_on_entry(tmp_path, monkeypatch):
    """同幣同向第 1/2/3 筆應分別記 stack_depth=0/1/2；反向不計。"""
    import l3_dispatcher.paper_journal as pj
    db = tmp_path / "tj.db"
    monkeypatch.setattr(pj, "DB_PATH", db)
    pj.init_db()
    id1 = pj.record_paper_entry("ETH", "deepdive", "bear", 1800, 1850, 1750, 1700, 1650)
    id2 = pj.record_paper_entry("ETH", "deepdive", "bear", 1790, 1840, 1740, 1690, 1640)
    id3 = pj.record_paper_entry("ETH", "deepdive", "bull", 1780, 1730, 1830, 1880, 1930)
    import sqlite3
    c = sqlite3.connect(str(db))
    rows = dict(c.execute("SELECT id, stack_depth FROM paper_trades").fetchall())
    c.close()
    assert rows[id1] == 0        # 首筆
    assert rows[id2] == 1        # 第二筆同向 → 已有 1 筆在場
    assert rows[id3] == 0        # 反向是自己的首筆


# ------------------------------------------------- v122 intraday 疊倉閘
def test_intraday_stack_cap_blocks_third(tmp_path, monkeypatch):
    """intraday 同幣同向在場達 2 筆 → 第 3 筆回 -1 不入帳；deepdive 不受限；反向不受限。"""
    import l3_dispatcher.paper_journal as pj
    db = tmp_path / "tj.db"
    monkeypatch.setattr(pj, "DB_PATH", db)
    pj.init_db()
    a = pj.record_paper_entry("ETH", "intraday", "bear", 1800, 1850, 1750, 1700, 1650)
    b = pj.record_paper_entry("ETH", "intraday", "bear", 1790, 1840, 1740, 1690, 1640)
    c3 = pj.record_paper_entry("ETH", "intraday", "bear", 1780, 1830, 1730, 1680, 1630)
    assert a > 0 and b > 0 and c3 == -1          # 第 3 筆被疊倉閘擋下
    # deepdive 同幣同向不受此閘（LLM 逐次重新分析的已驗證軌道）
    d1 = pj.record_paper_entry("ETH", "deepdive", "bear", 1800, 1850, 1750, 1700, 1650)
    assert d1 > 0
    # intraday 反向（bull）是自己的首筆 → 放行
    e1 = pj.record_paper_entry("ETH", "intraday", "bull", 1780, 1730, 1830, 1880, 1930)
    assert e1 > 0


# ------------------------------------------------- v124 StoplossGuard shadow
def test_dir_stops_24h_recorded(tmp_path, monkeypatch):
    """同方向近24h止損數落帳：2筆 bull 止損後,新 bull 單記 2、bear 單記 0。"""
    import sqlite3
    import time as _t
    import l3_dispatcher.paper_journal as pj
    db = tmp_path / "tj.db"
    monkeypatch.setattr(pj, "DB_PATH", db)
    pj.init_db()
    now = int(_t.time() * 1000)
    conn = sqlite3.connect(str(db))
    conn.execute("INSERT INTO paper_trades (symbol,setup,direction,entry_price,stop_price,"
                 "tp1,entry_at,created_at,status,exit_reason,exit_at) VALUES "
                 "('SOL','deepdive','bull',80,78,84,?,?,'closed','stop',?)",
                 (now - 5 * 3600_000,) * 2 + (now - 3 * 3600_000,))
    conn.execute("INSERT INTO paper_trades (symbol,setup,direction,entry_price,stop_price,"
                 "tp1,entry_at,created_at,status,exit_reason,exit_at) VALUES "
                 "('INJ','deepdive','bull',10,9.5,11,?,?,'closed','stop',?)",
                 (now - 4 * 3600_000,) * 2 + (now - 1 * 3600_000,))
    # 30h 前的舊止損 → 出窗不計
    conn.execute("INSERT INTO paper_trades (symbol,setup,direction,entry_price,stop_price,"
                 "tp1,entry_at,created_at,status,exit_reason,exit_at) VALUES "
                 "('FIL','deepdive','bull',0.8,0.75,0.9,?,?,'closed','stop',?)",
                 (now - 40 * 3600_000,) * 2 + (now - 30 * 3600_000,))
    conn.commit(); conn.close()
    b1 = pj.record_paper_entry("ETH", "deepdive", "bull", 1800, 1750, 1900, 1950, 2000)
    s1 = pj.record_paper_entry("ETH", "deepdive", "bear", 1800, 1850, 1700, 1650, 1600)
    conn = sqlite3.connect(str(db))
    rows = dict(conn.execute("SELECT id, dir_stops_24h FROM paper_trades "
                             "WHERE id IN (?,?)", (b1, s1)).fetchall())
    conn.close()
    assert rows[b1] == 2      # 24h 內 2 筆 bull 止損（30h 前那筆不計）
    assert rows[s1] == 0      # bear 方向乾淨
