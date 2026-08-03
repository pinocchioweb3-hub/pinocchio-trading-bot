# -*- coding: utf-8 -*-
"""r130/v236：自產報告只要在檔頭「提到上一份是代補產」，就會被判成代補產。

實測（2026-08-03 10:2x 台北，跑帳本自己的 `_read_org_digest_latest()`）
--------------------------------------------------------------------
    self_latest['pm'] = 2026-07-06      ← 28 天前
    bf_latest['pm']   = 2026-08-03      ← 當天 09:44 那份被歸成「代補產」

但 `docs/org/digests/pm-2026-08-03.md` 是 PM 席**自產**的，檔頭第 3 行寫得明明白白：

    **日期**：2026-08-03（週一）｜**本席自產**（非監督員代補）

它栽在第 5 行——交代沿革時提到**上一份**：

    **涵蓋區間**：2026-07-30 ～ 08-03（上一份為 `pm-2026-07-30.md`，
                  該份為監督員代補產；本席自產最新為 `pm-2026-07-06`）

「該份為監督員代補產」講的是別份檔、前面沒有否定詞 ⇒ r73 的窄窗否定判法命中 ⇒
整份自產檔被歸成代補產。r73 治好了「否定句被當成聲明」，沒治「講別人被當成講自己」。

為什麼這一次特別難看
--------------------
r123 起連續四輪把組織排程結案為「已修待驗——代補產只補內容不算痊癒，要等該席
**自產**檔落地才算」。8/03 09:44 那份自產檔真的落地了，是四輪來等的那個驗收訊號；
帳本卻在 09:54 仍印「產品總監週報自產最新為 28 天前……該席排程仍未自產＝未驗收」。
⇒ **驗收訊號被驗收工具自己吃掉**，且會慢性化：往後每一份會交代沿革的自產報告都會
再踩一次，這條 system_fault 結構上永遠關不掉。

⛔ 反方向的護欄（本檔一併釘住）
------------------------------
不可以改成「整行提到別的日期／別的檔名就跳過」：真代補產常會寫明自己在補哪一期
（「本檔為監督員代補產，補 2026-07-20 那一期」），整行判會把它洗成自產＝缺報被
監督員自己的代補產蓋成痊癒，那正是這個 marker 當初存在的理由（見 r73 檔）。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from l3_dispatcher.ceo_oversight import (  # noqa: E402
    ORG_BACKFILL_LOOKBEHIND, ORG_BACKFILL_MARKER, is_backfill_header,
)

# pm-2026-08-03.md 的真實檔頭（逐字，只截到出事那一行）
HEAD_PM_SELF_CITING_PRIOR_BACKFILL = (
    "# 皮諾丘交易訊號專案 — 產品週報（草稿）\n"
    "\n"
    "**日期**：2026-08-03（週一）｜**本席自產**（非監督員代補）\n"
    "**作者**：產品總監 Session（排程自動執行）— 全程唯讀，**不對外送出**。\n"
    "**涵蓋區間**：2026-07-30 ～ 08-03（上一份為 `pm-2026-07-30.md`，"
    "該份為監督員代補產；本席自產最新為 `pm-2026-07-06`）\n"
)

# 真代補產（r73 已釘住的三份，本次改動不得動搖）
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
# 1. 核心：講別份不是講自己
# --------------------------------------------------------------------------
def test_self_produced_digest_citing_a_prior_backfill_is_not_a_backfill():
    """差分回歸鎖：改動前這份**自產**檔頭會被判成代補產（行為性紅，非 import 錯）。"""
    # 前提複核：它確實同時具備「自產聲明」與「提到別份是代補產」兩句
    assert "**本席自產**（非監督員代補）" in HEAD_PM_SELF_CITING_PRIOR_BACKFILL
    assert "該份為監督員代補產" in HEAD_PM_SELF_CITING_PRIOR_BACKFILL
    assert is_backfill_header(HEAD_PM_SELF_CITING_PRIOR_BACKFILL) is False


def test_otherdoc_referent_sits_inside_the_narrow_lookbehind_window():
    """釘住「窄窗夠用」這個前提——它是本修法不必放寬窗的理由。

    窗一旦被放寬，隔壁子句的詞會被吃進來 ⇒ 少判代補產 ⇒ 缺報被蓋成痊癒。
    """
    line = "該份為監督員代補產"
    i = line.find(ORG_BACKFILL_MARKER)
    ctx = line[max(0, i - ORG_BACKFILL_LOOKBEHIND):i]
    assert "該份" in ctx, f"指涉詞落在窄窗外（ctx={ctx!r}）＝本修法的前提不成立"


# --------------------------------------------------------------------------
# 2. 反方向護欄：真代補產不可被洗成自產
# --------------------------------------------------------------------------
def test_real_backfills_still_detected():
    assert is_backfill_header(HEAD_PM_BACKFILL) is True
    assert is_backfill_header(HEAD_ENG_BACKFILL) is True
    assert is_backfill_header(HEAD_CG_BACKFILL) is True


def test_backfill_that_names_the_period_it_fills_is_still_a_backfill():
    """真代補產寫明自己在補哪一期 ⇒ 仍須判成代補產。

    ⛔ 這條擋住「整行提到別的日期就跳過」那種寫法——那會把缺報洗成痊癒。
    """
    head = "**日期**：2026-08-03｜**本檔為監督員代補產**，補 `pm-2026-07-20` 那一期\n"
    assert is_backfill_header(head) is True


def test_self_declaration_before_the_marker_still_wins():
    """r73 的既有行為不得回退：否定詞在前 ⇒ 自產。"""
    assert is_backfill_header("> **本檔為 CEO 席自產**（非監督員代補）\n") is False


# --------------------------------------------------------------------------
# 3. 永不拋
# --------------------------------------------------------------------------
def test_never_raises_on_junk():
    for junk in ("", None, "代補", "該份", "\n\n\n", "該份為監督員代補產" * 40):
        assert isinstance(is_backfill_header(junk), bool)
