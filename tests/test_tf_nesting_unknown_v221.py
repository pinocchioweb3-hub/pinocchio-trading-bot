"""v221（監督員 r115）：多時框嵌套「這輪沒算出來」不再折成「確認是盤整・對齊 0%」。

同物種第 41 次，首次落在**復盤紀錄的學習欄位**上。

破口（改動前 HEAD 實測）：
    market_intel_mcp/timeframe_nesting.py:526-538 —— 當所有時框都取不到 K 線
    （或每一層 candles 都不足 lookback+5 根而回 direction='unknown'）時，
    build_nesting() 不回「未知」，而是回一個**看起來算完了的完整答案**：

        stage / stage_code / stage_label = 'RANGE' / '盤整'
        alignment_score                  = 0.0

    這兩個值隨即被 l3_dispatcher/macro.py:_deepdive_extra_context()（:909/:911）
    寫進 plan_snapshot 的 nest_stage_code / nest_alignment_pct，而那兩欄正是
    plan_snapshot.py:46-47 宣告要拿去做「復盤 regime-conditioned 分桶」的學習欄位。

為什麼這裡要緊：
    「這幣當時是盤整、各層完全不對齊」與「那一刻我們根本沒抓到任何時框的 K 線」
    對優化器是**相反**的樣本——前者是一個 regime 分桶的觀測值，後者是缺料，
    只能不計入。舊碼把後者寫成前者，且 plan_snapshot 那一格看起來是有值的，
    復盤時**永遠分不出來**（stage_rationale 寫著「無有效分層 → 預設區間」是誠實的，
    但沒有任何消費端讀它）。plan_snapshot.py:45 自己的規格就寫著「缺料→None 誠實留空」。

判準（與產出端對齊）：
    有任何一層算出方向 → stage_code/alignment_score 是**答案**（含「確實是 RANGE」）。
    一層都沒有         → 未知：stage/stage_code/stage_label/alignment_score 一律 None，
                         並掛 insufficient_data=True 讓消費端可據以判斷。
    ⛔ 邊界：真的算出來就是盤整（有分層、方向為 range）仍必須照舊記 'RANGE'——
       把「確認是盤整」也折成未知，是把這個修補反向做壞。

執行：pytest tests/test_tf_nesting_unknown_v221.py
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from market_intel_mcp.timeframe_nesting import build_nesting  # noqa: E402

macro = importlib.import_module("l3_dispatcher.macro")


# ---------------------------------------------------------------- 合成 K 線
def _synth(direction: str, n: int, base: float = 100.0, step: float = 1.0) -> list[dict]:
    """升序合成 OHLCV（欄位同 okx_candles）。n < 25 → classify_tf_trend 回 unknown。"""
    candles: list[dict] = []
    price = float(base)
    for i in range(n):
        if direction == "up":
            o, c = price, price + step
            h, l = c + step * 0.3, o - step * 0.2
        elif direction == "down":
            o, c = price, price - step
            h, l = o + step * 0.2, c - step * 0.3
        else:  # range 鋸齒
            up = (i % 2 == 0)
            o = price
            c = price + step if up else price - step
            h, l = max(o, c) + step * 0.3, min(o, c) - step * 0.3
        candles.append({"ts": 1700000000000 + i * 3600000, "open": o, "high": h,
                        "low": l, "close": c, "volume": 1000.0})
        price = c
    return candles


_UNKNOWN_KEYS = ("stage", "stage_code", "stage_label", "alignment_score")


# =====================================================================
# 正向側：以下 8 條在改動前的 HEAD 上必須失敗（失敗訊息即舊行為原文）
# =====================================================================

def test_no_candles_at_all_is_unknown_not_range():
    """一根 K 線都沒有 → 未知，不是「確認是盤整」。"""
    n = build_nesting({})
    for k in _UNKNOWN_KEYS:
        assert n[k] is None, (
            f"舊行為：完全沒有 K 線時 {k}={n[k]!r}——把『這輪沒算出來』"
            f"講成『確認是盤整・對齊 0%』，而這一格會進復盤學習欄位")
    assert n["insufficient_data"] is True


def test_no_candles_marks_insufficient_flag():
    """未知態必須自報家門，消費端才有得判（不必去猜 stage_rationale 的字串）。"""
    assert build_nesting({}).get("insufficient_data") is True
    assert build_nesting(None).get("insufficient_data") is True


def test_all_timeframes_errored_is_unknown():
    """每個時框都回 {'error':...}（API 降級最常見的形）→ 未知。"""
    by_tf = {tf: {"error": "rate_limited", "candles": []}
             for tf in ("1M", "1w", "1d", "3d", "2d", "12h", "4h")}
    n = build_nesting(by_tf)
    for k in _UNKNOWN_KEYS:
        assert n[k] is None, f"全時框 error 時 {k}={n[k]!r}（應為未知）"
    assert n["insufficient_data"] is True


def test_all_layers_too_short_is_unknown():
    """K 線拿到了但每層都不足 lookback+5 根（新上市幣的真實形）→ 每層 unknown → 未知。"""
    by_tf = {tf: _synth("up", 10) for tf in ("1M", "1w", "1d", "4h")}
    n = build_nesting(by_tf)
    for k in _UNKNOWN_KEYS:
        assert n[k] is None, f"全層 K 線不足時 {k}={n[k]!r}（應為未知）"
    assert n["insufficient_data"] is True


def test_unknown_nesting_not_written_into_review_snapshot(monkeypatch):
    """未知的嵌套不得進復盤快照——寧可留空，不可寫一個假的 regime 分桶。"""
    monkeypatch.setattr(macro, "_read_macro_confluence_score", lambda: None)
    monkeypatch.setattr(macro, "_read_cycle_value_zone", lambda _s: None)
    out = macro._deepdive_extra_context("BTC", {"tf_nesting": build_nesting({})})
    assert "nest_stage_code" not in out, (
        f"舊行為：nest_stage_code={out.get('nest_stage_code')!r} 被寫進復盤快照"
        "＝優化器會把一筆缺料樣本當成一筆『盤整』觀測值")
    assert "nest_alignment_pct" not in out, (
        f"舊行為：nest_alignment_pct={out.get('nest_alignment_pct')!r} 被寫進復盤快照"
        "＝『各層完全不對齊』這個斷言其實一層都沒算過")


def test_unknown_nesting_snapshot_omits_all_nest_keys(monkeypatch):
    """四個 nest_* 欄位全部省略（骨架 None＝誠實留空，紅線③）。"""
    monkeypatch.setattr(macro, "_read_macro_confluence_score", lambda: None)
    monkeypatch.setattr(macro, "_read_cycle_value_zone", lambda _s: None)
    out = macro._deepdive_extra_context("ETH", {"tf_nesting": build_nesting({})})
    for k in ("nest_stage_code", "nest_alignment_pct",
              "nest_divergence_tf", "nest_1d_dir"):
        assert k not in out, f"{k} 不該出現在未知態的快照裡（值={out.get(k)!r}）"


def test_errored_timeframes_snapshot_is_empty(monkeypatch):
    """全時框 error 的那一輪，復盤快照同樣不得留下 nest_* 痕跡。"""
    monkeypatch.setattr(macro, "_read_macro_confluence_score", lambda: None)
    monkeypatch.setattr(macro, "_read_cycle_value_zone", lambda _s: None)
    by_tf = {tf: {"error": "timeout"} for tf in ("1M", "1w", "1d", "4h")}
    out = macro._deepdive_extra_context("SOL", {"tf_nesting": build_nesting(by_tf)})
    assert "nest_stage_code" not in out
    assert "nest_alignment_pct" not in out


def test_unknown_keeps_honest_neighbours():
    """未知態其餘欄位維持原本就誠實的形（不得順手把它們也改掉）。"""
    n = build_nesting({})
    assert n["dominant_trend"] == "unknown"
    assert n["layers"] == []
    assert n["layer_count"] == 0
    assert n["divergence_tf"] is None
    assert n["trade_side"]["side"] == "neutral"
    assert n["insufficient_data"] is True


# =====================================================================
# 反向側：以下 5 條在改動前的 HEAD 上本來就是綠的（＝非虛設檢定）
# 它們釘住邊界：算過就是答案，永遠不准被折成未知。
# =====================================================================

def test_real_layers_still_produce_a_verdict():
    """有分層 → 照舊給確定答案，insufficient_data 不得為真。"""
    by_tf = {"1M": _synth("up", 60), "1w": _synth("up", 60),
             "1d": _synth("up", 60), "4h": _synth("up", 60)}
    n = build_nesting(by_tf)
    assert isinstance(n["stage_code"], str) and n["stage_code"]
    assert isinstance(n["alignment_score"], float)
    assert not n.get("insufficient_data")
    assert n["layer_count"] > 0


def test_genuine_range_still_recorded_as_range():
    """⛔ 邊界：真的算出來是盤整，仍必須是 'RANGE'——不得被這次修補折成未知。"""
    by_tf = {tf: _synth("range", 60) for tf in ("1M", "1w", "1d", "4h")}
    n = build_nesting(by_tf)
    assert n["layer_count"] > 0
    assert n["stage_code"] == "RANGE"
    assert n["stage_label"] == "盤整"
    assert not n.get("insufficient_data")


def test_genuine_verdict_still_reaches_review_snapshot(monkeypatch):
    """算過的答案照舊要進復盤快照（本修補不得讓真樣本消失）。"""
    monkeypatch.setattr(macro, "_read_macro_confluence_score", lambda: None)
    monkeypatch.setattr(macro, "_read_cycle_value_zone", lambda _s: None)
    by_tf = {tf: _synth("range", 60) for tf in ("1M", "1w", "1d", "4h")}
    out = macro._deepdive_extra_context("BTC", {"tf_nesting": build_nesting(by_tf)})
    assert out["nest_stage_code"] == "RANGE"
    assert isinstance(out["nest_alignment_pct"], float)


def test_zero_alignment_from_real_layers_still_recorded(monkeypatch):
    """對齊真的算出來是 0.0（極端但合法）仍要記——0.0 是答案，不是未知的代名詞。"""
    monkeypatch.setattr(macro, "_read_macro_confluence_score", lambda: None)
    monkeypatch.setattr(macro, "_read_cycle_value_zone", lambda _s: None)
    sym_state = {"tf_nesting": {"stage_code": "RANGE", "alignment_score": 0.0,
                                "divergence_tf": "1w", "layers": [],
                                "layer_count": 3}}
    out = macro._deepdive_extra_context("BTC", sym_state)
    assert out["nest_stage_code"] == "RANGE"
    assert out["nest_alignment_pct"] == 0.0


def test_shadow_display_line_still_silent_on_unknown():
    """顯示層本來就有 layer_count 守門（回 ''）——本修補不得改變它。"""
    assert macro._shadow_tf_nesting_line({"tf_nesting": build_nesting({})}) == ""
    assert macro._shadow_tf_nesting_line({}) == ""
