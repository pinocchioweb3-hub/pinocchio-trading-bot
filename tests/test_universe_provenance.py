"""宇宙來源留痕測試 — v142（進場快照 provenance）。

驗證什麼、為什麼：
    CoinGlass 訂閱 2026-07-08 到期後，v141 讓宇宙空時自動降級到免費 OKX 大宗源。
    活線 `topN_agreement=0.0` ＝ 換源實質換掉了選出來的標的，所以「免費源時代」與
    「CG 時代」的加密樣本不可直接合併統計。若進場當下沒把來源凍進 plan_snapshot，
    日後在帳上就分不出這筆單出自哪個宇宙，而快照**只能前向累積、永不回填**（紅線③）。

涵蓋：
  1. 葉模組 set/get round-trip；壞型別／空字串 → None（誠實留空，不猜）。
  2. 加密三路徑（direct_fire / macro_deepdive / waiting_trigger）快照帶到來源。
  3. 美股路徑（us_breakout）恆 None——不經加密宇宙，絕不貼錯標。
  4. 從未 refresh 過 → None，且 universe_source 鍵**恆在**（schema 穩定）。
  5. 留痕欄嚴格分離：不進 _CONTEXT_KEYS、不進 missing_context_keys，
     不影響 rr / expected_r。
  6. watchlist.refresh 確實有呼叫 set_universe_source（接線護欄，防日後被刪掉）。

執行：pytest tests/test_universe_provenance.py  或  python tests/test_universe_provenance.py
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

# engine_epoch 檔不污染正式資料區，須在 import 前設好
_TMP = Path(tempfile.mkdtemp(prefix="univprov_test_"))
os.environ["BOT_DATA_DIR"] = str(_TMP)

import l3_dispatcher.plan_snapshot as ps            # noqa: E402
import l3_dispatcher.universe_provenance as up      # noqa: E402


def _snap(source: str):
    return ps.build_plan_snapshot(
        source=source, direction="bull",
        entry_price=100.0, planned_stop=90.0,
        tp1=110.0, tp2=120.0, tp3=130.0)


# --- 1. 葉模組 set/get；壞輸入誠實留空 ---
def test_set_get_roundtrip_and_bad_input():
    up.set_universe_source("okx_free_fallback")
    assert up.get_universe_source() == "okx_free_fallback"
    up.set_universe_source("coinglass")
    assert up.get_universe_source() == "coinglass"
    for bad in (None, "", 123, [], {}):
        up.set_universe_source(bad)
        assert up.get_universe_source() is None, f"壞輸入 {bad!r} 應留 None"


# --- 2. 加密三路徑帶到來源 ---
def test_crypto_sources_carry_provenance():
    up.set_universe_source("okx_free_fallback")
    for src in ("direct_fire", "macro_deepdive", "waiting_trigger"):
        snap = _snap(src)
        assert snap is not None
        assert snap["universe_source"] == "okx_free_fallback", f"{src} 應帶來源"
    up.set_universe_source("coinglass")
    assert _snap("direct_fire")["universe_source"] == "coinglass"


# --- 3. 美股路徑恆 None（不經加密宇宙，絕不貼錯標）---
def test_us_source_never_labelled():
    up.set_universe_source("okx_free_fallback")
    for src in ("us_breakout", "unknown_future_source"):
        assert _snap(src)["universe_source"] is None, f"{src} 不該被貼加密宇宙來源"


# --- 4. 未 refresh 過 → None，但鍵恆在（schema 穩定）---
def test_key_always_present_even_when_unknown():
    up.set_universe_source(None)
    snap = _snap("direct_fire")
    assert "universe_source" in snap, "universe_source 鍵必須恆在"
    assert snap["universe_source"] is None


# --- 5. 與可學維度嚴格分離、不影響下單數學 ---
def test_separated_from_context_and_math():
    up.set_universe_source("okx_free_fallback")
    snap = _snap("direct_fire")
    assert "universe_source" not in ps._CONTEXT_KEYS
    assert "universe_source" not in snap["context_at_entry"]
    assert "universe_source" not in snap["missing_context_keys"]
    # 換來源不得改動任何計畫數學
    before = (snap["rr_to_tp"], snap["expected_r"])
    up.set_universe_source("coinglass")
    after_snap = _snap("direct_fire")
    assert (after_snap["rr_to_tp"], after_snap["expected_r"]) == before


# --- 6. 接線護欄：watchlist.refresh 真的有呼叫 setter ---
def test_watchlist_wires_setter():
    src = (ROOT / "l3_dispatcher" / "watchlist.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    called = {n.func.id for n in ast.walk(tree)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "set_universe_source" in called, "watchlist 必須寫入宇宙來源，否則快照永遠是 None"


# ════════════════════════════════════════════════════════════════════════
#  v144（監督員 r29）：留痕的**統計消費者**——優化器不得混代算晉升
# ════════════════════════════════════════════════════════════════════════
import json as _json  # noqa: E402


def _row(gen, *, tid=0, entry_at=0, r=0.1):
    """一筆已平倉紙上單；gen=None ＝ v142 之前無留痕樣本（unknown 代）。"""
    snap = {"regime_at_entry": {"oi_price_quadrant": "price_up_oi_up"}}
    if gen is not None:
        snap["universe_source"] = gen
    return {"id": tid, "symbol": "BTC", "setup": "deepdive", "direction": "bull",
            "entry_price": 100.0, "stop_price": 90.0, "tp1": 110.0, "tp2": 120.0,
            "tp3": 140.0, "entry_at": entry_at, "exit_at": entry_at + 10,
            "legs_hit": "tp1,tp2,stop", "exit_reason": "stop", "realized_r": r,
            "pnl_usd": r * 100, "entry_filled_pct": 1.0,
            "plan_snapshot": _json.dumps(snap), "tp_alloc": None}


# --- 7. 單列世代判讀：缺鍵／壞 JSON／None → unknown，絕不猜 coinglass ---
def test_generation_of_row_never_guesses():
    up.set_universe_source(None)
    assert up.generation_of_row(_row("coinglass")) == "coinglass"
    assert up.generation_of_row(_row("okx_free_fallback")) == "okx_free_fallback"
    assert up.generation_of_row(_row(None)) == up.UNKNOWN_GENERATION      # v142 前無留痕
    assert up.generation_of_row({"plan_snapshot": "{壞JSON"}) == up.UNKNOWN_GENERATION
    assert up.generation_of_row({}) == up.UNKNOWN_GENERATION
    assert up.generation_of_row({"plan_snapshot": None}) == up.UNKNOWN_GENERATION


# --- 8. 現行世代：進程內真相優先，離線退回「最近一筆有留痕者」 ---
def test_active_generation_priority():
    rows = [_row(None, entry_at=1), _row("coinglass", entry_at=2),
            _row("okx_free_fallback", entry_at=3)]
    up.set_universe_source("coinglass")
    assert up.active_generation(rows) == "coinglass", "daemon 內應以本輪 refresh 為準"
    up.set_universe_source(None)
    assert up.active_generation(rows) == "okx_free_fallback", "離線應取最近一筆留痕"
    assert up.active_generation([_row(None), _row(None)]) == up.UNKNOWN_GENERATION
    assert up.active_generation([]) == up.UNKNOWN_GENERATION
    assert up.cohort_mix(rows) == {"unknown": 1, "coinglass": 1, "okx_free_fallback": 1}


# --- 9. 治本驗收：重現 2026-07-31 活庫污染比（unknown 152 : 免費源 5）---
#     舊碼會把 157 筆混在一桶算晉升；新碼只採現行代 5 筆 → 樣本不足 → 0 晉升。
def test_optimizer_excludes_other_generation():
    import tempfile as _tf
    from pathlib import Path as _P
    from backtest.l2_stat_gates import TrialLedger
    from l3_dispatcher import auto_optimizer as ao

    rows = ([_row(None, tid=i, entry_at=1000 + i) for i in range(152)]
            + [_row("okx_free_fallback", tid=900 + i, entry_at=9000 + i) for i in range(5)])
    up.set_universe_source("okx_free_fallback")   # 生產中＝免費源代
    with _tf.TemporaryDirectory() as td:
        res = ao.run_optimization(rows=rows, at_ms=1, ledger=TrialLedger(_P(td) / "l.jsonl"),
                                  active_path=_P(td) / "a.json", audit_path=_P(td) / "au.jsonl")
        c = res["cohort"]
        assert c["active_generation"] == "okx_free_fallback"
        assert c["n_in"] == 157 and c["n_kept"] == 5, f"應只留同代 5 筆，實得 {c}"
        assert c["n_excluded_other_generation"] == 152
        assert res["n_trades"] == 5, "報告筆數必須是實際進統計的筆數（不得虛報 157）"
        assert res["n_promoted"] == 0, "同代樣本 5 筆 <30 → minTRL fail-closed"
        assert ao._render_cohort_line(res).startswith("🌐 宇宙世代")


# --- 10. 不製造新阻斷：全庫皆無留痕（unknown 代）→ 維持既有行為，不清空樣本 ---
def test_all_unlabelled_keeps_status_quo():
    import tempfile as _tf
    from pathlib import Path as _P
    from backtest.l2_stat_gates import TrialLedger
    from l3_dispatcher import auto_optimizer as ao

    up.set_universe_source(None)
    rows = [_row(None, tid=i, entry_at=1000 + i) for i in range(40)]
    with _tf.TemporaryDirectory() as td:
        res = ao.run_optimization(rows=rows, at_ms=2, ledger=TrialLedger(_P(td) / "l.jsonl"),
                                  active_path=_P(td) / "a.json", audit_path=_P(td) / "au.jsonl")
        assert res["cohort"]["n_kept"] == 40, "無留痕時不得把樣本清空（那是新阻斷）"
        assert res["cohort"]["active_generation"] == up.UNKNOWN_GENERATION


# --- 11. 接線護欄：兩支優化器都必須真的呼叫消費者（防日後被刪回混代）---
def test_both_optimizers_consume_provenance():
    for mod in ("auto_optimizer.py", "entry_policy_optimizer.py"):
        src = (ROOT / "l3_dispatcher" / mod).read_text(encoding="utf-8")
        assert "generation_of_row" in src and "active_generation" in src, \
            f"{mod} 必須消費宇宙世代留痕，否則 120 天窗會再度混代算晉升"


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
