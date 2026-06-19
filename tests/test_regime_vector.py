"""多維 regime 向量影子層測試 — v56 / 復盤引擎 step4。

驗證 regime_vector.assemble 把『進場當下已算好的觀測值』純資料地打包成穩定的
regime_vector / context 影子向量：
  1. 分類器各分桶正確（funding_state / cvd_state / oi_price_quadrant）。
  2. _btc_above_from_regime / _htf_aligned 推導正確（曖昧回 None 不造假）。
  3. _get 相容 dict 與 dataclass。
  4. assemble 從 dict snap 與 MarketSnapshot dataclass 都能填實 per-symbol 欄。
  5. assemble(None) 只取市場層（廣度/均資費），per-symbol 欄留 None。
  6. assemble 全程 exception-safe：壞輸入 → 全 None 不拋。
  7. extra_context 覆蓋（deepdive 的 confluence/wyckoff）。
  8. schema 穩定：鍵恆在、值可空。
  9. 與 build_plan_snapshot 端到端：向量灌入後 regime_at_entry/context_at_entry
     填實、missing_context_keys 縮短。
 10. CI 護欄：regime_vector.py 絕不 import strength / 呼叫 evaluate / eval_cvd_divergence。

執行：pytest tests/test_regime_vector.py  或  python tests/test_regime_vector.py
"""
from __future__ import annotations

import ast
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 資料目錄指到臨時區（engine_epoch / breadth DB 不污染正式區），須在 import 前設好
_TMP = Path(tempfile.mkdtemp(prefix="regimevec_test_"))
os.environ["BOT_DATA_DIR"] = str(_TMP)

import l3_dispatcher.regime_vector as rv  # noqa: E402
import l3_dispatcher.plan_snapshot as ps  # noqa: E402
from l2_trigger.types import MarketSnapshot  # noqa: E402


# --- 1a. funding_state 分桶 ---
def test_classify_funding_state():
    assert rv.classify_funding_state(None) is None
    assert rv.classify_funding_state(0.001) == "hot"        # ≥0.0008
    assert rv.classify_funding_state(0.0008) == "hot"
    assert rv.classify_funding_state(-0.0005) == "negative"  # ≤-0.0001
    assert rv.classify_funding_state(0.0) == "neutral"
    assert rv.classify_funding_state("bad") is None          # 不合法 → None


# --- 1b. cvd_state：背離優先、其餘看斜率 ---
def test_classify_cvd_state():
    assert rv.classify_cvd_state(0.5, "bull") == "bull_divergence"   # 背離優先
    assert rv.classify_cvd_state(-0.5, "bear") == "bear_divergence"
    assert rv.classify_cvd_state(0.2, "none") == "rising"           # ≥0.15
    assert rv.classify_cvd_state(-0.2, "none") == "falling"         # ≤-0.15
    assert rv.classify_cvd_state(0.05, "none") == "flat"
    assert rv.classify_cvd_state(None, "none") is None
    assert rv.classify_cvd_state("bad", None) is None


# --- 1c. oi_price_quadrant：四象限 + 不明回 None ---
def test_classify_oi_price_quadrant():
    assert rv.classify_oi_price_quadrant(5.0, "up") == "price_up_oi_up"
    assert rv.classify_oi_price_quadrant(-5.0, "up") == "price_up_oi_down"
    assert rv.classify_oi_price_quadrant(5.0, "down") == "price_down_oi_up"
    assert rv.classify_oi_price_quadrant(-5.0, "down") == "price_down_oi_down"
    assert rv.classify_oi_price_quadrant(0.0, "up") == "price_up_oi_flat"
    assert rv.classify_oi_price_quadrant(5.0, None) is None     # 價格方向不明
    assert rv.classify_oi_price_quadrant(None, "up") is None    # OI 缺
    assert rv.classify_oi_price_quadrant("bad", "up") is None


# --- 2a. _btc_above_from_regime ---
def test_btc_above_from_regime():
    assert rv._btc_above_from_regime("trend_up") is True
    assert rv._btc_above_from_regime("trend_down") is False
    assert rv._btc_above_from_regime("range") is None       # 曖昧不造假
    assert rv._btc_above_from_regime(None) is None


