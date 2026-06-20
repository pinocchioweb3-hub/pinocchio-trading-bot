"""進場 PlanSnapshot 捕捉測試 — v56 / 復盤引擎 step1。

驗證『真復盤』的前置層：進場那一刻把預期劇本／止損劇本／當下上下文凍結存檔。
重點：
  1. build_plan_snapshot 回穩定 schema（context 鍵恆在值可空、missing 清單正確、RR 算對）。
  2. context / regime overlay：給的值覆蓋、雜鍵忽略、未給的留 None。
  3. _safe_rr 多空與除零正確。
  4. record_paper_entry 能 round-trip 快照進 DB；未給快照→NULL 不壞。
  5. 冷卻擋下時不建單（快照不寫）。
  6. migration 冪等、engine_epoch 穩定。
  7. CI 護欄：plan_snapshot.py 絕不碰策略數學（ast 檢查 import/call，不受 docstring 影響）。

執行：pytest tests/test_plan_snapshot.py  或  python tests/test_plan_snapshot.py
"""
from __future__ import annotations

import ast
import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 資料目錄指到臨時區（engine_epoch 檔不污染正式區），須在 import 前設好
_TMP = Path(tempfile.mkdtemp(prefix="plansnap_test_"))
os.environ["BOT_DATA_DIR"] = str(_TMP)

import l3_dispatcher.plan_snapshot as ps  # noqa: E402
import l3_dispatcher.paper_journal as pj  # noqa: E402

pj.DB_PATH = _TMP / "trade_journal_test.db"


def _fresh():
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(pj.DB_PATH) + suffix)
        if p.exists():
            p.unlink()
    pj.init_db()


# --- 1. build 回穩定 schema、context 全 None、RR 算對 ---
def test_build_returns_dict_null_context():
    snap = ps.build_plan_snapshot(
        source="direct_fire", direction="bull",
        entry_price=100.0, planned_stop=90.0,
        tp1=110.0, tp2=120.0, tp3=130.0,
        fire_id=1, signal_msg_id=42, regime="trending_up",
        thesis="test thesis", confidence=0.8)
    assert snap is not None
    assert snap["schema_ver"] == ps.SCHEMA_VER
    assert snap["source"] == "direct_fire"
    assert snap["direction"] == "bull"
    assert snap["thesis"] == "test thesis"
    assert snap["confidence"] == 0.8
    assert snap["join"]["fire_id"] == 1
    assert snap["join"]["signal_msg_id"] == 42
    assert snap["join"]["paper_id"] is None
    # context 鍵恆在、值全 None；missing 清單＝全部鍵
    assert set(snap["context_at_entry"].keys()) == set(ps._CONTEXT_KEYS)
    assert all(v is None for v in snap["context_at_entry"].values())
    assert snap["missing_context_keys"] == sorted(ps._CONTEXT_KEYS)
    # regime: vol_trend 由 regime 字串帶入，其餘 None
    assert snap["regime_at_entry"]["vol_trend"] == "trending_up"
    assert snap["regime_at_entry"]["funding_state"] is None
    # RR: (110-100)/(100-90)=1.0; tp2=2.0; tp3=3.0
    assert snap["rr_to_tp"]["tp1"] == 1.0
    assert snap["rr_to_tp"]["tp2"] == 2.0
    assert snap["rr_to_tp"]["tp3"] == 3.0
    assert snap["expected_r"] == 1.0
    assert snap["expected_stop_scenario"]["trigger_level"] == 90.0
    assert snap["engine_epoch_ms"] > 0


