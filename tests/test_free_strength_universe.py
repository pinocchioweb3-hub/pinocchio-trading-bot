"""免費 OKX 大宗源強度宇宙建構器 + 影子比對（task#68 shadow-first 半）。

鎖死三件事：
  1. build_items 逐欄忠實度：return=chg×5、oi_delta=(oi/past-1)×100×3、
     vol_vs=vol/avg、三 stub 與 CoinGlass 對齊、BTC corr=1.0；缺料/0 量略過。
  2. 進階因子 stub 常數鎖死（防 CoinGlass coinglass.py:666-668 漂移後本源失準）。
  3. compare_universes 純觀測：免費源從不改 cg_chosen；覆蓋差/一致度計數正確。

全離線：load_scanner_inputs 以 monkeypatch 注入假資料，不讀 scanner.db、不打網路。
執行：pytest tests/test_free_strength_universe.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import l3_dispatcher.free_strength_universe as fu
from market_intel_mcp.strength import compute_strength_scores


def test_stub_constants_match_coinglass():
    """進階因子 stub 必須與 CoinGlass per-coin 路徑(coinglass.py:666-668)相同，
    否則 35% 權重兩路徑給不同常數 → 影子比對失去意義。"""
    assert fu._STUB_CVD == 0.0
    assert fu._STUB_TOP_TRADER == 0.05
    assert fu._STUB_BTC_CORR == 0.70


def test_build_items_fidelity():
    snap = {
        "BTC": {"last": 100.0, "vol24h_usd": 1e9, "oi_usd": 110.0,
                "funding": 0.0001, "chg24h_pct": 2.0},
        "ETH": {"last": 50.0, "vol24h_usd": 5e8, "oi_usd": 90.0,
                "funding": -0.0002, "chg24h_pct": -1.0},
    }
    oi_past = {"BTC": 100.0, "ETH": 100.0}     # BTC OI +10%, ETH OI -10%
    vol_avg = {"BTC": 5e8, "ETH": 5e8}         # BTC vol 2×均量, ETH 1×

    items = fu.build_items(["BTC", "ETH"], snap, oi_past, vol_avg)
    by = {it["symbol"]: it for it in items}

    # return_7d_pct = chg × 5
    assert by["BTC"]["return_7d_pct"] == 2.0 * 5
    assert by["ETH"]["return_7d_pct"] == -1.0 * 5
    # oi_delta_7d_pct = (oi/past - 1)×100×3
    assert by["BTC"]["oi_delta_7d_pct"] == round((110/100 - 1) * 100 * 3, 3)   # +30.0
    assert by["ETH"]["oi_delta_7d_pct"] == round((90/100 - 1) * 100 * 3, 3)    # -30.0
    # vol_24h_vs_30d = vol / avg
    assert by["BTC"]["vol_24h_vs_30d"] == round(1e9 / 5e8, 3)   # 2.0
    assert by["ETH"]["vol_24h_vs_30d"] == round(5e8 / 5e8, 3)   # 1.0
    # funding / vol 直通
    assert by["BTC"]["funding"] == 0.0001
    assert by["BTC"]["vol_24h_usd"] == 1e9
    # 三 stub
    assert by["ETH"]["cvd_slope_7d"] == 0.0
    assert by["ETH"]["top_trader_dev"] == 0.05
    assert by["ETH"]["btc_corr_30d"] == 0.70   # 非 BTC
    assert by["BTC"]["btc_corr_30d"] == 1.0    # BTC 特例


def test_build_items_skips_missing_and_zero_vol():
    snap = {"BTC": {"vol24h_usd": 1e9, "oi_usd": 1.0, "funding": 0.0,
                    "chg24h_pct": 1.0, "last": 1.0},
            "DEAD": {"vol24h_usd": 0.0, "oi_usd": 1.0, "funding": 0.0,
                     "chg24h_pct": 1.0, "last": 1.0}}
    items = fu.build_items(["BTC", "DEAD", "NOTINSNAP"], snap, {}, {})
    syms = {it["symbol"] for it in items}
    assert syms == {"BTC"}   # DEAD（0 量）、NOTINSNAP（缺料）皆略過


def test_oi_delta_neutral_when_no_history():
    """缺 24h 前 OI → oi_delta 退 0.0（不捏造）。"""
    snap = {"BTC": {"vol24h_usd": 1e9, "oi_usd": 999.0, "funding": 0.0,
                    "chg24h_pct": 1.0, "last": 1.0}}
    items = fu.build_items(["BTC"], snap, {}, {"BTC": 1e9})
    assert items[0]["oi_delta_7d_pct"] == 0.0
    assert items[0]["vol_24h_vs_30d"] == 1.0   # avg==vol → 1.0


def test_build_free_universe_uses_loader(monkeypatch):
    snap = {"BTC": {"vol24h_usd": 1e9, "oi_usd": 110.0, "funding": 0.0,
                    "chg24h_pct": 3.0, "last": 1.0}}
    monkeypatch.setattr(fu, "load_scanner_inputs",
                        lambda: (snap, {"BTC": 100.0}, {"BTC": 1e9}))
    res = fu.build_free_universe(["BTC", "GHOST"])
    assert res["source"] == "free_okx_scanner"
    assert [it["symbol"] for it in res["items"]] == ["BTC"]


def _big(sym, chg, vol=1e9):
    """過得了硬性過濾的 snap entry。"""
    return {"vol24h_usd": vol, "oi_usd": 100.0, "funding": 0.0,
            "chg24h_pct": chg, "last": 1.0}


def test_compare_is_observation_only(monkeypatch):
    """純觀測鐵則：compare 回傳的 cg_chosen 必須原封不動等於傳入值。"""
    snap = {"BTC": _big("BTC", 1.0), "ETH": _big("ETH", 2.0),
            "SOL": _big("SOL", 3.0)}
    monkeypatch.setattr(fu, "load_scanner_inputs",
                        lambda: (snap, {}, {}))
    pool = ["BTC", "ETH", "SOL"]
    cg_items = [{"symbol": "BTC"}, {"symbol": "ETH"}]   # CoinGlass 只回 2 檔（SOL 被截斷）
    cg_chosen = ["BTC", "ETH"]
    res = fu.compare_universes(pool, cg_items, cg_chosen, trading_size=2)

    assert res["cg_chosen"] == ["BTC", "ETH"]          # 未被污染
    assert res["cg_coverage"] == 2
    assert res["free_coverage"] == 3                   # 免費源涵蓋全 3 檔
    assert "SOL" in res["free_extra_covered"]          # 截斷掉的 SOL 被免費源補回
    assert res["free_extra_n"] == 1
    assert res["cg_only_n"] == 0


def test_compare_free_chosen_matches_direct_scoring(monkeypatch):
    """free_chosen 必須等於直接對「過濾後免費 items」評分取 top-N 的結果。"""
    snap = {"BTC": _big("BTC", 1.0), "ETH": _big("ETH", 5.0),
            "SOL": _big("SOL", 3.0)}
    monkeypatch.setattr(fu, "load_scanner_inputs", lambda: (snap, {}, {}))
    pool = ["BTC", "ETH", "SOL"]
    items = fu.build_items(pool, snap, {}, {})
    filtered = [it for it in items if fu._passes_strength_filter(it)]
    expected = [it["symbol"] for it in compute_strength_scores(filtered)[:2]]

    res = fu.compare_universes(pool, [{"symbol": "BTC"}], ["BTC"], trading_size=2)
    assert res["free_chosen"] == expected
    assert 0.0 <= res["topN_agreement"] <= 1.0


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
