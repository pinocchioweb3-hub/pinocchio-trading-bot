"""convergence 純函式測試（task#33）— 離線、零網路。

執行（任一）：
    pytest tests/test_convergence.py
    python tests/test_convergence.py

涵蓋：direction_of 各情境（含 funding 反號 / None / 中性帶）；
metric_convergence 3 源同號 / 2 多 1 空 / 平手 / 僅 1 源；
strength_multiplier 夾 [0.8,1.2] 且 0.5→≈1.0；
aggregate 全共振高分 / 零共振≈1.0；input immutability。
"""
from __future__ import annotations

import copy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from l3_dispatcher.convergence import (
    aggregate_convergence,
    direction_of,
    metric_convergence,
    strength_multiplier,
)


# ---------------------------------------------------------------------------
# direction_of
# ---------------------------------------------------------------------------
def test_direction_price_basic():
    assert direction_of("price", 1.5) == 1
    assert direction_of("price", -0.3) == -1
    assert direction_of("price", 0.0) == 0


def test_direction_funding_inverted():
    # funding 反號：負 funding = 偏多燃料 → +1
    assert direction_of("funding", -0.0008) == 1
    assert direction_of("funding", 0.0008) == -1
    # funding 變體名也走反號
    assert direction_of("funding_8h", -0.01) == 1
    assert direction_of("funding_rate", 0.01) == -1


def test_direction_none_and_nonnumeric():
    assert direction_of("oi", None) == 0
    assert direction_of("price", "x") == 0
    assert direction_of("cvd", None) == 0


def test_direction_neutral_band():
    assert direction_of("price", 0.05, neutral_band=0.1) == 0    # 帶內 → 0
    assert direction_of("price", 0.2, neutral_band=0.1) == 1     # 帶外 → 方向
    assert direction_of("price", -0.2, neutral_band=0.1) == -1
    # funding 帶內也 0，帶外才反號
    assert direction_of("funding", 0.0, neutral_band=0.0001) == 0


# ---------------------------------------------------------------------------
# metric_convergence
# ---------------------------------------------------------------------------
def test_convergence_three_agree():
    r = metric_convergence("price", {"okx": 1, "binance": 1, "coinglass": 1})
    assert r["agree_dir"] == 1
    assert r["n_agree"] == 3
    assert r["n_present"] == 3
    assert r["agreement_ratio"] == 1.0
    assert r["is_convergent"] is True


def test_convergence_two_vs_one():
    # 2 多 1 空：ratio = 2/3 ≈ 0.667 ≥ 0.6 → 共振，方向多
    r = metric_convergence("oi", {"okx": 1, "binance": 1, "coinglass": -1})
    assert r["agree_dir"] == 1
    assert r["n_agree"] == 2
    assert r["n_present"] == 3
    assert r["is_convergent"] is True


def test_convergence_tie_not_convergent():
    # 1 多 1 空：平手 → 無主流方向，非共振
    r = metric_convergence("cvd", {"okx": 1, "binance": -1})
    assert r["agree_dir"] == 0
    assert r["is_convergent"] is False


def test_convergence_single_source_not_convergent():
    # 僅 1 源有方向：n_agree=1 < 2 → 非共振（單所不算共振）
    r = metric_convergence("funding", {"okx": 1, "binance": 0, "coinglass": 0})
    assert r["n_present"] == 1
    assert r["n_agree"] == 1
    assert r["is_convergent"] is False


def test_convergence_ratio_at_and_below_threshold():
    # 3 多 2 空：ratio = 3/5 = 0.6 → 恰好門檻，含等於 → 共振
    r = metric_convergence(
        "price",
        {"a": 1, "b": 1, "c": 1, "d": -1, "e": -1})
    assert r["agreement_ratio"] == 0.6
    assert r["is_convergent"] is True
    # 4 多 3 空：ratio = 4/7 ≈ 0.571 < 0.6 → 雖有多數方向但比例不足 → 非共振
    r2 = metric_convergence(
        "price",
        {"a": 1, "b": 1, "c": 1, "d": 1, "e": -1, "f": -1, "g": -1})
    assert r2["agree_dir"] == 1
    assert r2["agreement_ratio"] < 0.6
    assert r2["is_convergent"] is False


def test_convergence_empty_input():
    r = metric_convergence("price", {})
    assert r["n_present"] == 0
    assert r["agreement_ratio"] == 0.0
    assert r["is_convergent"] is False


def test_metric_convergence_input_immutability():
    sig = {"okx": 1, "binance": 1, "coinglass": -1}
    snap = copy.deepcopy(sig)
    _ = metric_convergence("price", sig)
    assert sig == snap


# ---------------------------------------------------------------------------
# strength_multiplier — SHADOW 專用，夾 [0.8,1.2]，0.5→≈1.0
# ---------------------------------------------------------------------------
def test_strength_multiplier_anchor_at_half():
    # convergence=0.5 → ≈1.0（與 presence 無關）
    assert strength_multiplier(0.5, 1.0) == 1.0
    assert strength_multiplier(0.5, 0.0) == 1.0
    assert strength_multiplier(0.5, 0.5) == 1.0


def test_strength_multiplier_caps():
    assert strength_multiplier(1.0, 1.0) == 1.2     # 封頂
    assert strength_multiplier(0.0, 1.0) == 0.8     # 封底