# --- 2. context / regime overlay：給的覆蓋、雜鍵忽略、未給留 None ---
def test_context_overlay_fills_provided():
    snap = ps.build_plan_snapshot(
        source="x", direction="bear",
        entry_price=100.0, planned_stop=110.0,
        tp1=90.0, tp2=80.0, tp3=70.0,
        context={"avg_funding": 0.01, "breadth_up_pct": 42.0, "BOGUS": 1},
        regime_vector={"funding_state": "high", "cvd_state": "bull_div"})
    assert snap["context_at_entry"]["avg_funding"] == 0.01
    assert snap["context_at_entry"]["breadth_up_pct"] == 42.0
    assert "BOGUS" not in snap["context_at_entry"]            # 雜鍵忽略
    assert snap["context_at_entry"]["oi_delta_pct"] is None   # 未給留 None
    assert "avg_funding" not in snap["missing_context_keys"]
    assert "oi_delta_pct" in snap["missing_context_keys"]
    assert snap["regime_at_entry"]["funding_state"] == "high"
    assert snap["regime_at_entry"]["cvd_state"] == "bull_div"
    # bear RR: (100-90)/(110-100)=1.0
    assert snap["rr_to_tp"]["tp1"] == 1.0


# --- 2b. news_at_entry：未給 → None；給 dict → 原樣打包，且絕不擾動 rr/方向（task#66 Q2 Phase0）---
def test_news_at_entry_default_none():
    snap = ps.build_plan_snapshot(
        source="direct_fire", direction="bull",
        entry_price=100.0, planned_stop=90.0, tp1=110.0, tp2=120.0, tp3=130.0)
    # 未接消息面 → 誠實 None（鍵恆在值可空）
    assert "news_at_entry" in snap and snap["news_at_entry"] is None


def test_news_at_entry_passthrough_and_does_not_perturb_decision():
    news = {"symbol": "BTC", "direction_observed": "bull",
            "narrative_lean": {"lean": "bear", "net": -3, "n_hits": 2},
            "recent_ticker_atoms": [{"ingestion_seq": 21}], "n_ticker_atoms": 1,
            "note": "observation-only"}
    with_news = ps.build_plan_snapshot(
        source="direct_fire", direction="bull",
        entry_price=100.0, planned_stop=90.0, tp1=110.0, tp2=120.0, tp3=130.0,
        news_context=news)
    without = ps.build_plan_snapshot(
        source="direct_fire", direction="bull",
        entry_price=100.0, planned_stop=90.0, tp1=110.0, tp2=120.0, tp3=130.0)
    # 原樣打包進觀測欄
    assert with_news["news_at_entry"] == news
    # 鐵則：即使消息面 narrative_lean 偏 bear，方向/rr/expected_r 一律不受影響
    assert with_news["direction"] == "bull"
    assert with_news["rr_to_tp"] == without["rr_to_tp"]
    assert with_news["expected_r"] == without["expected_r"]
    # news_at_entry 不得污染到任何決策欄（只活在自己的觀測鍵裡）
    assert "narrative_lean" not in with_news and "news_at_entry" in with_news


def test_news_at_entry_non_dict_coerced_to_none():
    # 防呆：誤傳非 dict（字串/數字/list）→ 一律 None，不讓壞型別污染 schema
    for bad in ("oops", 123, ["x"], 0):
        snap = ps.build_plan_snapshot(
            source="x", direction="bull", entry_price=100.0, planned_stop=90.0,
            tp1=110.0, tp2=120.0, tp3=130.0, news_context=bad)
        assert snap["news_at_entry"] is None, f"壞型別 {bad!r} 應退為 None"


# --- 2c. context_provenance：未給 → None；給 dict → 原樣打包，且嚴格與決策/可學維度分離（task#70）---
def test_context_provenance_default_none():
    snap = ps.build_plan_snapshot(
        source="macro_deepdive", direction="bull",
        entry_price=100.0, planned_stop=90.0, tp1=110.0, tp2=120.0, tp3=130.0)
    # 未帶口徑 provenance → 誠實 None（鍵恆在值可空）
    assert "context_provenance" in snap and snap["context_provenance"] is None


