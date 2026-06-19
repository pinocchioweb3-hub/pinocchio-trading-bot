"""postmortem 純函式測試（Session A）— 離線、零網路、零真實 DB 依賴。

執行（任一）：
    pytest tests/test_postmortem.py
    python tests/test_postmortem.py

涵蓋（題目指定最少集合 + 邊界）：
    1. ms↔s 換算（nearest_breadth）——最易錯處，重點覆蓋。
    2. exit 分類（classify_exit）各情境。
    3. 分桶 EV（bucket_ev）avg_R/勝率/賠錢模式偵測。
    4. D1 反事實（d1_counterfactual + btc_above_200ma_4h 無前視）。
    5. jsonl 去重（filter_new_closed / load_processed_keys / append_notes round-trip）。
    另：逆勢標記、廣度 up% 計算、環境回溯整合、輸入不可變。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from l3_dispatcher.postmortem import (
    BREADTH_MATCH_TOL_S,
    append_notes,
    btc_above_200ma_4h,
    bucket_ev,
    classify_exit,
    d1_counterfactual,
    enrich_with_environment,
    filter_new_closed,
    is_countertrend,
    load_processed_keys,
    nearest_breadth,
    _breadth_bucket,
    _dedup_key,
    _up_pct_from_breadth,
)


# ---------------------------------------------------------------------------
# 1) ms↔s 換算（最易錯處）
# ---------------------------------------------------------------------------
def test_nearest_breadth_ms_to_s_alignment():
    # entry_at 是毫秒：1_700_000_000_000ms = 1_700_000_000s
    entry_ms = 1_700_000_000_000
    rows = [
        {"ts": 1_700_000_000, "n_up24h": 60, "n_down24h": 40, "avg_funding": 0.0001},
        {"ts": 1_699_000_000, "n_up24h": 10, "n_down24h": 90, "avg_funding": -0.0002},
    ]
    b = nearest_breadth(entry_ms, rows)
    assert b is not None
    # 必須對齊到秒相等那列（不是把秒當毫秒比 → 那會兩列都差 1.7e12 而誤選）
    assert b["ts"] == 1_700_000_000
    assert b["n_up24h"] == 60


def test_nearest_breadth_picks_closest_within_tol():
    entry_ms = 1_700_000_500_000     # 1_700_000_500 s
    rows = [
        {"ts": 1_700_000_000, "n_up24h": 1, "n_down24h": 1},   # 差 500s
        {"ts": 1_700_000_400, "n_up24h": 2, "n_down24h": 2},   # 差 100s ← 最近
        {"ts": 1_700_001_000, "n_up24h": 3, "n_down24h": 3},   # 差 500s
    ]
    b = nearest_breadth(entry_ms, rows)
    assert b["ts"] == 1_700_000_400


def test_nearest_breadth_outside_tolerance_returns_none():
    entry_ms = 1_700_000_000_000      # 1_700_000_000 s
    far = entry_ms // 1000 + BREADTH_MATCH_TOL_S + 1
    rows = [{"ts": far, "n_up24h": 5, "n_down24h": 5}]
    assert nearest_breadth(entry_ms, rows) is None


def test_nearest_breadth_empty_returns_none():
    assert nearest_breadth(1_700_000_000_000, []) is None


# ---------------------------------------------------------------------------
# 2) exit 分類
# ---------------------------------------------------------------------------
def test_classify_exit_win_full():
    assert classify_exit("tp3", 2.5, "tp1,tp2,tp3") == "win_full"
    assert classify_exit(None, 2.0, "tp1,tp2,tp3") == "win_full"


def test_classify_exit_win_partial():
    assert classify_exit("tp1", 0.5, "tp1") == "win_partial"
    assert classify_exit("tp2", 1.2, "tp1,tp2") == "win_partial"


def test_classify_exit_stop_loss():
    assert classify_exit("stop", -1.0, "") == "stop_loss"
    # 移動停利打到 stop 但 R>0 → 不算真止損
    assert classify_exit("stop", 0.8, "tp1") == "win_partial"


def test_classify_exit_timeout_and_expired():
    assert classify_exit("timeout", -0.2, "") == "timeout"
    assert classify_exit("entry_expired", 0, "") == "entry_expired"


def test_classify_exit_fallback_by_r():
    assert classify_exit("", 0.4, "") == "win_partial"
    assert classify_exit("", -0.4, "") == "stop_loss"
    assert classify_exit("", 0, "") == "other"


# ---------------------------------------------------------------------------
# 逆勢標記 + up% 計算
# ---------------------------------------------------------------------------
def test_is_countertrend():
    assert is_countertrend("bull", 20.0) is True       # 跌市做多 = 逆勢
    assert is_countertrend("bull", 60.0) is False
    assert is_countertrend("bear", 80.0) is True       # 漲市做空 = 逆勢
    assert is_countertrend("bear", 50.0) is False
    assert is_countertrend("bull", None) is None       # 環境未知 → None


def test_up_pct_from_breadth():
    assert _up_pct_from_breadth({"n_up24h": 60, "n_down24h": 40}) == 60.0
    assert _up_pct_from_breadth({"n_up24h": 0, "n_down24h": 0}) is None


def test_breadth_bucket():
    assert _breadth_bucket(None) == "unknown"
    assert _breadth_bucket(20.0) == "bearish"
    assert _breadth_bucket(50.0) == "neutral"
    assert _breadth_bucket(80.0) == "bullish"


def test_enrich_does_not_mutate_input():
    trade = {"id": 1, "fire_id": 9, "direction": "bull", "setup": "deepdive",
             "entry_at": 1_700_000_000_000, "realized_r": -1.0,
             "exit_reason": "stop", "legs_hit": ""}
    rows = [{"ts": 1_700_000_000, "n_up24h": 10, "n_down24h": 90, "avg_funding": 0.0}]
    before = dict(trade)
    e = enrich_with_environment(trade, rows)
    assert trade == before                       # 輸入不變
    assert e["exit_class"] == "stop_loss"
    assert e["up_pct"] == 10.0
    assert e["countertrend"] is True             # bull@up10% = 逆勢
    assert e["dedup_key"] == "fire:9"


# ---------------------------------------------------------------------------
# 3) 分桶 EV
# ---------------------------------------------------------------------------
def _mk(setup, direction, r, exit_class="stop_loss", up_pct=None, regime="range",
        fire_id=None, _id=None, ct=None):
    return {"setup": setup, "direction": direction, "realized_r": r,
            "exit_class": exit_class, "up_pct": up_pct, "regime": regime,
            "fire_id": fire_id, "id": _id, "countertrend": ct}


def test_bucket_ev_basic_aggregation():
    rows = [
        _mk("deepdive", "bull", 2.0, "win_full", up_pct=70, ct=False),
        _mk("deepdive", "bull", -1.0, "stop_loss", up_pct=20, ct=True),
        _mk("us_breakout", "bull", 1.0, "win_partial", up_pct=55, ct=False),
    ]
    ev = bucket_ev(rows)
    assert ev["n_total"] == 3
    dd = ev["setup"]["deepdive"]
    assert dd["n"] == 2
    assert dd["avg_r"] == 0.5                      # (2 + -1)/2
    assert dd["win_rate"] == 50.0
    assert ev["setup"]["us_breakout"]["n"] == 1


def test_bucket_ev_excludes_entry_expired():
    rows = [
        _mk("deepdive", "bull", 0, "entry_expired", up_pct=50),
        _mk("deepdive", "bull", 1.0, "win_partial", up_pct=50, ct=False),
    ]
    ev = bucket_ev(rows)
    assert ev["n_total"] == 1                       # expired 不計
    assert ev["setup"]["deepdive"]["n"] == 1


def test_bucket_ev_detects_losing_pattern():
    # 6 筆 deepdive 逆勢偏空環境全賠 → 應被列為賠錢模式（n>=MIN_PATTERN_N=5）
    rows = [_mk("deepdive", "bull", -1.0, "stop_loss", up_pct=20, ct=True)
            for _ in range(6)]
    ev = bucket_ev(rows)
    lp = ev["losing_patterns"]
    assert len(lp) >= 1
    top = lp[0]
    assert "deepdive" in top["pattern"]
    assert "逆勢" in top["pattern"]
    assert top["n"] == 6
    assert top["sum_r"] == -6.0


def test_bucket_ev_skips_small_losing_pattern():
    # 只有 3 筆（< MIN_PATTERN_N）即使全賠也不該列為模式
    rows = [_mk("deepdive", "bull", -1.0, "stop_loss", up_pct=20, ct=True)
            for _ in range(3)]
    ev = bucket_ev(rows)
    assert ev["losing_patterns"] == []


# ---------------------------------------------------------------------------
# 4) D1 反事實 + 無前視 200MA
# ---------------------------------------------------------------------------
def test_btc_200ma_no_lookahead():
    # 構造 250 根 4h K：前 200 根低、之後抬高。entry 設在第 210 根。
    step = 14_400_000     # 4h in ms
    base = 1_600_000_000_000
    closes = []
    for i in range(250):
        ts = base + i * step
        price = 100.0 if i < 200 else 200.0
        closes.append((ts, price))
    # entry 在第 210 根（index 210）的時間點：此前有 200 根低 + 10 根高
    entry_at = base + 210 * step
    res = btc_above_200ma_4h(entry_at, closes, period=200)
    # 200MA 視窗 = past[-200:]（含那 10 根高、190 根低）→ ma 約 105；last_close=200 > ma
    assert res is True
    # 反例：entry 在第 50 根，past 只有 50 根 < period → 資料不足 → None
    early = base + 50 * step
    assert btc_above_200ma_4h(early, closes, period=200) is None


def test_btc_200ma_below():
    step = 14_400_000
    base = 1_600_000_000_000
    # 前面全高、最後一根低 → last_close 低於 200MA
    closes = [(base + i * step, 200.0) for i in range(200)]
    closes.append((base + 200 * step, 50.0))
    entry_at = base + 200 * step
    assert btc_above_200ma_4h(entry_at, closes, period=200) is False


def test_btc_200ma_empty_is_none():
    assert btc_above_200ma_4h(1_700_000_000_000, [], period=200) is None


def test_d1_counterfactual_blocks_losers():
    step = 14_400_000
    base = 1_600_000_000_000
    # BTC 4h 全程在 200MA 之下（last < ma 永遠不成立的構造）：先高後低
    closes = [(base + i * step, 200.0) for i in range(200)]
    closes.append((base + 200 * step, 50.0))     # entry 時點 below MA
    entry_at = base + 200 * step
    # 三筆 deepdive：環境差（up低）+ BTC below MA → 全被擋；其中是賠錢的
    rows = [
        {"setup": "deepdive", "direction": "bull", "realized_r": -1.0,
         "exit_class": "stop_loss", "up_pct": 20.0, "entry_at": entry_at},
        {"setup": "deepdive", "direction": "bull", "realized_r": -0.8,
         "exit_class": "stop_loss", "up_pct": 25.0, "entry_at": entry_at},
    ]
    d1 = d1_counterfactual(rows, closes, breadth_gate=45.0, period=200)
    assert d1["n_eval"] == 2
    assert d1["blocked_n"] == 2                   # up<45 且 below MA → 都被擋
    assert d1["passed_n"] == 0
    assert d1["blocked_sum_r"] == -1.8
    assert "可能有幫助" in d1["verdict"]


def test_d1_counterfactual_only_deepdive():
    # us_breakout 單不該進 D1 評估
    rows = [{"setup": "us_breakout", "direction": "bull", "realized_r": -1.0,
             "exit_class": "stop_loss", "up_pct": 20.0, "entry_at": 1_700_000_000_000}]
    d1 = d1_counterfactual(rows, [], breadth_gate=45.0)
    assert d1["n_eval"] == 0
    assert "無已平倉 deepdive" in d1["verdict"]


def test_d1_unknown_breadth_is_blocked_conservatively():
    # up_pct=None（回溯不到環境）→ 保守當「被擋」（gate 無法確認通過）
    rows = [{"setup": "deepdive", "direction": "bull", "realized_r": 1.5,
             "exit_class": "win_full", "up_pct": None, "entry_at": 1_700_000_000_000}]
    d1 = d1_counterfactual(rows, [], breadth_gate=45.0)
    assert d1["blocked_n"] == 1                   # 即使是賺的，未知環境也保守當擋
    assert d1["passed_n"] == 0


# ---------------------------------------------------------------------------
# 5) jsonl 去重
# ---------------------------------------------------------------------------
def test_dedup_key_prefers_fire_id():
    assert _dedup_key({"fire_id": 42, "id": 7}) == "fire:42"
    assert _dedup_key({"fire_id": None, "id": 7}) == "id:7"


def test_filter_new_closed_skips_processed():
    trades = [
        {"fire_id": 1, "id": 10},
        {"fire_id": 2, "id": 11},
        {"fire_id": None, "id": 12},
    ]
    processed = {"fire:1"}
    new = filter_new_closed(trades, processed)
    keys = {_dedup_key(t) for t in new}
    assert keys == {"fire:2", "id:12"}


def test_filter_new_closed_dedups_within_batch():
    # 同 fire_id 在同一批出現兩次 → 只留一筆
    trades = [{"fire_id": 5, "id": 1}, {"fire_id": 5, "id": 2}]
    new = filter_new_closed(trades, set())
    assert len(new) == 1


def test_append_notes_and_load_roundtrip():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "postmortem_notes.jsonl"
        recs = [{"dedup_key": "fire:1", "symbol": "BTC"},
                {"dedup_key": "id:9", "symbol": "ETH"}]
        n = append_notes(recs, p)
        assert n == 2
        keys = load_processed_keys(p)
        assert keys == {"fire:1", "id:9"}
        # 再 append 一筆 → load 應反映累積
        append_notes([{"dedup_key": "fire:2", "symbol": "SOL"}], p)
        assert load_processed_keys(p) == {"fire:1", "id:9", "fire:2"}


def test_load_processed_keys_missing_file():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "nope.jsonl"
        assert load_processed_keys(p) == set()


def test_load_processed_keys_tolerates_bad_lines():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "notes.jsonl"
        p.write_text('{"dedup_key": "fire:1"}\nNOT JSON\n\n{"dedup_key":"id:2"}\n',
                     encoding="utf-8")
        assert load_processed_keys(p) == {"fire:1", "id:2"}


# ---------------------------------------------------------------------------
# 直跑入口
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import traceback
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    passed = failed = 0
    for fn in fns:
        try:
            fn()
            passed += 1
        except Exception:
            failed += 1
            print(f"FAIL {fn.__name__}")
            traceback.print_exc()
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
