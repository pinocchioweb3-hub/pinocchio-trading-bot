"""presence_index 純函式測試（task#33）— 離線、零網路。

執行（任一）：
    pytest tests/test_presence_index.py
    python tests/test_presence_index.py

涵蓋：三源齊全→triple_present True；只 OKX→False+shallow；某源 None 不崩；
liquidity_tier 邊界(剛好 500M/50M)；presence_score 單調且夾 [0,1]；
vol 全 None 不除零；別名收斂（RNDR/1000PEPE/MATIC/kPEPE）；
跨源對齊（OKX RNDR-USDT-SWAP / CoinGlass RENDERUSDT / HL RENDER → 同 canonical）；
input immutability。
"""
from __future__ import annotations

import copy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from l3_dispatcher.presence_index import (
    DEEP_TIER_USD,
    MEDIUM_TIER_USD,
    compute_presence,
    liquidity_tier,
    presence_score,
)
from market_intel_mcp.symbol_mapping import to_canonical_aliased


# ---------------------------------------------------------------------------
# liquidity_tier 邊界
# ---------------------------------------------------------------------------
def test_liquidity_tier_deep_boundary():
    assert liquidity_tier(DEEP_TIER_USD) == "deep"          # 剛好 500M 含等於
    assert liquidity_tier(DEEP_TIER_USD + 1) == "deep"
    assert liquidity_tier(DEEP_TIER_USD - 1) == "medium"


def test_liquidity_tier_medium_boundary():
    assert liquidity_tier(MEDIUM_TIER_USD) == "medium"      # 剛好 50M 含等於
    assert liquidity_tier(MEDIUM_TIER_USD - 1) == "shallow"


def test_liquidity_tier_none_and_nonpositive():
    assert liquidity_tier(None) == "shallow"
    assert liquidity_tier(0) == "shallow"
    assert liquidity_tier(-100) == "shallow"


# ---------------------------------------------------------------------------
# presence_score 夾界 + 單調
# ---------------------------------------------------------------------------
def test_presence_score_bounds():
    assert presence_score(0, None) == 0.0
    assert presence_score(4, 2_000_000_000) == 1.0
    # 任意組合都落在 [0,1]
    for n in (0, 1, 2, 3, 4, 8):
        for v in (None, 0, 1_000_000, 100_000_000, 5_000_000_000):
            s = presence_score(n, v)
            assert 0.0 <= s <= 1.0


def test_presence_score_monotonic_in_n():
    vol = 100_000_000
    prev = -1.0
    for n in range(0, 5):
        s = presence_score(n, vol)
        assert s >= prev      # n 增 → 不減
        prev = s


def test_presence_score_monotonic_in_vol():
    n = 2
    prev = -1.0
    for v in (None, 1_000_000, 10_000_000, 100_000_000, 1_000_000_000, 5_000_000_000):
        s = presence_score(n, v)
        assert s >= prev      # vol 增 → 不減
        prev = s


def test_presence_score_vol_none_no_divzero():
    # vol=None 不應丟例外、不除零
    assert presence_score(3, None) == round(0.6 * (3 / 4), 4)


# ---------------------------------------------------------------------------
# compute_presence
# ---------------------------------------------------------------------------
def test_triple_present_all_three():
    per = {
        "okx": {"vol24h_usd": 600_000_000},
        "binance": {"vol24h_usd": 400_000_000},
        "coinglass": {"vol24h_usd": 200_000_000},
        "hyperliquid": {"vol_usd": 50_000_000},
    }
    out = compute_presence(per)
    assert out["triple_present"] is True
    assert out["n_exchanges"] == 4
    assert out["exchanges_present"] == ["binance", "coinglass", "hyperliquid", "okx"]
    assert out["liquidity_depth_usd"] == 1_250_000_000.0
    assert out["liquidity_tier"] == "deep"
    assert 0.0 <= out["presence_score"] <= 1.0


def test_only_okx_not_triple_and_shallow():
    per = {
        "okx": {"vol24h_usd": 1_000_000},   # 僅 1M
        "binance": None,
        "coinglass": None,
        "hyperliquid": None,
    }
    out = compute_presence(per)
    assert out["triple_present"] is False
    assert out["n_exchanges"] == 1
    assert out["exchanges_present"] == ["okx"]
    assert out["liquidity_tier"] == "shallow"


