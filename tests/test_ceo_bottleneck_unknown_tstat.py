# -*- coding: utf-8 -*-
"""v231（監督員 r125）：CEO 系統自評的「當前瓶頸」——沒有 t 值時不得印成「只差真錢人工閘」。

背景（同物種第 51 次，這次落在整段自評的**結論句**）：
`_synthesize_bottleneck` 的 v150 治本堵住了「毛口徑過了、淨口徑沒過／沒證據」那條路，
但那道閘只在 `_has_t` 為真時才生效。當毛與淨**兩個口徑都拿不到 t 值**（DB 讀不到、
或該引擎沒有可檢定的已平倉樣本）時，`_has_t` 為假 ⇒ 兩道顯著性閘整個被跳過 ⇒ 只要
紙上樣本數過門檻就直接落到 `live_n < live_min` 那一支，印出

    「當前瓶頸＝紙上樣本足、待真錢人工逐筆驗證（0/30，紅線①）」

這句話斷言了「edge 沒問題、只剩人工那一下」。但真相是 edge 到底成不成立**根本沒量到**。
v150 的 docstring 自己寫得很清楚：把敘事翻成『只差真錢人工閘』＝對本人謊報一個假的
準備就緒——這裡是同一個謊，只是成因從「數據」換成「未知」。

另一半：`_paper_edge_tstat_ex` 舊版把**讀取失敗**與**真的沒樣本**都回 `(0, None)`，
兩者同形 ⇒ 下游連「該修管線還是該等樣本」都分不出來。v231 加 `with_status=True` 分辨。
"""
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from l3_dispatcher.ceo_session import _synthesize_bottleneck  # noqa: E402


# --------------------------------------------------------------- 核心行為（舊碼必敗）
def test_no_tstat_must_not_claim_only_realmoney_gate_remains():
    """兩個口徑都沒有 t 值 + 真錢閘未過 → ⛔ 不得印『待真錢人工逐筆驗證』。

    舊碼在這組輸入下會落到人工閘分支＝行為性失敗（不是 ImportError）。"""
    out = _synthesize_bottleneck(385, 100, 0, 30, 31, 0)   # paper_t / paper_t_net 都不給
    assert "待真錢人工逐筆驗證" not in out
    assert "未知" in out


def test_no_tstat_says_edge_is_unknown_not_fine():
    """措辭必須明講『未知』且點名不可讀成只差人工閘（紅線③）。"""
    out = _synthesize_bottleneck(385, 100, 0, 30, 31, 0)
    assert "edge 是否成立" in out and "只差真錢人工閘" in out


def test_unreadable_and_no_rows_are_told_apart():
    """『讀不到』與『沒樣本』的處置不同，措辭必須分得出來。"""
    unreadable = _synthesize_bottleneck(385, 100, 0, 30, 31, 0, t_status="unreadable")
    no_rows = _synthesize_bottleneck(385, 100, 0, 30, 31, 0, t_status="no_rows")
    assert "讀不出來" in unreadable and "先修讀取" in unreadable
    assert "一筆可檢定的已平倉樣本都沒有" in no_rows
    assert unreadable != no_rows


# --------------------------------------------------------------- 對照組（舊碼須通過）
def test_existing_edge_unproven_narrative_unchanged():
    """有 t 值且毛沒過 → 維持 v101 敘事（優先序沒被新分支插隊）。"""
    out = _synthesize_bottleneck(169, 100, 0, 30, 20, 0,
                                 paper_t=1.07, paper_t_net=-0.54, net_n=110)
    assert "edge 未達統計顯著" in out and "1.07" in out


def test_existing_gross_ok_net_missing_narrative_unchanged():
    """v150 那道閘不受影響。"""
    out = _synthesize_bottleneck(169, 100, 0, 30, 20, 0, paper_t=2.5)
    assert "待真錢人工逐筆驗證" not in out and "不得視為已證實" in out


def test_live_gate_met_branch_untouched():
    """live_n 已達標那一支刻意不動——原文沒有斷言 edge 成立。"""
    out = _synthesize_bottleneck(120, 100, 35, 30, 35, 0)
    assert "樣本達標" in out


def test_real_significant_case_still_reaches_human_gate():
    """毛淨都過 → 仍然歸因到真錢人工閘（新分支不可把它也擋掉）。"""
    out = _synthesize_bottleneck(169, 100, 0, 30, 20, 0,
                                 paper_t=2.5, paper_t_net=2.2, net_n=120)
    assert "待真錢人工逐筆驗證" in out and "紅線①" in out


def test_too_few_samples_branch_wins_first():
    """樣本過少仍優先——新分支不可搶在它前面。"""
    out = _synthesize_bottleneck(3, 100, 0, 30, 0, 0)
    assert "尚無足夠基礎" in out


# --------------------------------------------------------------- 來源層 status（新符號）
def _mk_db(tmp_path, rows, with_table=True):
    db = tmp_path / "tj.db"
    conn = sqlite3.connect(db)
    if with_table:
        conn.execute("CREATE TABLE paper_trades (setup TEXT, status TEXT, "
                     "exit_reason TEXT, realized_r REAL, net_r REAL)")
        for r in rows:
            conn.execute("INSERT INTO paper_trades VALUES (?,?,?,?,?)", r)
    conn.commit()
    conn.close()
    return db


def test_status_unreadable_when_table_missing(tmp_path, monkeypatch):
    from l3_dispatcher import ceo_session as cs
    db = _mk_db(tmp_path, [], with_table=False)
    monkeypatch.setattr(cs, "_TJ_DB", str(db))
    n, t, st = cs._paper_edge_tstat_ex("paper_trades", with_status=True)
    assert (n, t) == (0, None)
    assert st == "unreadable"          # ⛔ 不是 no_rows：表根本不在＝讀不到


def test_status_no_rows_when_table_empty(tmp_path, monkeypatch):
    from l3_dispatcher import ceo_session as cs
    db = _mk_db(tmp_path, [])
    monkeypatch.setattr(cs, "_TJ_DB", str(db))
    n, t, st = cs._paper_edge_tstat_ex("paper_trades", with_status=True)
    assert (n, t, st) == (0, None, "no_rows")


def test_status_ok_and_default_shape_unchanged(tmp_path, monkeypatch):
    from l3_dispatcher import ceo_session as cs
    rows = [("deepdive", "closed", "", 1.0, 0.9),
            ("deepdive", "closed", "", -0.5, -0.6),
            ("deepdive", "closed", "", 0.3, 0.2)]
    db = _mk_db(tmp_path, rows)
    monkeypatch.setattr(cs, "_TJ_DB", str(db))
    n, t, st = cs._paper_edge_tstat_ex("paper_trades", setup="deepdive", with_status=True)
    assert n == 3 and t is not None and st == "ok"
    # ⛔ 預設回傳形狀必須仍是兩元組（既有呼叫端不受影響）
    assert len(cs._paper_edge_tstat_ex("paper_trades", setup="deepdive")) == 2
    assert cs._paper_edge_tstat("paper_trades", setup="deepdive") is not None
