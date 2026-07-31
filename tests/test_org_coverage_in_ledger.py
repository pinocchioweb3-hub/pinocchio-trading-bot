# -*- coding: utf-8 -*-
"""r71/v173：組織產出「期別覆蓋率」接進監督帳本。

為什麼要有這一層
----------------
現行 `org_digest_verdict()` 是**檔齡制**：只看各席「最新一份自產 digest 有多舊」。
任何一份新檔都會把它前面的歷史缺口一起蓋掉。真實例子（2026-07-31 人工清點才發現）：
創意總監 7/08 交過、7/15 與 7/22 兩期零產出、7/29 又交一次——檔齡制看到「最新才
2 天前」判無斷檔，那兩期缺報**從未上過任何一輪帳本**。

本檔釘住三件事：
  1. 新的 `org_coverage_verdict()` 看得見那兩期（差分回歸鎖：退化回檔齡制就會紅）。
  2. 它被 `assess()` 原樣帶進帳本欄位 ⇒ Layer 2 每輪讀帳本就看得到，不必逐檔清點。
  3. ⛔ 它**不**進 system_faults、**不**改 CEO 狀態——歷史缺口是既成事實不是現況故障；
     已經結束的缺口若把 state 壓成 STALLED 長達 12 期＝慢性假警報，最後沒人理
     （r66 memguard 噪音稀釋訊號的同一個教訓）。現況停擺仍由檔齡制負責。
"""
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from l3_dispatcher.ceo_oversight import (  # noqa: E402
    assess, org_coverage_verdict, org_digest_verdict, STALL_SEC,
)
from l3_dispatcher.org_digest_coverage import coverage  # noqa: E402

TODAY = date(2026, 8, 1)
CAD_DESIGN = {"design": ("創意總監週報", 7)}
# 創意總監真實史：7/08 自產 → 7/15、7/22 兩期零產出 → 7/29 自產。
DESIGN_HIST = {"design": [(date(2026, 7, 8), False), (date(2026, 7, 29), False)]}


def _cov(hist=None, cadence=None):
    return coverage(hist if hist is not None else DESIGN_HIST,
                    today=TODAY, cadence=cadence or CAD_DESIGN)


def _base(**kw):
    d = dict(now_ms=1_000_000_000, commit_age_sec=10, real_output_age_sec=10,
             paper_n=50, paper_min=100, live_n=0, live_min=30,
             demo_n=5, demo_live=1, demo_active=True,
             open_decisions=0, pending_outbox=0)
    d.update(kw)
    return d


# --------------------------------------------------------------------------
# 1. 差分回歸鎖——這一層存在的唯一理由
# --------------------------------------------------------------------------
def test_age_based_check_is_blind_to_closed_gap():
    """檔齡制看不見已被新檔蓋掉的歷史缺口（這是現況，不是 bug——本檔要補的就是它）。"""
    assert org_digest_verdict({"design": date(2026, 7, 29)}, today=TODAY,
                              cadence=CAD_DESIGN) is None


def test_coverage_verdict_sees_the_two_missed_periods():
    v = org_coverage_verdict(_cov())
    assert v is not None
    row = v["roles"][0]
    assert row["role"] == "design"
    assert row["missed"] == 2
    assert row["longest_miss_streak"] == 2
    assert "2/4" in v["text"]


def test_gap_invisible_to_age_check_is_flagged_as_hidden():
    v = org_coverage_verdict(_cov(), exclude_roles=[])
    assert v["any_hidden_gap"] is True
    assert v["hidden_from_age_check"] == ["design"]


def test_gap_already_reported_by_age_check_is_not_double_counted():
    """該席若已在檔齡制的斷檔清單裡，覆蓋率仍保留數據（完整），但不算「新發現」。"""
    v = org_coverage_verdict(_cov(), exclude_roles=["design"])
    assert v is not None
    assert [r["role"] for r in v["roles"]] == ["design"]   # 數據不刪
    assert v["any_hidden_gap"] is False                     # 但不重複當新故障
    assert v["hidden_from_age_check"] == []


