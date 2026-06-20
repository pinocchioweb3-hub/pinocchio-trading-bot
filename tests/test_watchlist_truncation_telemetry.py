"""watchlist.refresh：宇宙截斷遙測（task#64「先量測再決定是否治」）。

get_strength_universe 對候選池每檔各打一次 /pairs-markets；遇 429/錯誤會 silently
`continue`（見 coinglass.py），導致回傳 items 少於請求數＝模型對市場部分失明。
refresh 用「請求數 vs 回傳數」算出被丟幾檔，先量測截斷率。

本檔鎖死兩件事：
  1. universe_telemetry.{n_pool,n_universe,n_dropped} 計數正確（含全收/部分丟/全丟）。
  2. **純觀測鐵則**：遙測不改 chosen——同一份 items 餵進去，回來的 chosen 與「沒有
     遙測時應產生的名單」完全一致（用 compute_strength_scores 對照）。

全離線：假 source 不打網路、零真錢、零訊號數學變更。
執行：pytest tests/test_watchlist_truncation_telemetry.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import l3_dispatcher.watchlist as wl
from l3_dispatcher.watchlist import WatchlistManager
from market_intel_mcp.strength import compute_strength_scores


def _isolate_pool(monkeypatch):
    """釘死候選池＝candidate_pool（不讀 live scanner.db，測試才確定）。
    （實機 _market_candidates 會 union 全市場掃描清單；測試需與之隔離。）"""
    monkeypatch.setattr(wl, "_market_candidates", lambda *a, **k: [])


def _item(sym: str, vol: float = 100_000_000) -> dict:
    """產生一個過得了 refresh 硬性過濾（vol/ret/funding）的合法 universe item。"""
    return {"symbol": sym, "return_7d_pct": 8.0, "vol_24h_usd": vol,
            "vol_24h_vs_30d": 1.0, "oi_delta_7d_pct": 4.0,
            "cvd_slope_7d": 0.05, "top_trader_dev": 0.05, "btc_corr_30d": 0.7}


class _FakeSource:
    """回傳「請求的前 keep 檔」，模擬截斷掉其餘的 source。"""
    def __init__(self, keep: int | None = None):
        self.keep = keep
        self.last_limit = None
        self.last_pool = None

    async def get_strength_universe(self, limit, candidate_symbols=None):
        self.last_limit = limit
        pool = list(candidate_symbols or [])
        self.last_pool = pool
        n = self.keep if self.keep is not None else len(pool)
        items = [_item(s) for s in pool[:n]]
        return {"source": "fake", "ts": 0, "items": items}


def _mgr() -> WatchlistManager:
    # 固定候選池、不碰掃描器 DB（_market_candidates 失敗會 fallback 此池）
    return WatchlistManager(
        candidate_pool=("BTC", "ETH", "SOL", "SUI", "ARB", "OP", "INJ", "TIA",
                        "APT", "NEAR", "UNI", "PEPE"),
        trading_size=8)


def test_telemetry_no_drop(monkeypatch):
    _isolate_pool(monkeypatch)
    mgr = _mgr()
    src = _FakeSource(keep=None)   # 全數回傳
    res = asyncio.run(mgr.refresh(src))
    tel = res["universe_telemetry"]
    assert tel["n_pool"] == src.last_limit == len(src.last_pool)
    assert tel["n_universe"] == tel["n_pool"]
    assert tel["n_dropped"] == 0
    assert tel["errored"] is False


def test_telemetry_counts_drop(monkeypatch):
    _isolate_pool(monkeypatch)
    mgr = _mgr()
    src = _FakeSource(keep=5)      # 只回前 5 檔，其餘截斷
    res = asyncio.run(mgr.refresh(src))
    tel = res["universe_telemetry"]
    assert tel["n_universe"] == 5
    assert tel["n_dropped"] == tel["n_pool"] - 5
    assert tel["n_dropped"] > 0


def test_telemetry_is_observation_only(monkeypatch):
    """純觀測鐵則：有遙測產生的 chosen，必須等於直接對同一份 items 評分的結果。"""
    _isolate_pool(monkeypatch)
    pool_syms = list(_mgr().candidate_pool)
    keep = 9
    items = [_item(s) for s in pool_syms[:keep]]
    # refresh 內部硬性過濾後（此處全部過得了）→ compute_strength_scores → top trading_size
    expected = [it["symbol"] for it in compute_strength_scores(items)[:8]]

    mgr = _mgr()
    res = asyncio.run(mgr.refresh(_FakeSource(keep=keep)))
    assert res["chosen"] == expected, \
        f"遙測污染了 chosen：{res['chosen']} != {expected}"
    # 遙測在場但截斷數正確，且不影響落地名單
    assert res["universe_telemetry"]["n_dropped"] == len(pool_syms) - keep


def test_telemetry_present_on_error_path(monkeypatch):
    _isolate_pool(monkeypatch)
    class _ErrSource:
        async def get_strength_universe(self, limit, candidate_symbols=None):
            return {"error": True, "message": "source down"}
    mgr = _mgr()
    res = asyncio.run(mgr.refresh(_ErrSource()))
    tel = res["universe_telemetry"]
    assert tel["errored"] is True
    assert tel["n_universe"] == 0 and tel["n_dropped"] == 0
    assert tel["n_pool"] == len(mgr.candidate_pool)   # 錯誤路徑仍記錄請求基準


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
