# -*- coding: utf-8 -*-
"""r73/v175：期別覆蓋率把「本期還沒到期」當成「本期沒交」。

實測起點（2026-08-01 01:04 台北時間，帳本 generated_at 00:45 那一份）
--------------------------------------------------------------------
帳本 org_coverage 對 CEO 日報寫著「近 12 期自產 2/12（缺 10 期）」，缺報清單裡最後
一格是 `["2026-07-31", "2026-08-01", "全無"]`。但 CEO 日報排程是每天 09:08 跑
（cron `0 9 * * *`，list_scheduled_tasks 實查 nextRunAt=2026-08-01T01:07Z），也就是
說產出時間還在**八小時之後**——那一格不可能有檔，卻已經被記成一期缺報。

這是「未知 vs 確認沒有」的同一物種（r53–r57 在真錢路徑上收斂過五處）：期別視窗的
第 0 格右端就是「今天」，這一期**還沒走完**。沒有產出時，事實是「尚未到期」，不是
「沒交」。拿沒考完的試卷當不及格，數字每天都會錯：

  • CEO 節奏是每天、09:08 才跑 ⇒ 每天 00:00–09:08 這 9 小時，缺報數必定被灌水 +1；
  • `longest_miss_streak` 由新到舊算，最新那格恆為缺 ⇒ 連缺數永遠 ≥1。一席就算天天
    準時交，帳本也會長期寫著「缺 1 期、最長連缺 1 期」＝慢性假警報，正是
    `org_coverage_verdict` docstring 自己警告過、也是 r66 memguard 噪音的同一教訓。

⛔ 只有「這一格沒有自產檔」才轉成 pending。已經交了的那格是**確定的事實**，照算 hit
   ——修法不可對稱地把今天的產出也一起排除掉（那會變成另一個方向的失真）。
⛔ pending 那一格不可以就這樣消失：它要以 `pending_period` 留在該席資料列裡（帳本會
   原樣夾帶 roles），否則就從「記錯」變成「看不見」。
⛔ 這不會讓「今天真的沒交」逃掉：現況停擺本來就由檔齡制（org_digest_verdict）負責，
   期別制只管已經結束的期別。
"""
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from l3_dispatcher.ceo_oversight import org_coverage_verdict  # noqa: E402
from l3_dispatcher.org_digest_coverage import coverage  # noqa: E402

TODAY = date(2026, 8, 1)
CAD_CEO = {"ceo": ("CEO 日報", 1)}
CAD_PM = {"pm": ("產品總監週報", 7)}


def _ceo(*days, cadence=None):
    """days＝該席自產檔的日期（2026 年 7/8 月的日號，>=32 視為 8 月）。"""
    ds = [(date(2026, 8, d - 31) if d > 31 else date(2026, 7, d), False) for d in days]
    return coverage({"ceo": ds}, today=TODAY,
                    cadence=cadence or CAD_CEO)["roles"][0]


# --------------------------------------------------------------------------
# 1. 核心：沒到期的那一期不算缺報
# --------------------------------------------------------------------------
def test_current_period_not_yet_due_is_not_counted_as_missed():
    """真實情境：CEO 7/30、7/31 都交了，8/01 的班還沒到（09:08 才跑）。"""
    row = _ceo(30, 31)
    assert row["missed"] == 0, f"未到期的今天被記成缺報：{row['missed_windows']}"


def test_healthy_daily_role_raises_no_alarm_before_todays_run():
    """天天準時交的席次，在今天那一班跑之前不可以被寫成有缺報（慢性假警報）。"""
    assert org_coverage_verdict(coverage(
        {"ceo": [(date(2026, 7, d), False) for d in range(21, 32)]},
        today=TODAY, cadence=CAD_CEO)) is None


def test_pending_period_does_not_inflate_longest_miss_streak():
    """7/28、7/29 兩期真的沒交、7/30 交了、今天未到期 ⇒ 最長連缺是 2 不是 3。"""
    row = _ceo(25, 26, 27, 30, 31)
    assert row["longest_miss_streak"] == 2


def test_denominator_excludes_the_unfinished_period():
    """分母要是「已結束的期別數」——未走完的那期不該進 12 期的回看窗。

    7/21–7/31 天天交（11 份）＝回看窗 12 格裡有 11 格已結束、第 0 格還沒到期。
    """
    row = _ceo(*range(21, 32))
    assert row["periods"] == 11
    assert row["coverage_text"] == "11/11"


