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
