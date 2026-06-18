"""弱勢評分模組（weakness.py）測試 — 全離線、零 API、純合成資料。

執行方式（任一）：
    pytest tests/test_weakness.py
    python tests/test_weakness.py

涵蓋 10 案：
    1. 空輸入 → []。
    2. 單元素 → z=0、weakness_score≈50。
    3. 排序：A(大跌+OI增) > B(小跌) > C(上漲)。
    4. OI 象限：同跌幅，OI 增者分 > OI 減者。
    5. 誠實不假對稱：weakness_contrib 不含三個 stub 欄位。
    6. z-score clip：極端離群被夾 ±3。
    7. passes_short_liquidity：低於門檻者不通過。
    8. 輸入不可變（input immutability）。
    9. 全相同輸入 → 所有 weakness_score 相等（≈50）。
   10. 強弱對偶 sanity：最強幣不應同時最弱。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from market_intel_mcp.weakness import (  # noqa: E402
    WEAKNESS_WEIGHTS,
    compute_weakness_scores,
    passes_short_liquidity,
)

STUB_FIELDS = ("cvd_slope_7d", "top_trader_dev", "btc_corr_30d")


def _mk(symbol, ret, oi, vol, **extra):
    """合成一筆幣資料。"""
    return {
        "symbol": symbol,
        "return_7d_pct": ret,
        "oi_delta_7d_pct": oi,
        "vol_24h_vs_30d": vol,
        **extra,
    }


def test_empty_input_returns_empty():
    assert compute_weakness_scores([]) == []


def test_single_element_neutral_50():
    out = compute_weakness_scores([_mk("BTC", -10.0, 5.0, 2.0)])
    assert len(out) == 1
    # 單元素 → 每因子值池 len<2 → z=0 → composite=0 → sigmoid(0)=50
    assert abs(out[0]["weakness_score"] - 50.0) < 1e-6
    assert all(abs(c) < 1e-9 for c in out[0]["weakness_contrib"].values())


def test_ordering_weak_to_strong():
    # A：大跌 + OI 大增（新空進場）→ 最弱
    # B：小跌 → 中
    # C：上漲 → 最不弱
    a = _mk("A", -30.0, 40.0, 3.0)
    b = _mk("B", -5.0, 0.0, 1.0)
    c = _mk("C", 25.0, -10.0, 0.5)
    out = compute_weakness_scores([b, c, a])  # 故意亂序輸入
    order = [x["symbol"] for x in out]
    assert order.index("A") < order.index("B") < order.index("C")
    assert out[0]["symbol"] == "A"


def test_oi_quadrant_same_drop():
    # 兩幣同跌幅，OI 增者（新空）應比 OI 減者（空回補）更弱
    rising = _mk("RISE_OI", -20.0, 50.0, 1.0)
    falling = _mk("FALL_OI", -20.0, -50.0, 1.0)
    out = compute_weakness_scores([rising, falling])
    s = {x["symbol"]: x["weakness_score"] for x in out}
    assert s["RISE_OI"] > s["FALL_OI"]


def test_honest_no_fake_symmetry_contrib_keys():
    # 即使輸入帶三個 stub 欄位，contrib 也只有 3 個真因子 key
    item = _mk("BTC", -10.0, 10.0, 2.0,
               cvd_slope_7d=0.123, top_trader_dev=0.456, btc_corr_30d=0.7)
    out = compute_weakness_scores([item, _mk("ETH", 5.0, -3.0, 0.8)])
    for row in out:
        keys = set(row["weakness_contrib"].keys())
        assert keys == set(WEAKNESS_WEIGHTS.keys())
        for stub in STUB_FIELDS:
            assert stub not in row["weakness_contrib"]


def test_zscore_clip_at_three():
    # 一大群緊密常態值 + 單一極端離群 → 離群幣的 raw z 遠超 3，須被夾到恰 ±3。
    # 用大量點讓母體標準差由「常態群」主導，離群才能真正衝破 3 sigma。
    universe = [_mk(f"N{i}", float(i % 5) - 2.0, 0.0, 0.0) for i in range(200)]
    universe.append(_mk("OUT", -1_000_000.0, 0.0, 0.0))  # 暴跌離群
    out = compute_weakness_scores(universe)
    row = next(r for r in out if r["symbol"] == "OUT")
    # return 取負 z，離群往下 → 負 z → 取負後為正，夾在 +3，乘權重 0.45
    contrib = row["weakness_contrib"]["return_7d_pct"]
    bound = 3.0 * WEAKNESS_WEIGHTS["return_7d_pct"]
    assert abs(contrib) <= bound + 1e-9           # 不超界
    assert abs(contrib - bound) < 1e-6            # 確實被夾到上限（clip 生效）


def test_passes_short_liquidity_threshold():
    big = {"symbol": "BTC", "vol_24h_usd": 5_000_000_000.0}
    small = {"symbol": "TINY", "vol_24h_usd": 50_000.0}
    missing = {"symbol": "NOVOL"}  # 缺值 → 視為 0 → 不過
    assert passes_short_liquidity(big, 1_000_000.0) is True
    assert passes_short_liquidity(small, 1_000_000.0) is False
    assert passes_short_liquidity(missing, 1_000_000.0) is False
    # 邊界：恰等於門檻 → 通過（>=）
    assert passes_short_liquidity({"vol_24h_usd": 1_000_000.0}, 1_000_000.0) is True


def test_input_immutability():
    src = [_mk("A", -10.0, 5.0, 2.0), _mk("B", 3.0, -1.0, 0.5)]
    import copy
    snapshot = copy.deepcopy(src)
    compute_weakness_scores(src)
    assert src == snapshot  # 原輸入未被改動
    # 原 dict 不應被注入新欄位
    for d in src:
        assert "weakness_score" not in d
        assert "weakness_contrib" not in d


def test_all_identical_scores_equal_50():
    universe = [_mk(f"S{i}", -7.0, 4.0, 1.5) for i in range(6)]
    out = compute_weakness_scores(universe)
    scores = {r["weakness_score"] for r in out}
    assert len(scores) == 1
    assert abs(out[0]["weakness_score"] - 50.0) < 1e-6


def test_strength_weakness_duality_sanity():
    # 同一 universe 跑 strength 與 weakness，最強幣不應同時最弱。
    # 設計清楚分歧：BULL = 乾淨多頭（大漲 + OI 建倉 + 量平穩）；
    # BEAR = 乾淨崩壞（大跌 + 新空進場 OI 增 + 放量出貨）。
    from market_intel_mcp.strength import compute_strength_scores
    universe = [
        _mk("BULL", 35.0, 20.0, 1.0),    # 漲多、OI 增（多頭續命）→ strength 高、weakness 低
        _mk("BEAR", -35.0, 20.0, 4.0),   # 跌多、OI 增（新空）、放量 → weakness 高、strength 低
        _mk("MID1", 2.0, 0.0, 1.0),
        _mk("MID2", -2.0, 0.0, 1.0),
    ]
    s_out = compute_strength_scores([dict(u) for u in universe])
    w_out = compute_weakness_scores([dict(u) for u in universe])
    strongest = s_out[0]["symbol"]
    weakest = w_out[0]["symbol"]
    assert strongest != weakest
    assert strongest == "BULL"
    assert weakest == "BEAR"


# --- 直接執行（無 pytest 也能跑）---
if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
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
