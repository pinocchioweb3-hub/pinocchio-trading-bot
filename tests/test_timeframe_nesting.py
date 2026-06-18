"""多時框趨勢嵌套（timeframe_nesting）純函式核心測試 — task#34。

執行方式（任一）：
    pytest tests/test_timeframe_nesting.py
    python tests/test_timeframe_nesting.py

全離線、合成 K 線（不打任何 API）。17 案，涵蓋：
    單層 up/down/range/unknown；build_nesting 全同向 / 部分翻向 / 全 range /
    缺層容錯 / 空 dict 安全降級；detect_false_break 真假 / 大層反向加成 / OI 加權；
    infer_stage 7 階段決策表；classify_trade_side 左/右側。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from market_intel_mcp.timeframe_nesting import (
    STAGE_LABELS,
    TF_ORDER,
    build_nesting,
    classify_tf_trend,
    classify_trade_side,
    detect_false_break,
    infer_stage,
)


# ===========================================================================
# 合成 K 線工廠
# ===========================================================================

def _synth(direction: str, n: int, base: float = 100.0, step: float = 1.0) -> list[dict]:
    """產生合成 OHLCV（升序，欄位同 okx_candles）。

        direction='up'   → HH/HL 階梯（每根收高）
        direction='down' → LH/LL 階梯（每根收低）
        direction='range'→ 鋸齒（一上一下，無淨位移）
    """
    candles: list[dict] = []
    price = float(base)
    for i in range(n):
        if direction == "up":
            o = price
            c = price + step
            h = c + step * 0.3
            l = o - step * 0.2
        elif direction == "down":
            o = price
            c = price - step
            h = o + step * 0.2
            l = c - step * 0.3
        else:  # range：鋸齒
            up = (i % 2 == 0)
            o = price
            c = price + step if up else price - step
            h = max(o, c) + step * 0.3
            l = min(o, c) - step * 0.3
        candles.append({
            "ts": 1000 + i, "open": o, "high": h, "low": l, "close": c,
            "volume": 100.0, "volume_usd": 100.0 * c, "confirm": True,
        })
        price = c
    return candles


def _candle(o, h, l, c, v=100.0):
    return {"ts": 1, "open": o, "high": h, "low": l, "close": c,
            "volume": v, "volume_usd": v * c, "confirm": True}


def _flat_base(n: int = 8, level: float = 100.0, span: float = 1.0, v: float = 100.0):
    """近水平的區間段（給假突破當前置背景）。"""
    return [_candle(level, level + span, level - span, level, v=v) for _ in range(n)]


# ===========================================================================
# 1-4: 單層趨勢分類
# ===========================================================================

def test_single_layer_up():
    r = classify_tf_trend(_synth("up", 40))
    assert r["direction"] == "up"
    assert r["last_swing"] in ("HH", "HL")
    assert r["strength"] >= 50
    assert r["change_pct"] > 0
    assert 0.0 <= r["price_position"] <= 1.0


def test_single_layer_down():
    r = classify_tf_trend(_synth("down", 40))
    assert r["direction"] == "down"
    assert r["last_swing"] in ("LH", "LL")
    assert r["change_pct"] < 0


def test_single_layer_range():
    r = classify_tf_trend(_synth("range", 40))
    assert r["direction"] == "range"
    assert r["strength"] <= 35  # 區間天花板


def test_single_layer_unknown_insufficient():
    # 資料 < lookback+5 → unknown
    r = classify_tf_trend(_synth("up", 10), lookback=20)
    assert r["direction"] == "unknown"
    assert r["price_position"] is None
    # 空輸入也安全
    assert classify_tf_trend([])["direction"] == "unknown"


# ===========================================================================
# 5-9: build_nesting
# ===========================================================================

def test_nesting_all_aligned_up():
    cbt = {tf: _synth("up", 40) for tf in TF_ORDER}
    n = build_nesting(cbt)
    assert n["dominant_trend"] == "up"
    assert n["agreement_depth"] == n["layer_count"] == len(TF_ORDER)
    assert n["divergence_tf"] is None
    assert n["alignment_score"] >= 0.99  # 近 1
    assert n["stage_code"] == "UP_TREND"


def test_nesting_htf_up_ltf_down_divergence():
    # 月/週/日 up；12h/8h/4h down → 第一個翻向在 12h、深度 3
    cbt = {
        "1M": _synth("up", 40), "1w": _synth("up", 40), "1d": _synth("up", 40),
        "12h": _synth("down", 40), "8h": _synth("down", 40), "4h": _synth("down", 40),
    }
    n = build_nesting(cbt)
    assert n["dominant_trend"] == "up"
    assert n["divergence_tf"] == "12h"
    assert n["agreement_depth"] == 3
    # 大層上、小層下 → 高位回調 或 見頂待確認（依小層位置）
    assert n["stage_code"] in ("UP_PULLBACK", "TOP_WATCH")
    assert n["stage_label"] == STAGE_LABELS[n["stage_code"]]


def test_nesting_all_range():
    cbt = {tf: _synth("range", 40) for tf in TF_ORDER}
    n = build_nesting(cbt)
    assert n["dominant_trend"] == "range"
    assert n["stage_code"] == "RANGE"


def test_nesting_htf_down_ltf_up_left_side_bounce():
    # 大層 down + 小層 up → 低位反彈 + trade_side=left
    cbt = {
        "1M": _synth("down", 40), "1w": _synth("down", 40), "1d": _synth("down", 40),
        "12h": _synth("up", 40), "8h": _synth("up", 40), "4h": _synth("up", 40),
    }
    n = build_nesting(cbt)
    assert n["dominant_trend"] == "down"
    assert n["stage_code"] == "DOWN_BOUNCE"
    assert n["trade_side"]["side"] == "left"


def test_nesting_missing_and_error_layers_skipped():
    # 部分層缺、部分層標 error、部分層資料不足 → 跳過不崩
    cbt = {
        "1M": _synth("up", 40),
        "1w": {"error": True, "message": "boom"},     # error dict
        "1d": _synth("up", 8),                          # 資料不足 → unknown 被略
        "12h": {"candles": _synth("up", 40)},          # 包在 dict 內也吃
        # 8h / 4h 缺
    }
    n = build_nesting(cbt)
    tfs = [ly["tf"] for ly in n["layers"]]
    assert tfs == ["1M", "12h"]          # 只剩兩個有效層
    assert n["dominant_trend"] == "up"
    assert n["layer_count"] == 2


def test_nesting_empty_dict_safe_unknown():
    n = build_nesting({})
    assert n["dominant_trend"] == "unknown"
    assert n["layers"] == []
    assert n["agreement_depth"] == 0
    assert n["divergence_tf"] is None
    assert n["trade_side"]["side"] == "neutral"
    # None 輸入也不崩
    assert build_nesting(None)["dominant_trend"] == "unknown"


# ===========================================================================
# 10-13: detect_false_break
# ===========================================================================

def test_false_break_pierce_and_reject_long_wick():
    # 突破上沿後收回 + 長上影 → True + side=up
    candles = _flat_base(8) + [_candle(100, 110, 99.5, 100.2, v=40)]
    r = detect_false_break({"4h": candles}, "4h")
    assert r["is_false_break"] is True
    assert r["side"] == "up"
    assert r["confidence"] >= 0.5
    assert any("收回" in reason for reason in r["reasons"])


def test_false_break_sustained_breakout_is_false():
    # 持續站穩 + 放量大實體收在 level 之上 → 非假突破
    candles = _flat_base(8) + [_candle(100, 106, 100, 105.5, v=300)]
    r = detect_false_break({"4h": candles}, "4h")
    assert r["is_false_break"] is False
    assert r["confidence"] == 0.0
    assert r["side"] is None


def test_false_break_against_bigger_layer_gets_bonus():
    # 大層為空，4h 出現向上假突破 → 信心被加成（高於無大層時）
    small = _flat_base(8) + [_candle(100, 110, 99.5, 100.2, v=40)]
    big_down = _synth("down", 40, base=200.0)
    with_big = detect_false_break({"1d": big_down, "4h": small}, "4h")
    without_big = detect_false_break({"4h": small}, "4h")
    assert with_big["confidence"] > without_big["confidence"]
    assert any("逆勢" in reason for reason in with_big["reasons"])


def test_false_break_oi_negative_adds_weight():
    candles = _flat_base(8) + [_candle(100, 110, 99.5, 100.2, v=40)]
    base = detect_false_break({"4h": candles}, "4h")
    with_oi = detect_false_break({"4h": candles}, "4h", oi_delta_pct=-5.0)
    assert with_oi["confidence"] > base["confidence"]
    assert with_oi["is_false_break"] is True
    assert any("減倉" in reason for reason in with_oi["reasons"])
    # 缺資料安全降級
    assert detect_false_break({"4h": []}, "4h")["is_false_break"] is False


# ===========================================================================
# 14-15: infer_stage 決策表 7 階段
# ===========================================================================

def test_infer_stage_decision_table_all_seven():
    cases = {
        "UP_TREND": ("up", "up", 0.8, 3),
        "UP_PULLBACK": ("up", "down", 0.4, 2),
        "TOP_WATCH": ("up", "down", 0.85, 2),
        "RANGE": ("range", "range", 0.5, 1),
        "BOTTOM_WATCH": ("down", "up", 0.2, 2),
        "DOWN_BOUNCE": ("down", "up", 0.6, 2),
        "DOWN_TREND": ("down", "down", 0.2, 3),
    }
    for expected_code, args in cases.items():
        s = infer_stage(*args)
        assert s["stage_code"] == expected_code, f"{args} -> {s['stage_code']} != {expected_code}"
        assert s["stage_label"] == STAGE_LABELS[expected_code]
        assert s["rationale"]


def test_infer_stage_labels_provisional_set():
    # STAGE_LABELS 是 7 階段、值為暫定中文（鎖定預期值，抽換時測試會提醒）
    assert set(STAGE_LABELS) == {
        "UP_TREND", "UP_PULLBACK", "TOP_WATCH", "RANGE",
        "BOTTOM_WATCH", "DOWN_BOUNCE", "DOWN_TREND",
    }
    assert STAGE_LABELS["UP_TREND"] == "主升段"
    assert STAGE_LABELS["DOWN_TREND"] == "主跌段"
    assert STAGE_LABELS["RANGE"] == "區間震盪"


# ===========================================================================
# 16-17: classify_trade_side 左/右側
# ===========================================================================

def test_trade_side_right_trend_following():
    # 全同向上、高對齊、小層順勢 → 右側
    cbt = {tf: _synth("up", 40) for tf in TF_ORDER}
    n = build_nesting(cbt)
    r = classify_trade_side(n)
    assert r["side"] == "right"
    assert n["trade_side"]["side"] == "right"  # build_nesting 內已掛上一致


def test_trade_side_left_counter_trend():
    # 大層 up、小層回落至低位 → 左側（順大勢低接）
    cbt = {
        "1M": _synth("up", 40), "1w": _synth("up", 40), "1d": _synth("up", 40),
        "12h": _synth("down", 40), "8h": _synth("down", 40), "4h": _synth("down", 40),
    }
    n = build_nesting(cbt)
    r = classify_trade_side(n)
    assert r["side"] == "left"
    # 無分層 → neutral
    assert classify_trade_side({})["side"] == "neutral"


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