def test_strength_multiplier_always_in_range():
    for conv in (0.0, 0.25, 0.5, 0.75, 1.0, -5.0, 5.0):
        for pres in (0.0, 0.2, 0.5, 1.0, -1.0, 2.0):
            m = strength_multiplier(conv, pres)
            assert 0.8 <= m <= 1.2


def test_strength_multiplier_presence_scales_offset():
    # 同 convergence，presence 高 → 偏移較大（更偏離 1.0）
    high = strength_multiplier(1.0, 1.0)
    low = strength_multiplier(1.0, 0.2)
    assert high > low >= 1.0


# ---------------------------------------------------------------------------
# aggregate_convergence
# ---------------------------------------------------------------------------
def _conv(metric, sig):
    return metric_convergence(metric, sig)


def test_aggregate_all_convergent_high_score():
    presence = {"presence_score": 1.0, "triple_present": True}
    mr = {
        "price": _conv("price", {"okx": 1, "binance": 1, "coinglass": 1}),
        "oi": _conv("oi", {"okx": 1, "binance": 1, "coinglass": 1}),
        "funding": _conv("funding", {"okx": 1, "binance": 1, "coinglass": 1}),
    }
    out = aggregate_convergence("RENDER", mr, presence)
    assert set(out["convergent_metrics"]) == {"price", "oi", "funding"}
    assert out["n_convergent"] == 3
    assert out["dominant_direction"] == 1
    assert out["convergence_score"] == 1.0          # 全共振、ratio 全 1.0
    assert out["strength_multiplier"] == 1.2        # 高分 + presence 1.0 → 封頂
    assert out["triple_present"] is True


def test_aggregate_zero_convergent_neutral():
    presence = {"presence_score": 0.8, "triple_present": False}
    mr = {
        "price": _conv("price", {"okx": 1, "binance": -1}),     # 平手 → 非共振
        "oi": _conv("oi", {"okx": 1}),                          # 單源 → 非共振
    }
    out = aggregate_convergence("FOO", mr, presence)
    assert out["convergent_metrics"] == []
    assert out["n_convergent"] == 0
    assert out["dominant_direction"] == 0
    assert out["convergence_score"] == 0.0
    # 零共振 → multiplier ≈ 1.0（conv=0 但封底是 0.8；conv=0 → offset=(0-0.5)*0.4*0.8）
    # 注意：convergence_score=0 不是 0.5，故會略低於 1.0，但仍 ≥0.8
    assert 0.8 <= out["strength_multiplier"] <= 1.0


def test_aggregate_neutral_when_score_half():
    # 構造 convergence_score≈0.5：2 指標中 1 共振(ratio1.0) → coverage0.5×1.0=0.5
    presence = {"presence_score": 1.0, "triple_present": True}
    mr = {
        "price": _conv("price", {"okx": 1, "binance": 1, "coinglass": 1}),  # 共振 ratio1
        "oi": _conv("oi", {"okx": 1, "binance": -1}),                       # 平手 非共振
    }
    out = aggregate_convergence("BAR", mr, presence)
    assert out["convergence_score"] == 0.5
    assert out["strength_multiplier"] == 1.0         # 0.5 錨點 → 中性


def test_aggregate_dominant_direction_bear():
    presence = {"presence_score": 0.9, "triple_present": True}
    mr = {
        "price": _conv("price", {"okx": -1, "binance": -1, "coinglass": -1}),
        "oi": _conv("oi", {"okx": -1, "binance": -1, "coinglass": 1}),
    }
    out = aggregate_convergence("XYZ", mr, presence)
    assert out["dominant_direction"] == -1
    assert out["n_convergent"] == 2


def test_aggregate_empty_metrics():
    out = aggregate_convergence("EMPTY", {}, {"presence_score": 0.0,
                                              "triple_present": False})
    assert out["n_convergent"] == 0
    assert out["convergence_score"] == 0.0
    assert out["dominant_direction"] == 0
    assert 0.8 <= out["strength_multiplier"] <= 1.2


def test_aggregate_input_immutability():
    presence = {"presence_score": 1.0, "triple_present": True}
    mr = {"price": _conv("price", {"okx": 1, "binance": 1, "coinglass": 1})}
    mr_snap = copy.deepcopy(mr)
    pres_snap = copy.deepcopy(presence)
    _ = aggregate_convergence("RENDER", mr, presence)
    assert mr == mr_snap
    assert presence == pres_snap


def test_aggregate_missing_presence_score_safe():
    # presence 缺 presence_score → 視為 0，不崩
    mr = {"price": _conv("price", {"okx": 1, "binance": 1, "coinglass": 1})}
    out = aggregate_convergence("Z", mr, {})
    assert 0.8 <= out["strength_multiplier"] <= 1.2
    assert out["triple_present"] is False


# --- 直接執行（無 pytest 也能跑）---
if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v) and v.__name__.startswith("test_")]
    passed = 0
    for fn in fns:
        try:
            fn()
            print(f"  PASS  {fn.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"  FAIL  {fn.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            print(f"  ERROR {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{passed}/{len(fns)} passed")
    sys.exit(0 if passed == len(fns) else 1)
