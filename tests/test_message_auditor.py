# -*- coding: utf-8 -*-
"""message_auditor msg_kind 分類治本測試（v82）。

驗證呈現評估 wf 發現的三類誤標已治本，且無回歸：
  - CEO 簡報 → ceo_brief（非 econ）
  - 優化器報告 → tuner_report（非 tp_sl），且不再 cascade trade_msg_no_direction
  - OTS 錨定 / 美股永續快照 / 掛單逾時 → 各自分類（非 unknown）
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from telegram_bot.message_auditor import (
    infer_msg_kind, check_clarity, check_route,
)

# ── 真實樣本（取自 message_audit.db text_preview）──
CEO = "🧭 CEO 每日簡報 2026-06-21 09:00 台北 由 Claude Code（監督人角色）自動彙整 一切正常·今日重點 系統：全部 worker 由監督器看顧"
OPTIMIZER = "⚙️ 入場積極度自動優化器（過 L2 四關後寫入模擬盤入場政策覆寫表）掃 97 筆已平倉/逾時紙上單，符合完整重放窗 28 筆，分 21 桶｜本輪晉升 0 桶"
OTS = "帳本防竄改錨定（OpenTimestamps 比特幣）時間：2026-06-21T04:56:04Z 快照：ledger_xxx.json"
USQUOTE = "美股永續行情 — 即時快照 波動榜（24h 變化最大）mstr $118.66 +3.49%"
EXPIRY = "⚠️ ASTER 做多 掛單逾時作廢 掛單 12h 未觸及進場價（0% 成交），已自動取消（紙上，0R）"


def test_ceo_brief_not_econ():
    assert infer_msg_kind(CEO) == "ceo_brief"


def test_optimizer_not_tp_sl():
    assert infer_msg_kind(OPTIMIZER) == "tuner_report"


def test_optimizer_no_cascade_no_direction():
    # 治本核心：優化器報告(含「已平倉」但無方向)不再被誤判為交易訊息缺方向
    kind = infer_msg_kind(OPTIMIZER)
    assert "trade_msg_no_direction" not in check_clarity(OPTIMIZER, kind)


def test_ots_anchor_not_unknown():
    assert infer_msg_kind(OTS) == "anchor"


def test_usquote_not_unknown():
    assert infer_msg_kind(USQUOTE) == "usquote"


def test_expiry_not_unknown():
    assert infer_msg_kind(EXPIRY) == "expiry"


def test_new_kinds_no_false_misroute():
    # 新類不在 KIND_TO_TOPICS → check_route 一律 ok（不引入假路由警報）
    for txt, kind in [(CEO, "ceo_brief"), (OPTIMIZER, "tuner_report"),
                      (OTS, "anchor"), (USQUOTE, "usquote"), (EXPIRY, "expiry")]:
        assert check_route("intel", infer_msg_kind(txt)) == "ok"


# ── 回歸：既有分類不能壞 ──
def test_regression_existing_kinds():
    assert infer_msg_kind("🔥 BTC/USDT 做多 可立即執行 倉位配置（R 計） 進場 $65000") == "fire"
    assert infer_msg_kind("📊 每日宏觀分析 regime=bull") == "macro"
    assert infer_msg_kind("每小時即時動態 市場廣度") == "pulse"
    assert infer_msg_kind("📅 經濟數據 今日多檔代幣解鎖") == "econ"
    assert infer_msg_kind("每日績效總結 過去 7 天") == "perf"
    assert infer_msg_kind("紙上驗證事件（自動追蹤，非實倉） tao bull tp1") == "paper"
    assert infer_msg_kind("📰 美股快訊 NVDA 財報") == "usnews"


def test_real_fire_still_has_direction_check():
    # 真交易卡仍受方向檢查保護（沒回歸把保護拿掉）
    no_dir = "🔥 BTC 可立即執行 倉位配置（R） 進場 $65000"  # 缺方向
    assert "trade_msg_no_direction" in check_clarity(no_dir, "fire")
