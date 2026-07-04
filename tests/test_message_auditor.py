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


# ── v82(2)：旗艦訊號流分類缺口治本 ──
DEEPDIVE = "🎯 <b>SOL 交易計畫深度分析</b>\n做多 進場 $140 止損 $135 倉位配置（R 計）"
US_SIGNAL = "🧪🇺🇸 <b>MSTR 永續</b>［美股突破·實驗性］\n✓ 突破 24h 高 做多"


def test_deepdive_not_unknown():
    # 深度分析卡含「倉位配置（R」但 deepdive 強特徵在 fire 之前 → 正確歸 deepdive
    assert infer_msg_kind(DEEPDIVE) == "deepdive"


def test_us_signal_not_unknown():
    assert infer_msg_kind(US_SIGNAL) == "us_signal"


def test_flagship_kinds_get_route_check():
    # 不再落 unknown→不再被 route 全豁免（deepdive/us_signal 有 KIND_TO_TOPICS 映射）
    assert check_route("trade", "deepdive") == "ok"
    assert check_route("trade", "us_signal") == "ok"
    assert check_route("news", "us_signal").startswith("MISROUTE")  # 錯主題會被抓


def test_us_signal_direction_guarded():
    # us_signal 納入方向護欄：缺方向會被抓
    no_dir = "🧪🇺🇸 MSTR 永續［美股突破·實驗性］ 量 $4m"
    assert "trade_msg_no_direction" in check_clarity(no_dir, "us_signal")


# ------------------------------------------------- v115 合成健康留痕 + 401-stdout 防呆
def test_synth_health_record_roundtrip(monkeypatch, tmp_path):
    import json as _json
    import botpaths as _bp
    monkeypatch.setattr(_bp, "data_dir", lambda: tmp_path)
    from l3_dispatcher.synthesizer import _record_synth_health
    _record_synth_health(False, "claude exit=1: x")
    _record_synth_health(False, "claude exit=1: y")
    st = _json.loads((tmp_path / "synth_health.json").read_text(encoding="utf-8"))
    assert st["consecutive_failures"] == 2 and "y" in st["last_error"]
    _record_synth_health(True)
    st2 = _json.loads((tmp_path / "synth_health.json").read_text(encoding="utf-8"))
    assert st2["consecutive_failures"] == 0 and st2["last_error"] == ""


def test_auth_failure_markers_present():
    """401 走 stdout 且 exit=0 的實測樣態必須被標記字樣涵蓋（不可當分析結果送出）。"""
    from l3_dispatcher.synthesizer import _AUTH_FAIL_MARKERS
    sample = "Failed to authenticate. API Error: 401 Invalid authentication credentials"
    assert any(m in sample for m in _AUTH_FAIL_MARKERS)
