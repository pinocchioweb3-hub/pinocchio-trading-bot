# -*- coding: utf-8 -*-
"""熊底合流儀表板（v111）：分數/負對照/重正規化/免責/影子純度。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from l3_dispatcher.bottom_confluence import (
    DISCLAIMERS, TOTAL_MASS, compute_bottom_score, render_dashboard_block,
)


def test_2022_11_deep_bottom_fixture_scores_high():
    """2022-11 型深底（MVRV-Z<0/價<已實現/Mayer0.7破200週/dominance升/深BTC季/F&G18）→ ≥80。"""
    r = compute_bottom_score({"mvrv_z": -0.2, "price": 16000, "realized_price": 19800,
                              "mayer": 0.7, "dist_200wma_pct": -12,
                              "dominance_dir_90d": True, "altseason_idx": 18, "fng_avg30": 18})
    assert r["score"] >= 80 and "歷史極值合流" in r["band"]


def test_2018_12_bottom_fixture_scores_accumulation():
    """2018-12 型底（Z 略>0 半亮、其餘多亮）→ 至少落深度價值帶(≥60)。"""
    r = compute_bottom_score({"mvrv_z": 0.3, "price": 3200, "realized_price": 4200,
                              "mayer": 0.55, "dist_200wma_pct": 2,
                              "dominance_dir_90d": True, "altseason_idx": 15, "fng_avg30": 22})
    assert r["score"] >= 60


def test_2021_04_bull_top_negative_control():
    """牛頂負對照（設計驗收硬標準）：2021-04 型輸入必須 <15 分，否則設計失敗。"""
    r = compute_bottom_score({"mvrv_z": 6.5, "price": 60000, "realized_price": 20000,
                              "mayer": 1.5, "dist_200wma_pct": 250,
                              "dominance_dir_90d": False, "altseason_idx": 80, "fng_avg30": 75})
    assert r["score"] < 15, f"牛頂負對照失敗：{r['score']}"


def test_renormalize_by_present_mass():
    """缺料重正規化：只有 MVRV-Z(全亮) 有資料 → 分數=100 但 present_mass 低、不出 band。"""
    r = compute_bottom_score({"mvrv_z": -0.5})
    assert r["score"] == 100.0
    assert r["present_mass_pct"] < 60 and r["band"] is None    # 資料不足不下結論
    assert "不可與歷史比較" in r["note"]


def test_all_missing_honest_none():
    r = compute_bottom_score({})
    assert r["score"] is None and r["band"] is None


def test_disclaimers_always_present_and_complete():
    """六條免責必在（含對抗審查補的『指標選擇偏差』與 2022-06 假底反例）。"""
    r = compute_bottom_score({"mvrv_z": 0.0})
    text = " ".join(r["disclaimers"])
    assert len(r["disclaimers"]) == len(DISCLAIMERS) == 6
    assert "指標選擇偏差" in text and "2022-06" in text and "紅線①" in text


def test_render_includes_score_and_disclaimer_hint():
    r = compute_bottom_score({"mvrv_z": -0.2, "price": 1, "realized_price": 2,
                              "mayer": 0.5, "dist_200wma_pct": -5,
                              "dominance_dir_90d": True, "altseason_idx": 10, "fng_avg30": 10})
    out = render_dashboard_block(r, {"DXY(廣義,n=1)": "120 3月-2%↓走弱"}, {"🪙": "x"})
    assert "熊底合流儀表板" in out and "核心分數" in out
    assert "不計分" in out          # 背景/疊加皆標不計分
    assert "區間非底點" in out       # 卡上免責提示


def test_weights_sum():
    assert TOTAL_MASS == 75.0       # A40 + C20 + D15（B總經=0 背景）


def test_shadow_purity_no_trading_imports():
    """影子鐵則：bottom_confluence/bottom_feeds 不得 import 下單/強度/fire 模組。"""
    import ast
    import inspect
    from l3_dispatcher import bottom_confluence, bottom_feeds
    banned = ("strength", "fire_queue", "demo_trader", "demo_guard", "evaluate", "okx")
    for mod in (bottom_confluence, bottom_feeds):
        tree = ast.parse(inspect.getsource(mod))
        imported = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported += [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                imported.append(node.module or "")
        joined = " ".join(imported).lower()
        for b in banned:
            assert b not in joined, f"{mod.__name__} 不該 import 含 {b!r} 的模組"