# --- 2b. _htf_aligned ---
def test_htf_aligned():
    assert rv._htf_aligned(True, "bull") is True       # 站上 200MA + 做多 = 同向
    assert rv._htf_aligned(True, "bear") is False
    assert rv._htf_aligned(False, "bull") is False
    assert rv._htf_aligned(False, "bear") is True       # 在 200MA 下 + 做空 = 同向
    assert rv._htf_aligned(None, "bull") is None
    assert rv._htf_aligned(True, None) is None


# --- 3. _get 相容 dict 與 dataclass ---
def test_get_dict_and_dataclass():
    d = {"funding": 0.001, "cvd_slope": 0.2}
    assert rv._get(d, "funding") == 0.001
    assert rv._get(d, "missing") is None
    snap = MarketSnapshot(symbol="BTC", ts=1, price=100.0, funding=0.001)
    assert rv._get(snap, "funding") == 0.001
    assert rv._get(snap, "cvd_slope") is None
    assert rv._get(None, "funding") is None


# --- 4a. assemble 從 dict snap 填實 per-symbol 欄 ---
def test_assemble_from_dict():
    snap = {
        "funding": -0.0005, "cvd_slope": 0.3, "cvd_price_divergence": "none",
        "oi_delta_pct": 5.0, "top_trader_ratio": 1.2,
        "btc_regime": "trend_up", "above_4h_200ma": True,
        "breakout_1h_high": True,
    }
    rgv, ctx = rv.assemble(snap, direction="bull", include_market=False)
    assert rgv["funding_state"] == "negative"
    assert rgv["cvd_state"] == "rising"
    assert rgv["oi_price_quadrant"] == "price_up_oi_up"   # breakout_1h_high → up
    assert ctx["oi_delta_pct"] == 5.0
    assert ctx["cvd_slope"] == 0.3
    assert ctx["top_trader_ratio"] == 1.2
    assert ctx["btc_above_200ma_4h"] is True
    assert ctx["htf_aligned"] is True
    # 未填的市場層（include_market=False）→ None
    assert ctx["breadth_up_pct"] is None
    assert ctx["avg_funding"] is None
    # 未抓的維度留 None（誠實）
    assert ctx["whale_net"] is None
    assert ctx["wyckoff_phase"] is None
    assert ctx["macro_confluence_score"] is None


# --- 4b. assemble 從 MarketSnapshot dataclass 填實 ---
def test_assemble_from_dataclass():
    snap = MarketSnapshot(
        symbol="AAPL", ts=1, price=200.0, funding=0.0009,
        oi_delta_pct=-5.0, cvd_price_divergence="bear",
        us_breakout_dir="bear", above_4h_200ma=False)
    rgv, ctx = rv.assemble(snap, direction="bear", include_market=False)
    assert rgv["funding_state"] == "hot"
    assert rgv["cvd_state"] == "bear_divergence"
    assert rgv["oi_price_quadrant"] == "price_down_oi_down"  # us_breakout_dir bear → down
    assert ctx["htf_aligned"] is True   # 在 200MA 下 + 做空 = 同向


# --- 5. assemble(None) 只取市場層，per-symbol 欄全 None ---
def test_assemble_none_snap_market_only(monkeypatch):
    import l3_dispatcher.market_scanner as ms
    monkeypatch.setattr(ms, "get_latest_breadth", lambda: {
        "n_up24h": 30, "n_down24h": 10, "avg_funding": 0.0002})
    rgv, ctx = rv.assemble(None, direction="bull", include_market=True)
    assert ctx["breadth_up_pct"] == 75.0      # 30/(30+10)*100
    assert ctx["avg_funding"] == 0.0002
    # per-symbol 欄全 None（沒有 snapshot）
    assert ctx["oi_delta_pct"] is None
    assert ctx["top_trader_ratio"] is None
    assert rgv["funding_state"] is None
    assert rgv["cvd_state"] is None


# --- 5b. 市場層 DB 失敗 → 安全降級 None ---
def test_assemble_market_db_failure_safe(monkeypatch):
    import l3_dispatcher.market_scanner as ms

    def _boom():
        raise RuntimeError("db down")
    monkeypatch.setattr(ms, "get_latest_breadth", _boom)
    rgv, ctx = rv.assemble({"funding": 0.0}, direction="bull", include_market=True)
    assert ctx["breadth_up_pct"] is None
    assert ctx["avg_funding"] is None
    assert rgv["funding_state"] == "neutral"   # per-symbol 仍正常


