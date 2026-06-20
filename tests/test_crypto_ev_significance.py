"""task#27 crypto-only EV 顯著性工具測試（離線唯讀回測器）。

驗證重點：
  • 分類（_base / classify）：crypto vs 美股白名單 vs 代幣化非加密。
  • ev_stats 統計核：強 edge→顯著、零 edge→不顯著、小樣本 fail-closed、
    DSR 隨 n_trials 多重檢定懲罰單調下降、PSR 與 minTRL 一致性。
  • 載入層（_load_table / load_closed / real_money_count）：用 tmp_path 建合成 DB，
    驗證 status='closed' 過濾、未成交 filled 旗標、paper/demo 分表、缺表/缺欄 fail-soft、
    **且工具對 DB 純唯讀（不寫、不改 mtime/size）**。
  • build_report / render：切口齊全、誠實橫幅在、真錢分母與 paper 樣本分離、不崩。

全離線、零網路、零真錢、零訊號數學。
"""
import os
import sqlite3
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import backtest.crypto_ev_significance as cev


# ── 分類 ────────────────────────────────────────────────────────────────────
def test_base_strips_suffixes():
    assert cev._base("BTC-USDT-SWAP") == "BTC"
    assert cev._base("ETHUSDT") == "ETH"
    assert cev._base("SOLUSDC") == "SOL"
    assert cev._base("BTC") == "BTC"
    assert cev._base("xrp-usdt") == "XRP"        # 大小寫正規化
    assert cev._base("") == ""                    # 空字串不炸


def test_classify_three_buckets():
    assert cev.classify("BTC") == "crypto"
    assert cev.classify("SOL") == "crypto"
    assert cev.classify("NVDA") == "us"           # 美股白名單
    assert cev.classify("QQQ") == "us"
    assert cev.classify("XAU") == "noncrypto_token"   # 代幣化黃金
    assert cev.classify("CL") == "noncrypto_token"    # 原油


# ── ev_stats 統計核 ─────────────────────────────────────────────────────────
def test_strong_edge_is_significant():
    import random
    rng = random.Random(11)
    pos = [rng.gauss(0.4, 1.0) for _ in range(120)]
    s = cev.ev_stats(pos, "pos", 1)
    assert s.n == 120
    assert s.psr is not None and s.psr > 0.95
    assert s.significant is True
    assert s.min_trl is not None                  # 有限 minTRL
    assert "顯著" in s.verdict


def test_zero_edge_not_significant():
    # 確定性零 edge：等量 +1/-1 → 樣本均值嚴格=0 → SR=0、PSR≈0.5、minTRL=∞。
    # （刻意不用隨機抽樣：單次 N(0,1) 抽 120 筆均值可能偶然顯著偏離 0——
    #  那正是 data-snooping 的風險本身，不該拿來當「零 edge」的測試夾具。）
    flat = [1.0, -1.0] * 60
    s = cev.ev_stats(flat, "flat", 1)
    assert s.significant is False
    assert s.min_trl is None                       # SR≤0 → minTRL=∞
    assert s.psr is not None and 0.4 < s.psr < 0.6   # ≈0.5 擲硬幣


def test_small_sample_fails_closed():
    s = cev.ev_stats([0.5] * 10, "small", 1)
    assert s.n == 10
    assert s.significant is False
    assert "不足" in s.verdict                     # fail-closed 文字


def test_empty_sample():
    s = cev.ev_stats([], "empty", 1)
    assert s.n == 0
    assert s.significant is False
    assert s.verdict == "無樣本"


def test_dsr_monotonic_in_n_trials():
    """多重檢定懲罰：n_trials 越多 → deflated Sharpe 越低（或持平）。"""
    import random
    rng = random.Random(3)
    pos = [rng.gauss(0.3, 1.0) for _ in range(80)]
    d1 = cev.ev_stats(pos, "d1", 1).dsr_ledger
    d50 = cev.ev_stats(pos, "d50", 50).dsr_ledger
    assert d1 is not None and d50 is not None
    assert d50 <= d1


def test_profit_factor_and_winrate():
    # 3 勝(+1) 2 敗(-1)：win=60%、PF=3/2=1.5
    s = cev.ev_stats([1.0, 1.0, 1.0, -1.0, -1.0], "pf", 1)
    assert s.win_rate == 60.0
    assert s.profit_factor == 1.5
    assert s.sum_r == 1.0


def test_all_wins_profit_factor_none():
    # 無虧損 → gross loss=0 → PF 定義為 None（不除以零）
    s = cev.ev_stats([1.0, 2.0, 3.0], "allwin", 1)
    assert s.profit_factor is None


