# -*- coding: utf-8 -*-
"""task#83(B) 雙向顯示：render_paper_summary 統計裸奔治本測試。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import l3_dispatcher.paper_journal as pj
from l3_dispatcher.paper_journal import render_paper_summary, _wilson_ci

STATS = {"n_closed": 42, "n_open": 3, "win_rate_pct": 42.9, "avg_r": -0.02,
         "r_std": 1.2, "window_days": 30, "stage0_progress": "42/100", "total_pnl_usd": -264}
BASE_BITS = ["已平 <code>42</code>", "勝率 <code>42.9%</code>",
             "期望值 <code>-0.02R</code>", "PnL $-264"]


def _set_mode(monkeypatch, mode):
    import botconfig
    monkeypatch.setattr(botconfig, "get_str",
                        lambda k, d="": mode if k == "DISPLAY_MODE" else d)


def test_novice_adds_plain_caveat(monkeypatch):
    _set_mode(monkeypatch, "novice")
    out = render_paper_summary(STATS)
    assert "不代表未來" in out and "非真錢" in out
    for b in BASE_BITS:
        assert b in out  # 既有數字不變


def test_expert_adds_ci_and_threshold(monkeypatch):
    _set_mode(monkeypatch, "expert")
    out = render_paper_summary(STATS)
    assert "95%CI" in out and "n=42" in out
    for b in BASE_BITS:
        assert b in out


def test_expert_adds_ev_ci(monkeypatch):
    # task#4：EV 也帶信賴區間（avg_r=-0.02、r_std=1.2、n=42 → 區間含0＝未證實正期望值）
    _set_mode(monkeypatch, "expert")
    out = render_paper_summary(STATS)
    assert "EV 95%CI" in out and "含0" in out


def test_parity_numbers_identical(monkeypatch):
    # 兩模式的「既有數字」逐位元相同；只有附註不同（parity 不變量）
    _set_mode(monkeypatch, "novice")
    nov = render_paper_summary(STATS)
    _set_mode(monkeypatch, "expert")
    exp = render_paper_summary(STATS)
    nov_base = nov.split("\n🔰")[0]
    exp_base = exp.split("\n🎓")[0]
    assert nov_base == exp_base  # 計畫/績效數字逐位元一致


def test_small_n_shows_not_significant(monkeypatch):
    _set_mode(monkeypatch, "expert")
    out = render_paper_summary({**STATS, "n_closed": 12})
    assert "未達顯著門檻" in out


def test_large_n_no_threshold_note(monkeypatch):
    _set_mode(monkeypatch, "expert")
    out = render_paper_summary({**STATS, "n_closed": 60, "win_rate_pct": 55.0})
    assert "未達顯著門檻" not in out and "n=60" in out


def test_empty_no_note(monkeypatch):
    _set_mode(monkeypatch, "novice")
    out = render_paper_summary({**STATS, "n_closed": 0, "n_open": 0})
    assert "尚無紀錄" in out and "🔰" not in out


def test_wilson_ci_wide_for_small_n():
    lo, hi = _wilson_ci(18, 42)  # ~42.9%
    assert 0.0 <= lo < 0.43 < hi <= 1.0 and (hi - lo) > 0.25  # 小樣本區間很寬=誠實


def test_wilson_ci_clamps_domain():
    # v83(6) 縱深防禦：k>n（p>1）不得丟 complex 而崩；夾回 [0,1]
    lo, hi = _wilson_ci(50, 42)
    assert 0.0 <= lo <= 1.0 and 0.0 <= hi <= 1.0
    lo2, hi2 = _wilson_ci(-3, 42)
    assert 0.0 <= lo2 <= 1.0 and 0.0 <= hi2 <= 1.0