# --------------------------------------------------------------------------
# 2. 接進帳本
# --------------------------------------------------------------------------
def test_assess_carries_org_coverage_into_snapshot():
    v = assess(org_coverage=org_coverage_verdict(_cov()), **_base())
    assert v["org_coverage"] is not None
    assert v["org_coverage"]["roles"][0]["role"] == "design"


def test_assess_without_org_coverage_still_has_the_key():
    """欄位恆存在（值為 None）——Layer 2 才分得出「沒缺報」與「這版沒這功能」。"""
    assert assess(**_base())["org_coverage"] is None


# --------------------------------------------------------------------------
# 3. ⛔ 不得改判定（反向護欄）
# --------------------------------------------------------------------------
def test_org_coverage_never_enters_system_faults():
    v = assess(org_coverage=org_coverage_verdict(_cov()), **_base())
    assert v["system_faults"] == []
    assert v["blockers"] == []


def test_org_coverage_never_changes_state():
    v = assess(org_coverage=org_coverage_verdict(_cov()), **_base())
    assert v["state"] == "ADVANCING"


def test_org_digest_age_check_still_does_change_state():
    """⛔ 現況停擺（檔齡制）必須照舊進 system_faults 並壓成 STALLED——不可被本輪弄壞。"""
    v = assess(org_digest={"text": "組織產出斷檔：測試"}, **_base())
    assert v["system_faults"] == ["組織產出斷檔：測試"]
    assert v["state"] == "STALLED"


# --------------------------------------------------------------------------
# 4. 兩條不誤報守則（沿用 org_digest_verdict 口徑）
# --------------------------------------------------------------------------
def test_no_gap_returns_none():
    # 每個期別視窗都要落到一份自產檔（視窗是 (today-(k+1)*cad, today-k*cad]）。
    full = {"eng": [(date(2026, 8, 1), False)]
                   + [(date(2026, 7, d0), False) for d0 in (11, 18, 25)]}
    cov = coverage(full, today=TODAY, cadence={"eng": ("高級程式設計師週報", 7)})
    assert org_coverage_verdict(cov) is None


def test_empty_and_none_never_fabricate_a_fault():
    assert org_coverage_verdict(None) is None
    assert org_coverage_verdict({}) is None
    assert org_coverage_verdict({"roles": []}) is None


def test_garbage_shape_never_raises():
    """帳本任何一塊都不准把 daemon 弄掛——壞形狀回 None，不拋。"""
    for junk in ({"roles": None}, {"roles": [{}]}, {"roles": [{"missed": "x"}]},
                 {"roles": "not-a-list"}, {"unexpected": 1}):
        try:
            org_coverage_verdict(junk)
        except Exception as e:                                # pragma: no cover
            raise AssertionError(f"org_coverage_verdict 對 {junk!r} 拋了 {e!r}")


def test_backfill_only_period_is_shown_separately():
    """監督員代補產只補內容不補排程——不可把缺報洗成有交。

    r73 註：代補產日期由 7/30 改為 7/20。原因不是為了讓測試變綠——7/30 落在
    「(7/25, 8/01]」這一格，而那一格的右端就是今天＝**尚未走完**，r73 起不再計為
    缺報／代補產期，改以 `pending_period` 呈現（見 test_org_coverage_pending_period）。
    本測試要釘的是「代補產不得被算成該席自產有交」這件事，把日期挪進一格**已結束**
    的視窗才測得到它；同一件事在 pending 那一格另有專門測試把關。
    """
    hist = {"pm": [(date(2026, 7, 6), False), (date(2026, 7, 20), True)]}
    v = org_coverage_verdict(coverage(hist, today=TODAY,
                                      cadence={"pm": ("產品總監週報", 7)}))
    assert v["roles"][0]["backfill_only_hits"] == 1
    assert "代補產" in v["text"]


def test_stall_sec_import_still_available():
    """守住既有匯入面（本輪只做加法）。"""
    assert isinstance(STALL_SEC, (int, float))