# --- 6. exception-safe：壞輸入不拋，回穩定 schema ---
def test_assemble_exception_safe():
    rgv, ctx = rv.assemble(12345, direction="bull", include_market=False)  # 非 dict/dataclass
    assert set(rgv.keys()) == set(ps._REGIME_KEYS)
    assert set(ctx.keys()) == set(ps._CONTEXT_KEYS)
    assert all(v is None for v in rgv.values())


# --- 7. extra_context 覆蓋（deepdive 已算好的 confluence/wyckoff）---
def test_assemble_extra_context_overlay():
    rgv, ctx = rv.assemble(None, direction="bull", include_market=False,
                           extra_context={"macro_confluence_score": 7.5,
                                          "wyckoff_phase": "accumulation",
                                          "BOGUS": 1})
    assert ctx["macro_confluence_score"] == 7.5
    assert ctx["wyckoff_phase"] == "accumulation"
    assert "BOGUS" not in ctx           # 只認 schema 內鍵


# --- 8. schema 穩定：鍵恆在、值可空 ---
def test_assemble_schema_stable():
    rgv, ctx = rv.assemble({}, direction=None, include_market=False)
    assert set(rgv.keys()) == set(ps._REGIME_KEYS)
    assert set(ctx.keys()) == set(ps._CONTEXT_KEYS)


# --- 9. 端到端：向量灌入 build_plan_snapshot，missing 縮短 ---
def test_end_to_end_into_plan_snapshot():
    snap = {
        "funding": -0.0005, "cvd_slope": 0.3, "oi_delta_pct": 5.0,
        "top_trader_ratio": 1.2, "btc_regime": "trend_up",
        "above_4h_200ma": True, "breakout_1h_high": True,
    }
    rgv, ctx = rv.assemble(snap, direction="bull", include_market=False)
    plan = ps.build_plan_snapshot(
        source="direct_fire", direction="bull",
        entry_price=100.0, planned_stop=90.0,
        tp1=110.0, tp2=120.0, tp3=130.0, regime="low",
        regime_vector=rgv, context=ctx)
    assert plan is not None
    # regime 填實
    assert plan["regime_at_entry"]["vol_trend"] == "low"          # 由 regime 字串
    assert plan["regime_at_entry"]["funding_state"] == "negative"  # 由向量
    assert plan["regime_at_entry"]["oi_price_quadrant"] == "price_up_oi_up"
    # context 填實
    assert plan["context_at_entry"]["oi_delta_pct"] == 5.0
    assert plan["context_at_entry"]["htf_aligned"] is True
    # missing 縮短：填了的不在 missing，沒填的仍在
    assert "oi_delta_pct" not in plan["missing_context_keys"]
    assert "top_trader_ratio" not in plan["missing_context_keys"]
    assert "whale_net" in plan["missing_context_keys"]
    assert "macro_confluence_score" in plan["missing_context_keys"]


# --- 10. CI 護欄：regime_vector.py 絕不碰策略數學 ---
def test_no_strategy_imports():
    src = (ROOT / "l3_dispatcher" / "regime_vector.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    imported, called = set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for n in node.names:
                imported.add(n.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imported.add(node.module.split(".")[-1])
        elif isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Name):
                called.add(fn.id)
            elif isinstance(fn, ast.Attribute):
                called.add(fn.attr)
    assert "strength" not in imported, f"regime_vector 不可 import strength；imports={imported}"
    assert "eval_cvd_divergence" not in called, "regime_vector 不可呼叫 eval_cvd_divergence"
    assert "evaluate" not in called, "regime_vector 不可呼叫 evaluate"


# --- 直接執行（無 pytest 也能跑；monkeypatch 測試自動跳過）---
if __name__ == "__main__":
    fns = [(k, v) for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    passed = skipped = 0
    for name, fn in fns:
        if "monkeypatch" in fn.__code__.co_varnames[:fn.__code__.co_argcount]:
            print(f"  SKIP  {name} (需 pytest monkeypatch)")
            skipped += 1
            continue
        try:
            fn()
            print(f"  PASS  {name}")
            passed += 1
        except AssertionError as e:
            print(f"  FAIL  {name}: {e}")
        except Exception as e:  # noqa: BLE001
            print(f"  ERROR {name}: {type(e).__name__}: {e}")
    total = len(fns) - skipped
    print(f"\n{passed}/{total} passed ({skipped} skipped)")
    sys.exit(0 if passed == total else 1)
