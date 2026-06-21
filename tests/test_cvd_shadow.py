"""task#20 第二步 影子接線：cvd_shadow 記錄併記純函式 契約測試（離線、零網路、零訊號變更）。

鎖住影子 worker 的「資料併記」不變量，避免日後改動悄悄：
  1. 把 Binance 自算 CVD 寫回了 universe/strength（影子鐵則禁止——這裡只驗 _build_item 不混欄）。
  2. Binance 缺料時捏造數字（紅線③——必須誠實 None / binance_ok=False）。
  3. 夾帶非白名單因子鍵進 sink。

全離線：不 import daemon 來源、不載 .env、不觸網（只測純函式 _build_item / _selftest）。
"""
import asyncio
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from l3_dispatcher.cvd_shadow import (
    _build_item, _selftest, _FACTOR_KEYS, _universe_factors, _span_seconds,
    _backfill_factors,
)
from market_intel_mcp.symbol_mapping import TRADING_CANDIDATES


_FACTORS = {
    "return_7d_pct": 5.0, "vol_24h_usd": 1e9, "vol_24h_vs_30d": 1.2,
    "oi_delta_7d_pct": 3.0, "cvd_slope_7d": 0.0, "top_trader_dev": 0.05,
    "btc_corr_30d": 0.7, "funding": 0.0001,
}
_CVD = {"cvd": 1234.5, "cvd_slope": 8.9, "cvd_slope_7d": 12.34,
        "series": [{"ts": i, "value": float(i)} for i in range(168)], "deltas": []}


def test_build_item_keeps_stub_and_binance_side_by_side():
    item = _build_item("BTC", _FACTORS, _CVD)
    # universe 路徑的 0.0 缺口被忠實保留（不被 Binance 值覆寫）
    assert item["stub_cvd_slope_7d"] == 0.0
    assert item["factors_live"]["cvd_slope_7d"] == 0.0
    # 該補的 Binance 值並排記錄（但活在獨立鍵，從不回寫 factors_live）
    assert item["binance_cvd_slope_7d"] == 12.34
    assert item["binance_cvd_slope"] == 8.9
    assert item["binance_cvd"] == 1234.5
    assert item["binance_bars"] == 168
    assert item["binance_ok"] is True


def test_build_item_never_overwrites_live_factor_with_binance():
    # 影子鐵則：binance 值絕不汙染 strength 排名器吃的 factors_live
    item = _build_item("ETH", _FACTORS, _CVD)
    assert item["factors_live"]["cvd_slope_7d"] != item["binance_cvd_slope_7d"]
    assert item["factors_live"]["cvd_slope_7d"] == 0.0


def test_build_item_honest_none_when_binance_missing():
    item = _build_item("SOL", _FACTORS, None)
    assert item["binance_cvd_slope_7d"] is None
    assert item["binance_cvd_slope"] is None
    assert item["binance_cvd"] is None
    assert item["binance_bars"] == 0
    assert item["binance_ok"] is False
    # 因子仍完整保留（缺的只有 Binance 那半）
    assert item["factors_live"]["return_7d_pct"] == 5.0


def test_build_item_factor_key_whitelist():
    noisy = dict(_FACTORS, secret="x", _internal=1, password="leak")
    item = _build_item("DOGE", noisy, _CVD)
    assert set(item["factors_live"].keys()) == set(_FACTOR_KEYS)
    assert "secret" not in item["factors_live"]
    assert "password" not in item["factors_live"]


def test_build_item_missing_factor_becomes_none():
    # 因子 dict 缺某鍵 → 該鍵記 None（不 KeyError、不捏造）
    partial = {"return_7d_pct": 1.0}
    item = _build_item("APT", partial, None)
    assert item["factors_live"]["vol_24h_usd"] is None
    assert item["factors_live"]["cvd_slope_7d"] is None
    assert item["stub_cvd_slope_7d"] is None


def test_module_selftest_passes():
    assert _selftest() == 0


# ════════════════════════════════════════════════════════════════════════
#  v63 修：universe 逐幣節流取數（避開 burst 被上游 429 截斷成 ~2–5 筆）
# ════════════════════════════════════════════════════════════════════════
class _PerSymbolSource:
    """模擬 live get_strength_universe 的逐幣契約：每次只回被問的那批 symbol。"""
    def __init__(self):
        self.calls = []  # 記每次呼叫的 candidate_symbols

    async def get_strength_universe(self, limit, candidate_symbols=None):
        self.calls.append(list(candidate_symbols) if candidate_symbols else None)
        syms = list(candidate_symbols or [])[:limit]
        return {"source": "fake", "ts": 0,
                "items": [{"symbol": s, "cvd_slope_7d": 0.0,
                           "return_7d_pct": 1.0} for s in syms]}