def test_context_provenance_passthrough_and_does_not_perturb_decision():
    prov = {"macro_confluence_score": {
        "score_method": "v2_renorm_present_mass", "present_mass": 0.7,
        "n_present": 12, "floor_bound": False}}
    with_prov = ps.build_plan_snapshot(
        source="macro_deepdive", direction="bull",
        entry_price=100.0, planned_stop=90.0, tp1=110.0, tp2=120.0, tp3=130.0,
        context={"macro_confluence_score": 6.5},
        context_provenance=prov)
    without = ps.build_plan_snapshot(
        source="macro_deepdive", direction="bull",
        entry_price=100.0, planned_stop=90.0, tp1=110.0, tp2=120.0, tp3=130.0,
        context={"macro_confluence_score": 6.5})
    # 原樣打包進觀測欄
    assert with_prov["context_provenance"] == prov
    # 鐵則：provenance 一律不擾動 rr/expected_r/方向
    assert with_prov["direction"] == "bull"
    assert with_prov["rr_to_tp"] == without["rr_to_tp"]
    assert with_prov["expected_r"] == without["expected_r"]
    # 與可學維度嚴格分離：不洩進 context_at_entry、不改 missing_context_keys
    assert with_prov["context_at_entry"] == without["context_at_entry"]
    assert with_prov["missing_context_keys"] == without["missing_context_keys"]
    assert "context_provenance" not in with_prov["context_at_entry"]


def test_context_provenance_not_in_context_keys():
    # 治本鐵則：provenance 是中繼觀測欄，絕不可進可學 schema（否則優化器會把它當特徵）
    assert "context_provenance" not in ps._CONTEXT_KEYS
    assert "context_provenance" not in ps._REGIME_KEYS


def test_context_provenance_non_dict_coerced_to_none():
    # 防呆：誤傳非 dict（字串/數字/list）→ 一律 None，不讓壞型別污染 schema
    for bad in ("oops", 123, ["x"], 0):
        snap = ps.build_plan_snapshot(
            source="x", direction="bull", entry_price=100.0, planned_stop=90.0,
            tp1=110.0, tp2=120.0, tp3=130.0, context_provenance=bad)
        assert snap["context_provenance"] is None, f"壞型別 {bad!r} 應退為 None"


# --- 3. _safe_rr 多空與除零 ---
def test_safe_rr():
    assert ps._safe_rr("bull", 100, 90, 120) == 2.0
    assert ps._safe_rr("long", 100, 90, 120) == 2.0
    assert ps._safe_rr("bear", 100, 110, 80) == 2.0
    assert ps._safe_rr("short", 100, 110, 80) == 2.0
    assert ps._safe_rr("bull", 100, 100, 120) is None        # risk=0 → None
    assert ps._safe_rr("bull", "x", 90, 120) is None         # 不合法輸入 → None


# --- 3b. vol_regime_from_atr 共用口徑：分桶正確、缺料 unknown、與舊內聯式等價 ---
def test_vol_regime_from_atr_buckets():
    assert ps.vol_regime_from_atr(9.0) == "extreme"
    assert ps.vol_regime_from_atr(8.0) == "extreme"     # 邊界含等於
    assert ps.vol_regime_from_atr(7.99) == "high"
    assert ps.vol_regime_from_atr(5.0) == "high"        # 邊界含等於
    assert ps.vol_regime_from_atr(4.99) == "low"
    assert ps.vol_regime_from_atr(0.0) == "low"         # 0 是合法數值，非缺料
    assert ps.vol_regime_from_atr(None) == "unknown"    # 缺料誠實留空
    assert ps.vol_regime_from_atr("x") == "unknown"     # 非數值 → unknown（比舊式更穩，不炸）
    assert ps.vol_regime_from_atr("6.0") == "high"      # 數字字串容錯


def test_vol_regime_from_atr_equivalent_to_legacy_inline():
    """治本鐵則：共用函式對『數值/None』輸入須與舊 direct_fire 內聯三元式逐一等價，
    否則重構就偷改了歷史語意。"""
    def _legacy(atr):
        return ("unknown" if atr is None else
                "extreme" if atr >= 8.0 else
                "high" if atr >= 5.0 else "low")
    for atr in [None, 0.0, 1.5, 4.999, 5.0, 5.0001, 7.999, 8.0, 8.0001, 12.3, 100.0]:
        assert ps.vol_regime_from_atr(atr) == _legacy(atr), f"分歧於 atr={atr}"