# ── 載入層（合成 DB） ────────────────────────────────────────────────────────
def _make_db(path: str):
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE paper_trades (symbol TEXT, realized_r REAL, status TEXT, "
        "exit_reason TEXT, direction TEXT, regime TEXT)")
    con.executemany(
        "INSERT INTO paper_trades VALUES (?,?,?,?,?,?)",
        [
            ("BTC-USDT-SWAP", 1.5, "closed", "tp3", "long", "trend"),
            ("ETH-USDT-SWAP", -0.8, "closed", "stop", "short", "range"),
            ("SOL-USDT-SWAP", 0.0, "closed", "entry_expired", "long", "trend"),  # 未成交
            ("NVDA", 0.3, "closed", "tp1", "long", "trend"),                      # 美股
            ("XAU-USDT", 0.2, "closed", "tp1", "long", "trend"),                  # 代幣化
            ("DOGE-USDT-SWAP", 0.4, "open", "", "long", "trend"),                 # 未平倉→排除
        ])
    con.execute(
        "CREATE TABLE demo_trades (symbol TEXT, realized_r REAL, status TEXT, "
        "exit_reason TEXT, direction TEXT, regime TEXT)")
    con.execute("INSERT INTO demo_trades VALUES ('BTC-USDT-SWAP', 0.5, 'closed', 'tp1', 'long', 'trend')")
    # 真錢帳 trades 表（Phase-0 分母）
    con.execute("CREATE TABLE trades (symbol TEXT, status TEXT)")
    con.execute("INSERT INTO trades VALUES ('BTC-USDT-SWAP', 'closed')")
    con.execute("INSERT INTO trades VALUES ('ETH-USDT-SWAP', 'open')")  # 未平倉→不計
    con.commit()
    con.close()


def test_load_closed_filters_and_classifies(tmp_path):
    db = str(tmp_path / "j.db")
    _make_db(db)
    rows = cev.load_closed(db, include_demo=True)
    # paper 已平倉 5 筆（含美股+代幣化，排除 open 的 DOGE）+ demo 1 筆 = 6
    assert len(rows) == 6
    paper = [r for r in rows if r.table == "paper"]
    demo = [r for r in rows if r.table == "demo"]
    assert len(paper) == 5
    assert len(demo) == 1
    # SOL entry_expired → filled=False；其餘成交=True
    sol = [r for r in paper if r.base == "SOL"][0]
    assert sol.filled is False
    btc = [r for r in paper if r.base == "BTC"][0]
    assert btc.filled is True
    # 分類正確
    assert {r.base: r.klass for r in paper} == {
        "BTC": "crypto", "ETH": "crypto", "SOL": "crypto",
        "NVDA": "us", "XAU": "noncrypto_token"}


def test_exclude_demo(tmp_path):
    db = str(tmp_path / "j.db")
    _make_db(db)
    rows = cev.load_closed(db, include_demo=False)
    assert all(r.table == "paper" for r in rows)
    assert len(rows) == 5


def test_real_money_count(tmp_path):
    db = str(tmp_path / "j.db")
    _make_db(db)
    # trades 表 1 closed + 1 open → 只計 closed=1
    assert cev.real_money_count(db) == 1


def test_missing_tables_fail_soft(tmp_path):
    db = str(tmp_path / "empty.db")
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE unrelated (x INTEGER)")
    con.commit()
    con.close()
    assert cev.load_closed(db) == []
    assert cev.real_money_count(db) == 0


def test_tool_is_read_only(tmp_path):
    """治本鐵則：本工具對 DB 純唯讀——跑完 size 與 mtime 不變。"""
    db = str(tmp_path / "j.db")
    _make_db(db)
    size0 = os.path.getsize(db)
    mtime0 = os.path.getmtime(db)
    cev.build_report(db, include_demo=True)
    cev.load_closed(db)
    cev.real_money_count(db)
    assert os.path.getsize(db) == size0
    assert os.path.getmtime(db) == mtime0


# ── build_report / render ───────────────────────────────────────────────────
def test_build_report_cuts_and_separation(tmp_path):
    db = str(tmp_path / "j.db")
    _make_db(db)
    rep = cev.build_report(db, include_demo=True)
    # 真錢與 paper 分離：真錢=1（trades closed），paper 樣本另計
    assert rep.real_money_closed == 1
    assert len(rep.cuts) == 5                       # 5 切口固定
    labels = [c.label for c in rep.cuts]
    assert any("嚴格" in l for l in labels)
    assert any("寬鬆" in l for l in labels)
    assert any("US" in l for l in labels)
    # 嚴格 crypto paper：BTC/ETH/SOL = 3 筆（NVDA、XAU 已排除）
    strict = rep.cuts[0]
    assert strict.n == 3
    # 寬鬆含 XAU → 4 筆
    loose = rep.cuts[1]
    assert loose.n == 4
    # by_reason 含 entry_expired
    assert "entry_expired" in rep.by_reason


def test_render_has_honesty_banner(tmp_path):
    db = str(tmp_path / "j.db")
    _make_db(db)
    rep = cev.build_report(db, include_demo=True)
    txt = cev.render(rep)
    assert "模擬盤" in txt and "非真錢" in txt        # 紅線③ 誠實橫幅
    assert "Phase-0" in txt
    assert isinstance(txt, str) and len(txt) > 100


def test_selftest_passes():
    assert cev._selftest() is True
