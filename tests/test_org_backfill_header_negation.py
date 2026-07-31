# -*- coding: utf-8 -*-
"""r73/v175：出處判定是純子字串比對，把「非監督員代補」判成了代補產。

實測（2026-08-01 01:20 台北，逐檔掃 docs/org/digests/）
------------------------------------------------------
被判成「監督員代補產」的四份檔裡，有一份是**該席自產**的：

    ceo-2026-07-31.md 第 5 行
    > 產出時間：2026-07-31 09:2x 台北　｜　**本檔為 CEO 席自產**（非監督員代補）

它正因為聲明自己「不是」代補產而被判成代補產。`ORG_BACKFILL_MARKER = "代補"` 只問
「檔頭有沒有這兩個字」，答不了「這句話是在承認還是在否認」——又一次拿代理值當事實。

為什麼這件事比數字難看更嚴重
----------------------------
`_read_org_digest_latest()` 用同一個判定分流「各席**最新自產**日期」，而檔齡制
（`org_digest_verdict`）是**會把 CEO state 壓成 STALLED、會進 system_faults** 的那一
支。CEO 日報節奏是每天、斷檔門檻 2 期：這個誤判讓 CEO 的自產新鮮度憑空老一天，等於
把一個**準時交件**的席次推到假斷檔前一天。它這次沒炸，只是因為差那一天。

⛔ 反方向的坑（本檔的反向護欄釘住它）
------------------------------------
不可以改成「整行出現『非』就當否定」。pm-2026-07-30.md 的檔頭是：

    **日期**：2026-07-30（週四・**監督員 Layer 2 代補產**，非 PM Session 自產）

否定詞在**後面**、修飾的是別的東西；那份是如假包換的代補產。整行判會把它洗成自產，
缺報被監督員自己的代補產蓋成痊癒——那正是這個 marker 當初存在的理由。
"""
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from l3_dispatcher.ceo_oversight import (  # noqa: E402
    ORG_HEADER_LINES, is_backfill_header, org_digest_verdict,
)
from l3_dispatcher.org_digest_coverage import coverage  # noqa: E402

# 三份真實檔頭（逐字，只截到判定行）
HEAD_CEO_SELF = (
    "# 執行總監每日彙整簡報（草稿）— 2026-07-31（五）\n"
    "\n"
    "> CEO 監督 Session · 每日 09:00 台北自動執行 · **全程唯讀**\n"
    "> 本輪零下單、零 push、零 commit、零對外送出。\n"
    "> 產出時間：2026-07-31 09:2x 台北　｜　**本檔為 CEO 席自產**（非監督員代補）\n"
)
HEAD_PM_BACKFILL = (
    "# 產品總監週報（草稿）\n"
    "\n"
    "**日期**：2026-07-30（週四・**監督員 Layer 2 代補產**，非 PM Session 自產）\n"
)
HEAD_ENG_BACKFILL = "# 高級程式設計師席｜工程盤點 2026-07-29（監督員代補產）\n"
HEAD_CG_BACKFILL = (
    "# CoinGlass 稽核官週報 2026-07-28\n"
    "\n"
    "> **本份為 CEO 代補產**。排程 session 層 7/12–7/28 無聲斷檔。\n"
)


# --------------------------------------------------------------------------
# 1. 核心：否定句不是聲明
# --------------------------------------------------------------------------
def test_naive_substring_check_gets_this_real_header_wrong():
    """差分回歸鎖：舊判法（檔頭含「代補」即算代補產）對這份真實檔頭答錯。

    有人若把 `is_backfill_header()` 改回純子字串比對，這一條會紅。
    """
    assert "代補" in HEAD_CEO_SELF          # 舊判法的答案：是代補產（錯）
    assert is_backfill_header(HEAD_CEO_SELF) is False   # 正確答案：該席自產


def test_self_produced_header_that_denies_backfill_is_not_a_backfill():
    assert is_backfill_header(HEAD_CEO_SELF) is False


def test_real_backfill_headers_are_still_detected():
    """⛔ 反向護欄：三份真的代補產一份都不准漏——漏了就是缺報被洗成痊癒。"""
    assert is_backfill_header(HEAD_PM_BACKFILL) is True
    assert is_backfill_header(HEAD_ENG_BACKFILL) is True
    assert is_backfill_header(HEAD_CG_BACKFILL) is True


def test_negation_after_the_marker_does_not_cancel_it():
    """pm 檔頭的『非』在 marker 之後、修飾別的東西——不可因此判成自產。"""
    assert is_backfill_header("**監督員 Layer 2 代補產**，非 PM Session 自產") is True


def test_mixed_header_with_one_real_declaration_wins():
    """一句否定 + 一句聲明 ⇒ 仍是代補產（寧可算代補，不可把缺報洗白）。"""
    assert is_backfill_header("（非監督員代補）\n本份為 CEO 代補產\n") is True


def test_no_marker_at_all_is_self_produced():
    assert is_backfill_header("# 一般週報\n沒有出處聲明\n") is False


def test_never_raises_on_junk():
    for junk in (None, "", "代補", "非代補", "\n\n代補\n", "非" * 50 + "代補"):
        assert isinstance(is_backfill_header(junk), bool)


# --------------------------------------------------------------------------
# 2. 後果面：誤判會讓準時交件的席次新鮮度老一天
# --------------------------------------------------------------------------
def test_misclassified_self_digest_no_longer_ages_the_role():
    """CEO 7/31 自產若被判成代補 ⇒ 最新自產退到 7/30，檔齡制看到的就是老一天。"""
    latest = date(2026, 7, 31) if not is_backfill_header(HEAD_CEO_SELF) else date(2026, 7, 30)
    assert latest == date(2026, 7, 31)
    # 節奏每天、門檻 2 期：7/31 自產 + 今天 8/01 ⇒ 不該叫斷檔。
    assert org_digest_verdict({"ceo": latest}, today=date(2026, 8, 1),
                              cadence={"ceo": ("CEO 日報", 1)}) is None


def test_coverage_counts_the_denied_header_as_a_self_hit():
    row = coverage({"ceo": [(date(2026, 7, 30), False),
                            (date(2026, 7, 31), is_backfill_header(HEAD_CEO_SELF))]},
                   today=date(2026, 8, 1), cadence={"ceo": ("CEO 日報", 1)})["roles"][0]
    assert row["self_hits"] == 2
    assert row["backfill_only_hits"] == 0


def test_header_scan_window_unchanged():
    """只掃檔頭這條口徑沒被本輪動到（正文提到別席代補產不算自己是代補）。"""
    assert ORG_HEADER_LINES == 12
