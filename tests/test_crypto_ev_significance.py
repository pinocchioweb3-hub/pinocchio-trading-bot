"""task#27 crypto-only EV 顯著性工具測試（離線唯讀回測器，v2 對抗稽核後）。

驗證重點：
  • 分類（_base / classify）：crypto vs 美股白名單 vs 代幣化非加密。
  • ev_stats 統計核：強 edge→顯著、零 edge→不顯著、小樣本 fail-closed、
    PSR 與 minTRL 一致性、量級 _mag。
  • 獨立性校正：_icc_oneway / effective_n / _psr_with_n（叢聚→n_eff<n、
    PSRc≤PSR、judgeable 以 n_eff 把關 fail-closed）。
  • 載入層（_load_table / load_closed / real_money_count）：用 tmp_path 建合成 DB，
    驗證 status='closed' 過濾、未成交 filled 旗標、entry_at→entry_ms、paper/demo
    分表、缺表/缺欄 fail-soft，**且工具對 DB 純唯讀（不寫、不改 mtime/size）**。
  • build_report / render：切口齊全（主結論＝僅真成交在首）、誠實橫幅在、
    真錢分母與 paper 樣本分離、n<門檻不予判讀、不崩。

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
    s = cev.ev_stats(pos, "pos", role="primary")   # 無 day_keys → n_eff=None
    assert s.n == 120
    assert s.psr is not None and s.psr > 0.95
    assert s.judgeable is True                      # n≥30、n_eff=None 不擋
    assert s.significant is True
    assert s.min_trl is not None                    # 有限 minTRL
    assert "顯著" in s.verdict


def test_zero_edge_not_significant():
    # 確定性零 edge：等量 +1/-1 → 樣本均值嚴格=0 → SR=0、PSR≈0.5、minTRL=∞。
    # （刻意不用隨機抽樣：單次 N(0,1) 抽 120 筆均值可能偶然顯著偏離 0——
    #  那正是 data-snooping 的風險本身，不該拿來當「零 edge」的測試夾具。）
    flat = [1.0, -1.0] * 60
    s = cev.ev_stats(flat, "flat")
    assert s.significant is False
    assert s.min_trl is None                        # SR≤0 → minTRL=∞
    assert s.psr is not None and 0.4 < s.psr < 0.6   # ≈0.5 擲硬幣


def test_small_sample_fails_closed():
    s = cev.ev_stats([0.5] * 10, "small")
    assert s.n == 10
    assert s.judgeable is False
    assert s.significant is False
    assert "不足" in s.verdict                      # fail-closed 文字


def test_empty_sample():
    s = cev.ev_stats([], "empty")
    assert s.n == 0
    assert s.significant is False
    assert s.verdict == "無樣本"


def test_profit_factor_and_winrate():
    # 3 勝(+1) 2 敗(-1)：win=60%、PF=3/2=1.5
    s = cev.ev_stats([1.0, 1.0, 1.0, -1.0, -1.0], "pf")
    assert s.win_rate == 60.0
    assert s.profit_factor == 1.5
    assert s.sum_r == 1.0


def test_all_wins_profit_factor_none():
    # 無虧損 → gross loss=0 → PF 定義為 None（不除以零）
    s = cev.ev_stats([1.0, 2.0, 3.0], "allwin")
    assert s.profit_factor is None


# ── 獨立性校正（n_eff / ICC / PSRc / _mag） ─────────────────────────────────
def test_icc_high_when_clustered():
    # 群內全同值（within=0）→ ICC=1.0
    icc, mbar, k = cev._icc_oneway([[0.5] * 20, [-0.5] * 20, [0.5] * 20])
    assert k == 3
    assert mbar == 20.0
    assert icc > 0.99


def test_icc_zero_when_singletons():
    # 每群 1 筆（total<=k）→ 退化，ICC=0（無法估群內變異）
    icc, _, k = cev._icc_oneway([[0.1], [0.2], [0.3]])
    assert icc == 0.0
    assert k == 3


def test_effective_n_shrinks_under_clustering():
    # 6 天、每天 20 筆強同向 → 高 ICC → n_eff 遠小於名目 n=120
    days, rr = [], []
    import random
    rng = random.Random(3)
    for di in range(6):
        base = 0.5 if di % 2 == 0 else -0.5
        for _ in range(20):
            rr.append(base + rng.gauss(0, 0.05))
            days.append(f"2026-06-{10 + di:02d}")
    neff, icc, deff, cov = cev.effective_n(rr, days)
    assert neff is not None and neff < len(rr)
    assert icc > 0.5
    assert deff > 1.0
    assert cov == 1.0


def test_effective_n_independent_is_full():
    # 每筆不同天 → 群皆 singleton → deff≈1 → n_eff≈n
    rr = [0.1 * (i % 5 - 2) for i in range(60)]
    days = [f"2026-{1 + i // 28:02d}-{1 + i % 28:02d}" for i in range(60)]
    neff, icc, deff, cov = cev.effective_n(rr, days)
    assert neff is not None and neff >= 0.8 * len(rr)
    assert deff == pytest.approx(1.0, abs=1e-6)


def test_effective_n_none_on_low_coverage():
    # 過半 day_key 缺失 → 無法可靠估 → None
    rr = [0.1] * 10
    days = [None] * 7 + ["2026-06-10", "2026-06-11", "2026-06-12"]
    neff, icc, deff, cov = cev.effective_n(rr, days)
    assert neff is None
    assert cov < 0.5


def test_effective_n_none_on_tiny():
    neff, *_ = cev.effective_n([0.1, 0.2], ["2026-06-10", "2026-06-11"])
    assert neff is None                             # n<3


def test_psr_clustered_not_above_iid():
    # 正 edge＋真實同日共振（day-shock）：n_eff<n、PSRc 不應高於 i.i.d. PSR。
    # 關鍵：ICC 量的是「同群值的相關」，非僅共用日期標籤——故每日須有共同衝擊。
    import random
    rng = random.Random(7)
    pos, days = [], []
    for di in range(6):                              # 6 天，每天 20 筆共用 day-shock
        day_effect = rng.gauss(0.4, 0.8)            # 整體正 edge，但日層共振
        for _ in range(20):
            pos.append(day_effect + rng.gauss(0, 0.1))
            days.append(f"2026-06-{10 + di:02d}")
    s = cev.ev_stats(pos, "pos", day_keys=days, role="primary")
    assert s.psr is not None and s.psr_clustered is not None
    assert s.n_eff is not None and s.n_eff < s.n    # 真叢聚 → n_eff<n
    assert s.psr_clustered <= s.psr + 1e-9


def test_judgeable_fails_when_neff_below_min():
    # 名目 n≥30 但強叢聚使 n_eff<30 → fail-closed（不可判讀）
    days, rr = [], []
    for di in range(3):                             # 僅 3 天，每天 20 筆同向
        base = 0.6 if di % 2 == 0 else -0.6
        for j in range(20):
            rr.append(base + (0.01 if j % 2 else -0.01))
            days.append(f"2026-06-{10 + di:02d}")
    s = cev.ev_stats(rr, "clustered", day_keys=days, role="primary")
    assert s.n >= cev.MIN_N
    assert s.n_eff is not None and s.n_eff < cev.MIN_N
    assert s.judgeable is False
    assert s.significant is False


def test_mag_orders_of_magnitude():
    assert cev._mag(None) == "∞"
    assert cev._mag(0) == "—"
    assert cev._mag(10000) == "~10^4"
    assert cev._mag(6450) == "~10^4"               # log10≈3.81→4
    assert cev._mag(950) == "~10^3"


# ── 載入層（合成 DB） ────────────────────────────────────────────────────────
def _make_db(path: str):
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE paper_trades (symbol TEXT, realized_r REAL, status TEXT, "
        "exit_reason TEXT, direction TEXT, regime TEXT, entry_at INTEGER)")
    con.executemany(
        "INSERT INTO paper_trades VALUES (?,?,?,?,?,?,?)",
        [
            ("BTC-USDT-SWAP", 1.5, "closed", "tp3", "long", "trend", 1781000000000),
            ("ETH-USDT-SWAP", -0.8, "closed", "stop", "short", "range", 1781100000000),
            ("SOL-USDT-SWAP", 0.0, "closed", "entry_expired", "long", "trend", 1781200000000),  # 未成交
            ("NVDA", 0.3, "closed", "tp1", "long", "trend", 1781300000000),                      # 美股
            ("XAU-USDT", 0.2, "closed", "tp1", "long", "trend", 1781400000000),                  # 代幣化
            ("DOGE-USDT-SWAP", 0.4, "open", "", "long", "trend", 1781500000000),                 # 未平倉→排除
        ])
    con.execute(
        "CREATE TABLE demo_trades (symbol TEXT, realized_r REAL, status TEXT, "
        "exit_reason TEXT, direction TEXT, regime TEXT, entry_at INTEGER)")
    con.execute("INSERT INTO demo_trades VALUES "
                "('BTC-USDT-SWAP', 0.5, 'closed', 'tp1', 'long', 'trend', 1781600000000)")
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
    assert btc.entry_ms == 1781000000000          # entry_at→entry_ms 解析
    # 分類正確
    assert {r.base: r.klass for r in paper} == {
        "BTC": "crypto", "ETH": "crypto", "SOL": "crypto",
        "NVDA": "us", "XAU": "noncrypto_token"}


def test_load_fail_soft_without_entry_at(tmp_path):
    # 缺 entry_at 欄 → entry_ms=None（fail-soft，不炸）
    db = str(tmp_path / "noeat.db")
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE paper_trades (symbol TEXT, realized_r REAL, status TEXT)")
    con.execute("INSERT INTO paper_trades VALUES ('BTC-USDT-SWAP', 1.0, 'closed')")
    con.commit()
    con.close()
    rows = cev.load_closed(db, include_demo=False)
    assert len(rows) == 1
    assert rows[0].entry_ms is None
    assert rows[0].filled is True                  # 無 exit_reason → 視為成交


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
    assert any("主結論" in l for l in labels)
    assert any("僅真成交" in l for l in labels)
    assert any("含未成交" in l for l in labels)
    assert any("代幣化" in l for l in labels)
    assert any("US" in l for l in labels)
    assert any("demo" in l for l in labels)
    # 主結論在首位＝僅真成交 crypto：BTC/ETH = 2 筆（SOL entry_expired 排除）
    primary = rep.cuts[0]
    assert primary.role == "primary"
    assert primary.n == 2
    # 敏感度A：含未成交 → BTC/ETH/SOL = 3 筆
    assert rep.cuts[1].n == 3
    # 敏感度B：＋XAU 代幣化 → 4 筆
    assert rep.cuts[2].n == 4
    # 對照：US = NVDA 1 筆，role=control
    us = rep.cuts[3]
    assert us.role == "control"
    assert us.n == 1
    # demo crypto = 1 筆
    assert rep.cuts[4].n == 1
    # 成交率：crypto paper 2/3 成交
    assert rep.fill_stats["crypto_total"] == 3
    assert rep.fill_stats["crypto_filled"] == 2
    assert rep.fill_stats["unfilled"] == 1
    # by_reason 含 entry_expired
    assert "entry_expired" in rep.by_reason


def test_render_has_honesty_banner(tmp_path):
    db = str(tmp_path / "j.db")
    _make_db(db)
    rep = cev.build_report(db, include_demo=True)
    txt = cev.render(rep)
    assert "模擬盤" in txt and "非真錢" in txt        # 紅線③ 誠實橫幅
    assert "Phase-0" in txt
    assert "未證實" in txt                            # 「未證實≠已證明無」校準語
    assert isinstance(txt, str) and len(txt) > 100


def test_render_suppresses_psr_for_small_cuts(tmp_path):
    # n<門檻切口（US n=1）→ 渲染顯示「不判讀」，不露 PSR 數字（避免接近顯著誤讀）
    db = str(tmp_path / "j.db")
    _make_db(db)
    rep = cev.build_report(db, include_demo=True)
    txt = cev.render(rep)
    assert "不判讀" in txt


def test_selftest_passes():
    assert cev._selftest() is True
