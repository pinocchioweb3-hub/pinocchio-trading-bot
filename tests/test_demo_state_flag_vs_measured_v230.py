# -*- coding: utf-8 -*-
"""v230 / 監督員 r124：CEO 日報「🧪 模擬盤實單」的狀態欄不得拿環境旗標當量測結果。

「拿代理值當事實」同物種第 50 次，且是 **v167 治了一半**的那一半：v167 把 ledger 的
demo_active 從 is_active()（純環境旗標）改成 demo_activity_verdict()（實測輪次結果），
卻漏了 CEO 日報這個消費端 ⇒ 同一份資料在兩處講相反的話。

線上實測（2026-08-03）：旗標 True、最近一輪 105 秒前、outcome=skipped:demo_guard:…
⇒ 舊碼印「運行中」，實際自 7/29 起每輪被擋、零新單、在場部位停止對帳。

正向（改動前應為紅）：停擺／未知時不得出現「運行中」，且必須具名原因、標記帳面已停更。
反向（改動前即為綠）：真的在跑時仍給乾淨的「運行中」、旗標沒開仍是「待命」，不冒未知字樣。
"""
import pytest

from l3_dispatcher.ceo_session import demo_state_text


# --------------------------------------------------------------------------
# 正向：這些在改動前的碼上會失敗（舊碼只看旗標，一律印「運行中」）
# --------------------------------------------------------------------------
def test_被閘擋住不得印運行中並須具名原因():
    """線上此刻的真實情形：旗標開著，但每輪被 demo_guard 擋。"""
    reason = ("skipped:demo_guard:偵測到實盤交易金鑰已設定：['OKX_TRADE_API_KEY']。"
              "模擬盤模式下 OKX_TRADE_* 必須全空。")
    text, stalled = demo_state_text({"active": False, "reason": reason})
    assert "運行中" not in text
    assert "停擺" in text
    assert "demo_guard" in text          # 原因具名，不得摘要成健康字眼
    assert stalled is True               # 帳面數字須被標成舊值


def test_迴圈沒在轉不得印運行中():
    text, stalled = demo_state_text({"active": False, "reason": "stale"})
    assert "運行中" not in text
    assert "停擺" in text
    assert stalled is True


def test_讀不出來要講未知而不是講停了():
    """未知不可壓成確認——兩個方向都不行。"""
    text, stalled = demo_state_text({"active": False, "reason": "unknown"})
    assert "運行中" not in text
    assert "未知" in text
    assert "不是沒問題" in text
    assert stalled is True


def test_停擺時原因過長仍須截斷但不得吞掉():
    long_reason = "skipped:demo_guard:" + "字" * 300
    text, _ = demo_state_text({"active": False, "reason": long_reason})
    assert "運行中" not in text
    assert "demo_guard" in text
    assert len(text) < 160               # 不讓一行爆掉整份日報


def test_空的verdict不得被讀成運行中():
    """fail-closed：拿不到量測結果時，預設不是健康。"""
    for bad in ({}, None, {"reason": None}):
        text, stalled = demo_state_text(bad)
        assert "運行中" not in text
        assert "未知" in text
        assert stalled is True


# --------------------------------------------------------------------------
# 反向：這些在改動前即為綠——證明沒有矯枉過正
# --------------------------------------------------------------------------
def test_真的在跑時仍給乾淨的運行中():
    text, stalled = demo_state_text({"active": True, "reason": "ran"})
    assert text == "運行中"
    assert "未知" not in text and "停擺" not in text
    assert stalled is False              # 在跑＝帳面是現況，不加舊值警語


def test_旗標沒開仍是待命且不算停擺():
    """沒開＝設定狀態，不是故障，也沒有『帳面變舊』可言。"""
    text, stalled = demo_state_text({"active": False, "reason": "flag_off"})
    assert "待命" in text
    assert "DEMO_OPERATOR_ACTIVE" in text
    assert "停擺" not in text and "未知" not in text
    assert stalled is False