# --- 4a. record_paper_entry round-trip 快照 ---
def test_record_roundtrips_snapshot():
    _fresh()
    snap = ps.build_plan_snapshot(
        source="direct_fire", direction="bull",
        entry_price=100.0, planned_stop=90.0,
        tp1=110.0, tp2=120.0, tp3=130.0, fire_id=7)
    pid = pj.record_paper_entry("BTC", "deepdive", "bull", 100.0, 90.0,
                                110.0, 120.0, 130.0, fire_id=7,
                                plan_snapshot=snap)
    assert pid > 0
    conn = pj._conn()
    try:
        row = conn.execute("SELECT plan_snapshot FROM paper_trades WHERE id=?",
                           (pid,)).fetchone()
    finally:
        conn.close()
    assert row[0] is not None
    back = json.loads(row[0])
    assert back["join"]["fire_id"] == 7
    assert back["rr_to_tp"]["tp1"] == 1.0


# --- 4b. 未給快照 → NULL，不壞既有流程 ---
def test_record_without_snapshot_null():
    _fresh()
    pid = pj.record_paper_entry("ETH", "deepdive", "bull", 3000.0, 2900.0,
                                3100.0, 3200.0, 3300.0)
    assert pid > 0
    conn = pj._conn()
    try:
        row = conn.execute("SELECT plan_snapshot FROM paper_trades WHERE id=?",
                           (pid,)).fetchone()
    finally:
        conn.close()
    assert row[0] is None, "未給快照應為 NULL"


# --- 5. 冷卻擋下時不建單（快照也不寫）---
def test_cooldown_blocked_writes_no_snapshot():
    _fresh()
    conn = pj._conn()
    try:
        now_ms = int(time.time() * 1000)
        conn.execute(
            "INSERT INTO paper_trades (symbol, setup, direction, entry_price, stop_price, "
            "entry_at, status, exit_reason, exit_at, realized_r, pnl_usd, size_remaining, "
            "created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("BTC", "deepdive", "bull", 101.0, 100.0, now_ms - 7_200_000,
             "closed", "timeout", now_ms - 600_000, 1.0, 100.0, 0, now_ms - 7_200_000))
    finally:
        conn.close()
    snap = ps.build_plan_snapshot(source="direct_fire", direction="bull",
                                  entry_price=101.0, planned_stop=100.0,
                                  tp1=110.0, tp2=120.0, tp3=130.0)
    rid = pj.record_paper_entry("BTC", "deepdive", "bull", 101.0, 100.0,
                                110.0, 120.0, 130.0, plan_snapshot=snap)
    assert rid == -1, f"冷卻應擋下（回 -1），實得 {rid}"


# --- 6a. migration 冪等 ---
def test_migration_idempotent():
    _fresh()
    pj.init_db()
    pj.init_db()          # 重複呼叫不應出錯
    conn = pj._conn()
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(paper_trades)").fetchall()}
    finally:
        conn.close()
    assert "plan_snapshot" in cols


# --- 6b. engine_epoch 穩定 ---
def test_engine_epoch_stable():
    a = ps.get_engine_epoch_ms()
    b = ps.get_engine_epoch_ms()
    assert a == b
    assert a > 0


# --- 7. CI 護欄：plan_snapshot.py 絕不碰策略數學（ast 檢查，不受 docstring 影響）---
def test_no_strategy_imports():
    src = (ROOT / "l3_dispatcher" / "plan_snapshot.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    imported, called = set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for n in node.names:
                imported.add(n.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imported.add(node.module.split(".")[0])
        elif isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Name):
                called.add(fn.id)
            elif isinstance(fn, ast.Attribute):
                called.add(fn.attr)
    assert "strength" not in imported, f"plan_snapshot 不可 import strength；imports={imported}"
    assert "eval_cvd_divergence" not in called, "plan_snapshot 不可呼叫 eval_cvd_divergence"
    assert "evaluate" not in called, "plan_snapshot 不可呼叫 evaluate"


# --- 直接執行（無 pytest 也能跑）---
if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
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