def test_universe_factors_paced_per_symbol_never_bursts():
    # 逐幣呼叫（每次只問 1 個 symbol）→ 永不 burst 一次問 N 個 → 不會被上游 429 截斷
    fake = _PerSymbolSource()
    items = asyncio.run(_universe_factors(fake, 5, pace=0))
    assert len(fake.calls) == 5
    assert all(c is not None and len(c) == 1 for c in fake.calls)
    # 完整捕捉整個 universe（不被截斷成 ~2 筆，這就是 v63 修的核心）
    assert len(items) == 5
    assert [it["symbol"] for it in items] == list(TRADING_CANDIDATES)[:5]


def test_universe_factors_reproduces_live_universe_order():
    # 逐幣重現的 universe 必須＝live 路徑（無 candidate_symbols → TRADING_CANDIDATES[:n]）
    fake = _PerSymbolSource()
    items = asyncio.run(_universe_factors(fake, 8, pace=0))
    assert [it["symbol"] for it in items] == list(TRADING_CANDIDATES)[:8]


def test_universe_factors_tolerates_individual_symbol_failure():
    # 個別幣失敗（429/error/raise）→ 跳過該幣、其餘照常捕捉，絕不拖垮整輪
    target_fail = list(TRADING_CANDIDATES)[1]
    target_raise = list(TRADING_CANDIDATES)[2]

    class _FlakySource:
        async def get_strength_universe(self, limit, candidate_symbols=None):
            sym = (candidate_symbols or [None])[0]
            if sym == target_fail:
                return {"error": "rate_limited"}
            if sym == target_raise:
                raise RuntimeError("transient")
            return {"items": [{"symbol": sym, "cvd_slope_7d": 0.0}]}

    items = asyncio.run(_universe_factors(_FlakySource(), 4, pace=0))
    syms = [it["symbol"] for it in items]
    assert target_fail not in syms and target_raise not in syms
    assert len(items) == 2  # 4 個候選 - 2 個失敗


def test_universe_factors_no_source_returns_empty():
    assert asyncio.run(_universe_factors(None, 5, pace=0)) == []

    class _NoMethod:
        pass
    assert asyncio.run(_universe_factors(_NoMethod(), 5, pace=0)) == []


# ════════════════════════════════════════════════════════════════════════
#  v63 修：補抓回合 + 同 T 橫斷面驗證（captured_ts / span_sec）
# ════════════════════════════════════════════════════════════════════════
class _RecoveringSource:
    """前 K 幣第一趟假裝被 429（error），補抓回合才成功——驗 retries 把 miss 撈回來。"""
    def __init__(self, fail_first):
        self.fail_first = set(fail_first)
        self.seen = {}  # sym -> 被問過幾次

    async def get_strength_universe(self, limit, candidate_symbols=None):
        sym = (candidate_symbols or [None])[0]
        self.seen[sym] = self.seen.get(sym, 0) + 1
        if sym in self.fail_first and self.seen[sym] == 1:
            return {"error": "rate_limited"}   # 第一趟瞬時 429
        return {"items": [{"symbol": sym, "cvd_slope_7d": 0.0}]}


def test_universe_factors_retry_recovers_missed_symbols():
    # 前 3 幣第一趟 miss，補抓回合（預設 retries=2）必須把它們全撈回 → 不被截斷
    cands = list(TRADING_CANDIDATES)[:6]
    src = _RecoveringSource(cands[:3])
    items = asyncio.run(_universe_factors(src, 6, pace=0))   # 用模組預設 retries=2
    syms = [it["symbol"] for it in items]
    assert syms == cands                                     # 完整 6 幣、canonical 順序
    # 每個被捕捉的 item 都釘了取到時刻（離線同 T 橫斷面驗證需要）
    assert all(it.get("_captured_ts") for it in items)


def test_universe_factors_stashes_captured_ts_on_success():
    fake = _PerSymbolSource()
    items = asyncio.run(_universe_factors(fake, 4, pace=0))
    assert len(items) == 4
    assert all(isinstance(it.get("_captured_ts"), str) and it["_captured_ts"]
               for it in items)


def test_build_item_carries_captured_ts():
    item = _build_item("BTC", _FACTORS, _CVD, captured_ts="2026-01-01T00:00:05+00:00")
    assert item["captured_ts"] == "2026-01-01T00:00:05+00:00"
    # 未傳 → 誠實 None（不捏造，紅線③）
    assert _build_item("ETH", _FACTORS, _CVD)["captured_ts"] is None


