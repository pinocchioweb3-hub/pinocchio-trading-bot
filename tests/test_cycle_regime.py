# -*- coding: utf-8 -*-
"""週期/部位層(shadow) 純函式測試：cycle_regime 分類 + cycle_session 卡片組裝。
影子鐵則斷言：不得 import strength/fire/下單；輸出必帶誠實免責。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from l3_dispatcher.cycle_regime import (
    classify_cycle_phase, mayer_multiple, drawdown_from_ath_pct, DISCLAIMER,
)
from l3_dispatcher import cycle_session


def test_mayer_and_drawdown_basic():
    assert mayer_multiple(60000, 75000) == 0.8
    assert mayer_multiple(60000, 0) is None          # 缺料→None
    assert drawdown_from_ath_pct(63000, 126000) == -50.0


def test_deep_value_zone():
    r = classify_cycle_phase(price=16000, ma200d=32000, ma200w=18000, ath=69000)
    assert r["value_zone"] == "deep_value"
    assert r["phase"] == "markdown/accumulation"
    assert r["confluence_n"] >= 3
    assert DISCLAIMER in r["disclaimer"]


def test_euphoria_zone_distribution():
    r = classify_cycle_phase(price=100000, ma200d=31000, ma200w=45000, ath=100000)
    assert r["value_zone"] == "euphoria"
    assert r["phase"] == "distribution"


def test_missing_data_no_fabrication():
    r = classify_cycle_phase(price=60000, ma200d=76000, ma200w=0, ath=126000)
    assert r["dist_200wma_pct"] is None          # 缺 200週線→None，不臆測
    assert r["mayer"] is not None


def test_mvrv_joins_confluence():
    r = classify_cycle_phase(60000, 76000, 61000, 126000, mvrv_z=0.3)
    assert "mvrv_z<0.5" in r["signals"]


def test_card_sorts_deep_value_first_and_has_disclaimer():
    reads = [
        {"symbol": "BTC", "value_zone": "neutral", "phase": "early_markup",
         "mayer": 0.83, "dist_200wma_pct": 2.0, "drawdown_pct": -49.0,
         "confluence_n": 1, "label": "中性・復甦初段（合流 1/4）", "disclaimer": DISCLAIMER},
        {"symbol": "AAVE", "value_zone": "deep_value", "phase": "markdown/accumulation",
         "mayer": 0.62, "dist_200wma_pct": -46.0, "drawdown_pct": -81.0,
         "confluence_n": 3, "label": "深度價值帶・下跌末段/吸籌帶（合流 3/4）",
         "disclaimer": DISCLAIMER},
    ]
    card = cycle_session.build_cycle_card(reads)
    assert "深度價值帶" in card and "週期觀察" in card
    assert card.index("AAVE") < card.index("BTC")     # deep_value 排在 neutral 之前
    assert "非底點承諾" in card and "分批累積非梭哈" in card   # 誠實免責


def test_cycle_modules_are_pure_no_trading_imports():
    """影子鐵則：cycle_regime/cycle_session 不得 import 任何下單/強度/fire 模組。
    （檢 AST 實際 import，而非原始碼字眼——免責註解合法提到這些字。）"""
    import ast
    import inspect
    from l3_dispatcher import cycle_regime
    banned = ("strength", "fire_queue", "demo_trader", "demo_guard", "evaluate", "okx")
    for mod in (cycle_regime, cycle_session):
        tree = ast.parse(inspect.getsource(mod))
        imported = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported += [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                imported.append(node.module or "")
        joined = " ".join(imported).lower()
        for b in banned:
            assert b not in joined, f"{mod.__name__} 不該 import 含 {b!r} 的模組（影子層）"
