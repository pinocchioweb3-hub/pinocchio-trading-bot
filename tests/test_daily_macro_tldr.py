# -*- coding: utf-8 -*-
"""task#4：每日宏觀『一句話結論』前置（巨牆漸進揭露最小安全落地）。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import l3_dispatcher.macro as macro
from l3_dispatcher.macro import _daily_macro_tldr


def test_tldr_with_label_and_score(monkeypatch):
    monkeypatch.setattr(macro, "_read_macro_confluence_score", lambda: 2.3)
    out = _daily_macro_tldr({"regime_advice": {"label": "中性", "color": "⚪"}})
    assert "一句話" in out and "Regime" in out and "中性" in out and "+2.3" in out
    assert "影子" in out  # 誠實標示非進場訊號


def test_tldr_no_score_omits_score(monkeypatch):
    monkeypatch.setattr(macro, "_read_macro_confluence_score", lambda: None)
    out = _daily_macro_tldr({"regime_advice": {"label": "偏多", "color": "🟢"}})
    assert "偏多" in out and "綜合宏觀" not in out  # 缺料誠實省略，不杜撰


def test_tldr_empty_state_returns_blank():
    assert _daily_macro_tldr({}) == ""
    assert _daily_macro_tldr(None) == ""
    assert _daily_macro_tldr({"regime_advice": {}}) == ""
