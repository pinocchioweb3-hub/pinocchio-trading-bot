# -*- coding: utf-8 -*-
"""task#7：CEO 監督 ADVANCING 改用「實質產出代理」(近期紙上活動)，非只 git commit。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from l3_dispatcher.ceo_oversight import assess, STALL_SEC


def _base(**kw):
    d = dict(now_ms=1_000_000, paper_n=50, paper_min=100, live_n=0, live_min=30,
             demo_n=5, demo_live=1, demo_active=True, open_decisions=0, pending_outbox=0)
    d.update(kw)
    return d


def test_recent_output_advances_despite_stale_commit():
    # 無關/久無 commit，但引擎近期有紙上產出 → ADVANCING（不謊報停滯）
    v = assess(commit_age_sec=STALL_SEC + 9999, real_output_age_sec=10, **_base())
    assert v["state"] == "ADVANCING"


def test_both_stale_is_stalled():
    v = assess(commit_age_sec=STALL_SEC + 1, real_output_age_sec=STALL_SEC + 1, **_base())
    assert v["state"] == "STALLED"


def test_recent_commit_still_advances():
    v = assess(commit_age_sec=10, real_output_age_sec=STALL_SEC + 1, **_base())
    assert v["state"] == "ADVANCING"


def test_blockers_win_over_advancing():
    v = assess(commit_age_sec=10, real_output_age_sec=10, **_base(open_decisions=1))
    assert v["state"] == "BLOCKED_ON_USER"


def test_both_none_idle():
    v = assess(commit_age_sec=None, real_output_age_sec=None, **_base())
    assert v["state"] == "IDLE"


def test_backward_compat_commit_only():
    # 不傳 real_output（舊呼叫）→ 退回純 commit 判斷（向後相容）
    assert assess(commit_age_sec=10, **_base())["state"] == "ADVANCING"
    assert assess(commit_age_sec=STALL_SEC + 1, **_base())["state"] == "STALLED"


def test_verdict_exposes_real_output_age():
    v = assess(commit_age_sec=10, real_output_age_sec=42, **_base())
    assert v["real_output_age_sec"] == 42