# --------------------------------------------------------------------------
# 2. pending 不可以就這樣消失（記錯 → 看不見，只是換一種失真）
# --------------------------------------------------------------------------
def test_pending_period_is_surfaced_in_the_row():
    row = _ceo(30, 31)
    assert row.get("pending_period") is not None, "未到期的那一期整個不見了"
    start, end, why = row["pending_period"]
    assert (start, end) == ("2026-07-31", "2026-08-01")
    assert "尚未到期" in why


def test_pending_period_is_absent_once_the_period_is_satisfied():
    """今天已經交了 ⇒ 那一格是確定的事實，不是 pending。"""
    assert _ceo(31, 32).get("pending_period") is None


# --------------------------------------------------------------------------
# 3. ⛔ 反向護欄：不可趁機把真的缺報也一起吃掉
# --------------------------------------------------------------------------
def test_period_already_delivered_today_still_counts_as_a_hit():
    row = _ceo(31, 32)
    assert row["self_hits"] >= 1
    assert ("2026-07-31", "2026-08-01", "全無") not in row["missed_windows"]


def test_closed_gaps_are_still_counted_in_full():
    """創意總監真實史（7/08 交、7/15 與 7/22 零產出、7/29 交）＝本模組存在的理由。"""
    row = coverage({"design": [(date(2026, 7, 8), False), (date(2026, 7, 29), False)]},
                   today=TODAY, cadence={"design": ("創意總監週報", 7)})["roles"][0]
    assert row["missed"] == 2
    assert row["longest_miss_streak"] == 2


def test_weekly_role_with_a_real_current_gap_is_still_reported():
    """pm 自產停在 7/06 ⇒ 已結束的那幾期照樣是缺報，不因本輪修法而消失。"""
    row = coverage({"pm": [(date(2026, 7, 6), False)]}, today=TODAY,
                   cadence=CAD_PM)["roles"][0]
    assert row["missed"] == 2                    # (7/11,7/18] 與 (7/18,7/25]
    assert row["pending_period"][:2] == ("2026-07-25", "2026-08-01")


def test_reporting_a_period_late_is_the_deliberate_cost():
    """⛔ 這條是刻意的代價，不是漏洞——別把它「修」回去。

    視窗是滾動的（今天往回切 cad 天），本模組刻意不吃 cron 的星期錨點（換排程日才
    不會整排錯位）。代價：週報席的班可能已經在這一格裡跑過而沒交（pm 的 cron 是週一，
    7/27 實測有觸發、無產出），本模組卻看不出來，要等這一格走完才記上。
    這符合本模組一貫的「寧可晚叫不可誤報」，而且**現況停擺另有人管**——檔齡制
    （org_digest_verdict）對 pm 早就在報 26 天未自產。少報的只有「最新一期」，
    不是整段歷史。
    """
    row = coverage({"pm": [(date(2026, 7, 6), False)]}, today=TODAY,
                   cadence=CAD_PM)["roles"][0]
    assert row["pending_period"] is not None      # 事實仍在帳本上，只是不記缺
    assert row["missed"] == 2                     # 而非把最新那格也算進去的 3


def test_backfill_in_the_pending_period_is_not_promoted_to_a_hit():
    """代補產補的是內容不是排程——就算今天有代補產檔，該席仍是「尚未自產」。"""
    row = coverage({"ceo": [(date(2026, 7, 30), False), (date(2026, 8, 1), True)]},
                   today=TODAY, cadence=CAD_CEO)["roles"][0]
    assert row.get("pending_period") is not None
    assert "代補產" in row["pending_period"][2]


# --------------------------------------------------------------------------
# 4. 不誤報守則照舊（壞形狀、空輸入不得製造假故障或例外）
# --------------------------------------------------------------------------
def test_role_that_only_ever_delivered_today_is_not_reported_as_missing():
    """今天才上線的席次：上線前的期別不追溯記過，今天這期已交 ⇒ 零缺報。"""
    row = coverage({"ceo": [(date(2026, 8, 1), False)]}, today=TODAY,
                   cadence=CAD_CEO)["roles"][0]
    assert row["missed"] == 0
    assert row["periods"] == 1


def test_empty_inputs_never_raise_nor_fabricate():
    assert coverage({}, today=TODAY)["roles"] == []
    assert coverage(None, today=TODAY)["roles"] == []
    assert coverage({"ceo": []}, today=TODAY, cadence=CAD_CEO)["roles"] == []
