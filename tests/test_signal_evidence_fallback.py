# -*- coding: utf-8 -*-
"""task#4(B) FIRE 證據 fallback：未知訊號不倒 raw dict（開發者殘渣外洩治本）。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from telegram_bot.message_format import _fmt_signal_evidence


def test_unknown_signal_no_raw_dict():
    out = _fmt_signal_evidence({"name": "brand_new_sig",
                                "evidence": {"foo": 1, "bar": 2, "secret_internal": 3}})
    assert "{" not in out and "foo" not in out and "secret_internal" not in out


def test_unknown_signal_uses_name():
    out = _fmt_signal_evidence({"name": "brand_new_sig", "evidence": {}})
    assert "brand_new_sig" in out and "/指標" in out


def test_known_label_in_fallback():
    # atr_coiling 無專屬 formatter→走 fallback，應用 _SIGNAL_LABEL 中文名而非原名
    out = _fmt_signal_evidence({"name": "atr_coiling", "evidence": {}})
    assert "ATR 收斂" in out and "{" not in out


def test_specific_formatter_still_works():
    # 有專屬 formatter 的不受影響
    out = _fmt_signal_evidence({"name": "funding",
                                "evidence": {"funding": 0.0001, "regime": "neutral"}})
    assert "8h" in out and "中性" in out


# ── v83(6)：孿生函式 intent_format._signal_note 同樣不得倒 raw dict（可執行 JSON 出口）──
def test_intent_note_unknown_no_raw_dict():
    from telegram_bot.intent_format import _signal_note
    out = _signal_note({"name": "brand_new_sig",
                        "evidence": {"foo": 1, "secret_internal": 9}})
    assert "{" not in out and "foo" not in out and "secret_internal" not in out


def test_intent_note_uses_shared_label():
    from telegram_bot.intent_format import _signal_note
    out = _signal_note({"name": "atr_coiling", "evidence": {}})
    assert "ATR 收斂" in out and "{" not in out