def test_some_source_none_does_not_crash():
    per = {
        "okx": {"vol24h_usd": 600_000_000},
        "binance": None,                       # 缺料
        "coinglass": {"vol24h_usd": 100_000_000},
        "hyperliquid": None,
    }
    out = compute_presence(per)
    assert out["triple_present"] is False       # binance 缺 → 非三源
    assert out["n_exchanges"] == 2
    assert set(out["exchanges_present"]) == {"okx", "coinglass"}


def test_all_vol_none_no_divzero():
    per = {
        "okx": {"last": 100.0},                 # 沒有任何量欄
        "binance": {"foo": 1},
        "coinglass": {"bar": 2},
    }
    out = compute_presence(per)
    assert out["liquidity_depth_usd"] == 0.0
    assert out["liquidity_tier"] == "shallow"
    assert out["triple_present"] is True        # 三源 present（只是無量）
    assert 0.0 <= out["presence_score"] <= 1.0


def test_alt_vol_key_day_notional():
    # HL 用 day_notional_volume_usd；應被 _extract_vol 抓到
    per = {"hyperliquid": {"day_notional_volume_usd": 80_000_000}}
    out = compute_presence(per)
    assert out["liquidity_depth_usd"] == 80_000_000.0
    assert out["liquidity_tier"] == "medium"


def test_empty_input():
    out = compute_presence({})
    assert out["n_exchanges"] == 0
    assert out["exchanges_present"] == []
    assert out["triple_present"] is False
    assert out["liquidity_depth_usd"] == 0.0
    assert out["presence_score"] == 0.0


def test_compute_presence_input_immutability():
    per = {
        "okx": {"vol24h_usd": 600_000_000},
        "binance": None,
        "coinglass": {"vol24h_usd": 100_000_000},
    }
    snapshot = copy.deepcopy(per)
    _ = compute_presence(per)
    assert per == snapshot       # 輸入未被修改


# ---------------------------------------------------------------------------
# 別名收斂 + 跨源對齊
# ---------------------------------------------------------------------------
def test_alias_rndr_to_render():
    assert to_canonical_aliased("RNDR") == "RENDER"
    assert to_canonical_aliased("RNDR-USDT-SWAP", "okx") == "RENDER"


def test_alias_1000pepe_and_kpepe():
    assert to_canonical_aliased("1000PEPE") == "PEPE"
    assert to_canonical_aliased("kPEPE") == "PEPE"
    assert to_canonical_aliased("1000PEPE-USDT-SWAP", "okx") == "PEPE"


def test_alias_matic_to_pol():
    assert to_canonical_aliased("MATIC") == "POL"
    assert to_canonical_aliased("MATICUSDT", "coinglass") == "POL"


def test_alias_ftm_sats_shib():
    assert to_canonical_aliased("FTM") == "S"
    assert to_canonical_aliased("1000SATS") == "SATS"
    assert to_canonical_aliased("kSHIB") == "SHIB"
    assert to_canonical_aliased("1000SHIB") == "SHIB"


def test_cross_source_alignment_render():
    # OKX RNDR-USDT-SWAP、CoinGlass RENDERUSDT、HL RENDER → 同 canonical
    a = to_canonical_aliased("RNDR-USDT-SWAP", "okx")
    b = to_canonical_aliased("RENDERUSDT", "coinglass")
    c = to_canonical_aliased("RENDER", "canonical")
    assert a == b == c == "RENDER"


def test_alias_passthrough_for_unmapped():
    # 未列入別名表者原樣（去後綴 + 大寫）通過
    assert to_canonical_aliased("BTC-USDT-SWAP", "okx") == "BTC"
    assert to_canonical_aliased("ETHUSDT", "coinglass") == "ETH"
    assert to_canonical_aliased("sol") == "SOL"


def test_existing_to_canonical_unchanged():
    # ADDITIVE 保證：既有 to_canonical 行為一個字不變（RNDR 不被別名併）
    from market_intel_mcp.symbol_mapping import to_canonical
    assert to_canonical("RNDR-USDT-SWAP", "okx") == "RNDR"
    assert to_canonical("MATICUSDT", "coinglass") == "MATIC"
    assert to_canonical("sui", "canonical") == "SUI"


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