def test_span_seconds_computes_first_to_last_skew():
    ts = ["2026-01-01T00:00:00+00:00", "2026-01-01T00:01:00+00:00",
          None, "2026-01-01T00:00:30+00:00"]
    assert _span_seconds(ts) == 60.0          # max(60s) - min(0s)
    # 不足 2 筆可解析 / 全壞值 → 誠實 None，不丟例外
    assert _span_seconds([]) is None
    assert _span_seconds(["2026-01-01T00:00:00+00:00"]) is None
    assert _span_seconds([None, None]) is None
    assert _span_seconds(["bad-ts", "worse"]) is None


# ════════════════════════════════════════════════════════════════════════
#  v65 因子回補（top_trader_dev / btc_corr_30d / vol_24h_vs_30d）並排記錄
# ════════════════════════════════════════════════════════════════════════
_BACKFILL = {"top_trader_ratio": 1.93, "top_trader_dev": 0.93,
             "btc_corr_30d": 0.42, "vol_24h_vs_30d": 1.8, "daily_bars": 34}


def test_build_item_carries_backfill_side_by_side():
    item = _build_item("BTC", _FACTORS, _CVD, backfill=_BACKFILL)
    assert item["binance_top_trader_ratio"] == 1.93
    assert item["binance_top_trader_dev"] == 0.93
    assert item["binance_btc_corr_30d"] == 0.42
    assert item["binance_vol_24h_vs_30d"] == 1.8
    assert item["binance_daily_bars"] == 34


def test_build_item_backfill_never_overwrites_factors_live():
    # 影子鐵則：回補的正確值絕不汙染 strength 排名器吃的 factors_live（仍是死值缺口）
    item = _build_item("ETH", _FACTORS, _CVD, backfill=_BACKFILL)
    assert item["factors_live"]["top_trader_dev"] == 0.05      # 死值 stub 原封不動
    assert item["factors_live"]["btc_corr_30d"] == 0.7         # 死值 stub 原封不動
    assert item["factors_live"]["vol_24h_vs_30d"] == 1.2       # 反符號 stub 原封不動
    # 並排的回補值與 live 值不同源、不互通
    assert item["binance_top_trader_dev"] != item["factors_live"]["top_trader_dev"]


def test_build_item_honest_none_when_backfill_missing():
    item = _build_item("SOL", _FACTORS, _CVD)            # 不傳 backfill
    assert item["binance_top_trader_ratio"] is None
    assert item["binance_top_trader_dev"] is None
    assert item["binance_btc_corr_30d"] is None
    assert item["binance_vol_24h_vs_30d"] is None
    assert item["binance_daily_bars"] == 0
    # factors_live 仍完整（缺的只有回補那半）
    assert item["factors_live"]["return_7d_pct"] == 5.0


def test_build_item_factor_key_whitelist_unaffected_by_backfill():
    # 加了 backfill 後 factors_live 白名單仍嚴格（回補值活在 binance_* 不混入）
    noisy = dict(_FACTORS, secret="x", password="leak")
    item = _build_item("DOGE", noisy, _CVD, backfill=_BACKFILL)
    assert set(item["factors_live"].keys()) == set(_FACTOR_KEYS)
    assert "secret" not in item["factors_live"]


def test_backfill_factors_btc_self_corr_and_dev():
    bf = _backfill_factors("BTC", 1.5, [100, 101, 102], [10, 20, 30],
                           [100, 101, 102])
    assert bf["btc_corr_30d"] == 1.0          # BTC 對自己定義為 1.0
    assert bf["top_trader_dev"] == 0.5        # 1.5 − 1
    assert bf["daily_bars"] == 3
    # 之前日數 < min_days=7 → vol 誠實 None（不捏造，紅線③）
    assert bf["vol_24h_vs_30d"] is None


def test_backfill_factors_honest_none_on_missing():
    bf = _backfill_factors("ETH", None, None, None, [100, 101])
    assert bf["top_trader_ratio"] is None
    assert bf["top_trader_dev"] is None       # ratio None → dev None
    assert bf["btc_corr_30d"] is None         # 無 sym_closes → None
    assert bf["vol_24h_vs_30d"] is None
    assert bf["daily_bars"] == 0


def test_backfill_factors_vol_sign_and_corr_use_returns():
    # 量增 → 比值 > 1（修正反符號）；需 ≥ min_days+1 日量
    bf = _backfill_factors("XRP", 1.0,
                           [100.0] * 40,                 # 平盤收盤 → corr 零變異 → None
                           [100.0] * 39 + [300.0],       # 24h=300、之前 39 日均=100 → 3.0
                           [100.0] * 40)
    assert bf["vol_24h_vs_30d"] is not None and bf["vol_24h_vs_30d"] > 1.0
    # 平盤序列日報酬零變異 → Pearson 未定義 → 誠實 None（不假裝 1.0）
    assert bf["btc_corr_30d"] is None
