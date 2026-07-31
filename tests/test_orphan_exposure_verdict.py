"""孤兒部位（曝險型故障）在帳本上被講反了——v179（監督員 r77）。

背景（2026-08-01 02:32 線上實地量到）
------------------------------------
真錢消費器的 401 白名單阻塞在連續 1260 輪後，於當輪改判為 `orphan_position`：
交易所上有一筆 WLFI-USDT-SWAP long、本地部位帳沒有。`manage_positions()` 只在
**account positions 查詢成功**的輪才可能記到這個類別（查不到一律回 None），所以
類別會換本身就是「認證已經通了」的正向證據。

但帳本這一層有兩個病灶，正好在這個真實事件上同時發作：

1. **語意講反**：`orphan_position` 不在 `USER_ACTIONABLE_FAIL_CLASSES` 裡，於是掉進
   「未知類別」那一句——寫成「未下單（無金錢虧損），但管線實質停擺，須查明修復」，
   並歸進 system_faults（＝該 push CEO 改碼）。三件事全錯：交易所上那筆倉是**在場
   的真實曝險**（不是沒虧損）；管線其餘部分照常運作（只擋同幣同向）；而且只有人能
   到 OKX 確認並決定平不平，CEO 改碼救不回來（球在使用者，該進 blockers）。

2. **輪數／時長跨類別繼承**：`consecutive_fail_rounds` 與 `first_fail_ts` 是單一計數
   器，不分類別。類別一換，舊類別的 1260 輪與 22 小時被原封不動掛到新故障頭上，
   帳本會寫成「連續 1263 輪 orphan_position、已持續 22 小時」——而它其實三分鐘前才
   出現；順帶把「上一個擋點剛剛解除」這件大事整個藏起來。
   `class_counts` 是歷來累計，所以 `min(class_counts[cls], rounds)` 是本類別當前連續
   輪數可證的**上界**；上界 < rounds ⇒ 這段 streak 必然混了別的類別。

⛔ 反方向的護欄（本檔同時釘住）
------------------------------
- 沒有 `class_counts` 或該類別數字 ≥ rounds ⇒ 維持原本的輪數與時長敘述，不可因為
  「保守」就把所有故障的時長都抹掉（那會弄丟 401 這種真的持續很久的事實）。
- 白名單類仍是「未下單（無金錢虧損）」——那句話對它是誠實的，不可一起改掉。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from l3_dispatcher.ceo_oversight import assess, live_exec_verdict  # noqa: E402

NOW = 1_785_522_900.0

# 線上實地形狀（數字照 2026-08-01 02:35 的健康檔）
ORPHAN = {
    "consecutive_fail_rounds": 1263,
    "updated_at": NOW - 60,
    "last_fail_class": "orphan_position",
    "last_fail_sample": ("孤兒部位 WLFI-USDT-SWAP long 11618 張：交易所上有、"
                         "本地帳沒有。此倉不在自動管理之下……"),
    "class_counts": {"auth_ip_whitelist": 1551, "query_fail": 770,
                     "leverage_fail": 92, "orphan_position": 3},
    "first_fail_ts": NOW - 80_500,          # 22 小時前＝401 那場的起點
}
WHITELIST = {
    "consecutive_fail_rounds": 1260,
    "updated_at": NOW - 60,
    "last_fail_class": "auth_ip_whitelist",
    "class_counts": {"auth_ip_whitelist": 1551},
    "first_fail_ts": NOW - 80_500,
}


def _assess(**kw):
    base = dict(now_ms=NOW * 1000, commit_age_sec=60, paper_n=360, paper_min=100,
                live_n=0, live_min=30, demo_n=31, demo_live=1, demo_active=False,
                open_decisions=0, pending_outbox=0)
    base.update(kw)
    return assess(**base)


# --------------------------------------------------------------------------
# 1. 語意：孤兒部位是「在場曝險」，不是「沒下單所以沒事」
# --------------------------------------------------------------------------
def test_orphan_is_user_actionable_not_a_system_fault():
    v = live_exec_verdict(ORPHAN, now_s=NOW)
    assert v is not None
    assert v["user_actionable"] is True          # 只有人能到 OKX 確認並決定平不平
    a = _assess(live_exec=v)
    assert any("孤兒部位" in b for b in a["blockers"])
    assert not any("孤兒部位" in s for s in a["system_faults"])
    assert a["state"] == "BLOCKED_ON_USER"


def test_orphan_text_never_claims_no_money_at_risk():
    """舊碼的致命句：「未下單（無金錢虧損）」——對一筆在場真錢倉是把風險講反。"""
    t = live_exec_verdict(ORPHAN, now_s=NOW)["text"]
    assert "無金錢虧損" not in t
    assert "曝險" in t


def test_orphan_text_never_claims_the_whole_pipeline_is_down():
    """只擋同幣同向，其餘照跑——講「管線實質停擺」會讓人以為要去修管線。"""
    t = live_exec_verdict(ORPHAN, now_s=NOW)["text"]
    assert "管線實質停擺" not in t
    assert "同幣同向" in t


def test_orphan_text_names_the_instrument():
    """人要去 OKX 找那筆倉，帳本必須講是哪一個。"""
    t = live_exec_verdict(ORPHAN, now_s=NOW)["text"]
    assert "WLFI-USDT-SWAP" in t


# --------------------------------------------------------------------------
# 2. 輪數／時長不可跨類別繼承
# --------------------------------------------------------------------------
def test_mixed_class_streak_is_not_attributed_to_the_new_class():
    v = live_exec_verdict(ORPHAN, now_s=NOW)
    assert v["mixed_class_streak"] is True
    assert v["cls_rounds_max"] == 3             # class_counts 給的可證上界
    t = v["text"]
    # ⛔ 分寸：數字本身可以出現在「這是跨類別累計值」的警語裡（那是誠實揭露），
    #   不可出現在把它掛在本類別頭上的**斷言句**（舊碼就是這樣寫的）。
    assert "真錢執行器連續 1263 輪" not in t
    assert "22 小時" not in t and "22 時" not in t
    assert "跨" in t                             # 必須明講這是跨類別累計值


def test_mixed_class_note_says_the_previous_blocker_is_no_longer_current():
    """類別換掉＝上一個擋點已不是現在的擋點；這是本次最重要的事實，不可默默吞掉。"""
    t = live_exec_verdict(ORPHAN, now_s=NOW)["text"]
    assert "上一個" in t


# --------------------------------------------------------------------------
# 3. 反向護欄：不可弄壞既有的白名單敘述
# --------------------------------------------------------------------------
def test_whitelist_class_keeps_its_rounds_duration_and_wording():
    v = live_exec_verdict(WHITELIST, now_s=NOW)
    assert v["user_actionable"] is True
    assert v.get("mixed_class_streak") is False
    t = v["text"]
    assert "連續 1260 輪" in t
    assert "無金錢虧損" in t                     # 對這一類是誠實的
    assert "IP 白名單" in t


def test_no_class_counts_falls_back_to_the_old_wording():
    """健康檔沒有 class_counts＝無從證明混類別 ⇒ 維持現況，不製造新阻斷。"""
    h = {k: v for k, v in WHITELIST.items() if k != "class_counts"}
    v = live_exec_verdict(h, now_s=NOW)
    assert v["mixed_class_streak"] is False
    assert "連續 1260 輪" in v["text"]


def test_unknown_class_still_goes_to_system_faults():
    v = live_exec_verdict({**WHITELIST, "last_fail_class": "network"}, now_s=NOW)
    assert v["user_actionable"] is False
    a = _assess(live_exec=v)
    assert any("network" in s for s in a["system_faults"])
