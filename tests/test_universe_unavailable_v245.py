# -*- coding: utf-8 -*-
"""v245：宇宙來源「每一檔都失敗」不再折成「宇宙裡本來就沒有這些幣」。

同物種第 66 次。落點：`market_intel_mcp/sources/coinglass.py:get_strength_universe`
——**v244 那個症狀的真正根因**，也是 v242 治過的 `get_sentiment()` 一模一樣的形狀。

怎麼找到它的（v244 的線上實證把我指過來）：
    v244 讓影子層說得出成因，結果它說的是「回應成功但 items 為空」——
    不是我預期的 401。也就是說 401 在更下面一層就被吞掉了：

        if r.get("error"):   continue     # ← 401 死在這裡，不留任何痕跡
        rows = r.get("data") or []
        if not rows:         continue     # ← 「這幣沒交易對」也死在這裡
        ...
        return {"source": "coinglass", "ts": 0, "items": items}   # 沒有 error 旗標

    兩種處置完全不同的成因（續訂 vs 查資料契約）收斂成同一個空清單，而回傳的
    dict **連 error 旗標都沒有**。上層 `watchlist.py` 只看 `universe["error"]`，
    於是「全滅」被包裝成「一切正常，只是沒東西」。

⚠️ 這個處方 2026-07-28 就寫在
   `docs/監督員診斷-CoinGlass全端點停權20天-2026-07-28.md:109`——六天沒落地，
   期間 cvd_shadow 又多空轉了 6 天。診斷寫完不等於治好。

⛔ 邊界：
  * `items` 與所有訊號數學一律不動——本次只加**附帶說明的中繼資料**。
    （加欄位不改任何進出場數字 ⇒ 不需要過回測閘；一旦碰 items 就要過。）
  * 全數成功時 ⛔ 不得多出 `unavailable` 鍵（否則 sink／log 膨脹、⚠️ 貶值）。
  * 成因去重成 `{成因: [幣]}`——60 檔候選池同一句 401 時逐幣重複會讓一列遙測
    膨脹 60 倍。
  * ⛔ 不得把 `unavailable` 混進 `items`，也不得改變回傳的 `source`/`ts` 語意。
  * `watchlist.py` 的 `errored` 語意不動（既有測試鎖著 `errored is False`）——
    **加欄位，不改語意**。

改動前的碼會失敗在哪（非虛設檢定的證明）：
  * `get_strength_universe` 回的 dict 根本沒有 `unavailable` 鍵 → 下面每個
    「說得出成因」的 assert 都拿到 None
  * `universe_telemetry` 也沒有 → watchlist 那三個 assert 同樣掛
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from market_intel_mcp.sources.coinglass import CoinGlassSource


_ROW = {
    "volume_usd": "1000000000",
    "open_interest_usd": "500000000",
    "price_change_percent_24h": "1.5",
    "volume_usd_change_percent_24h": "10",
    "open_interest_change_percent_24h": "2",
    "funding_rate": "0.0001",
}

_401 = {"error": True, "code": "AUTH_FAILED",
        "message": "API key invalid or expired"}
_429 = {"error": True, "code": "RATE_LIMITED",
        "message": "CoinGlass rate limit hit"}


def _src(by_sym: dict, default=None):
    """真 CoinGlassSource（零網路：只換掉 _get）。"""
    s = CoinGlassSource()

    async def _fake_get(path, params, *, tool, symbol=None):
        return by_sym.get(symbol, default if default is not None
                          else {"data": [dict(_ROW)]})
    s._get = _fake_get   # type: ignore[method-assign]
    return s


def _universe(src, syms):
    return asyncio.run(src.get_strength_universe(limit=len(syms),
                                                 candidate_symbols=list(syms)))


# ─────────── ① 全滅：不得回一個「乾淨的空清單」 ───────────


def test_all_symbols_401_names_the_cause():
    """線上此刻的形狀：整把金鑰死了。"""
    u = _universe(_src({}, default=dict(_401)), ["BTC", "ETH", "SOL"])
    assert u["items"] == []
    assert u.get("unavailable"), \
        "全滅卻回一個沒有 error 旗標、沒有成因的空清單 ⇒ 上層永遠讀成「宇宙本來就空」"
    blob = repr(u["unavailable"])
    assert "AUTH_FAILED" in blob or "invalid or expired" in blob, blob


def test_empty_rows_is_a_different_cause_than_auth():
    """「這幣在此端點沒有交易對」要查資料契約，「401」要續訂——⛔ 不可同一句話。"""
    empty = _universe(_src({}, default={"data": []}), ["BTC", "ETH"])
    auth = _universe(_src({}, default=dict(_401)), ["BTC", "ETH"])
    assert empty["items"] == [] and auth["items"] == []
    assert set(empty["unavailable"]) != set(auth["unavailable"]), \
        f"兩種完全不同的處置給了同一句話：{empty['unavailable']} vs {auth['unavailable']}"


def test_rate_limited_is_distinguishable_from_auth():
    """429 是「等一下再來」，401 是「去續訂」——混在一起會拖著不處理。"""
    u = _universe(_src({}, default=dict(_429)), ["BTC", "ETH"])
    assert "RATE_LIMITED" in repr(u["unavailable"]) \
        and "AUTH_FAILED" not in repr(u["unavailable"])


def test_causes_are_deduped_not_one_per_symbol():
    """60 檔候選池同一句 401 ⇒ 合併成一組，⛔ 不得在遙測裡重複 60 次。"""
    syms = [f"C{i}" for i in range(30)]
    u = _universe(_src({}, default=dict(_401)), syms)
    unavail = u["unavailable"]
    assert isinstance(unavail, dict) and len(unavail) == 1, unavail
    assert sorted(next(iter(unavail.values()))) == sorted(syms)


# ─────────── ② 半死：活著的照回，死掉的要具名 ───────────


def test_partial_failure_keeps_items_and_names_the_dead():
    src = _src({"BTC": {"data": [dict(_ROW)]},
                "ETH": {"data": [dict(_ROW)]}}, default=dict(_401))
    u = _universe(src, ["BTC", "ETH", "SOL", "DOGE"])
    assert [i["symbol"] for i in u["items"]] == ["BTC", "ETH"], \
        "⛔ items 被動到了：本次修補只准加中繼資料"
    dead = sorted(s for syms in u["unavailable"].values() for s in syms)
    assert dead == ["DOGE", "SOL"]


def test_full_success_adds_no_noise_key():
    """反向側守門：全數成功 ⇒ ⛔ 不得多出 unavailable。"""
    u = _universe(_src({}), ["BTC", "ETH", "SOL"])
    assert len(u["items"]) == 3
    assert "unavailable" not in u, f"全成功卻多了雜訊鍵：{u.get('unavailable')}"


def test_shape_contract_is_unchanged():
    """既有消費端（server.py / watchlist.py / convergence_shadow）的契約不得變。"""
    u = _universe(_src({}, default=dict(_401)), ["BTC"])
    assert u["source"] == "coinglass" and u["ts"] == 0
    assert isinstance(u["items"], list)
    assert "error" not in u, \
        "⛔ 不得順手加 error 旗標：watchlist 會改走早退路徑，等於偷偷改了行為"


@pytest.mark.parametrize("payload", [
    dict(_401), dict(_429), {"data": []}, {"data": None},
    {"error": True, "code": "TIMEOUT", "message": "HTTP timeout after 10s"},
])
def test_no_cause_is_a_placeholder(payload):
    u = _universe(_src({}, default=payload), ["BTC"])
    for why in u["unavailable"]:
        assert why and why.strip()
        assert why.strip().lower() not in ("unknown", "error", "none", "n/a", "?")
        assert len(why.strip()) >= 4, f"成因太短講不出東西：{why!r}"


# ─────────── ③ 成因要一路走到 watchlist 遙測 ───────────


def _isolate_pool(monkeypatch):
    import l3_dispatcher.watchlist as wl
    monkeypatch.setattr(wl, "_market_candidates", lambda: [])
    return wl


def _mgr(wl):
    return wl.WatchlistManager(candidate_pool=("BTC", "ETH", "SOL"),
                               trading_size=2)


class _Src:
    def __init__(self, payload):
        self._p = payload

    async def get_strength_universe(self, limit, candidate_symbols=None):
        return dict(self._p)


def test_watchlist_telemetry_carries_the_cause(monkeypatch):
    """`n_universe=0 且 errored=False` 不得是唯一線索——遙測要說得出為什麼。"""
    wl = _isolate_pool(monkeypatch)
    res = asyncio.run(_mgr(wl).refresh(_Src(
        {"source": "coinglass", "ts": 0, "items": [],
         "unavailable": {"AUTH_FAILED: API key invalid or expired":
                         ["BTC", "ETH", "SOL"]}})))
    tel = res["universe_telemetry"]
    assert tel["errored"] is False, "⛔ errored 語意不得改（既有測試鎖著）"
    assert tel.get("unavailable"), \
        "宇宙全滅的成因沒進遙測 ⇒ 開機那行 log 還是只印 0/3 errored=False"
    assert "AUTH_FAILED" in repr(tel["unavailable"])


def test_watchlist_telemetry_clean_on_full_success(monkeypatch):
    """反向側：源全數成功時遙測 ⛔ 不得多出 unavailable。"""
    wl = _isolate_pool(monkeypatch)
    items = [{"symbol": s, "return_7d_pct": 1.0, "vol_24h_usd": 5e8,
              "vol_24h_vs_30d": 1.1, "oi_delta_7d_pct": 1.0,
              "cvd_slope_7d": 0.05, "top_trader_dev": 0.05,
              "btc_corr_30d": 0.7, "funding": 0.0001}
             for s in ("BTC", "ETH", "SOL")]
    res = asyncio.run(_mgr(wl).refresh(_Src(
        {"source": "coinglass", "ts": 0, "items": items})))
    tel = res["universe_telemetry"]
    assert tel["n_universe"] == 3 and tel["errored"] is False
    assert "unavailable" not in tel, f"全成功卻多了雜訊鍵：{tel.get('unavailable')}"


def test_cause_survives_the_free_fallback(monkeypatch):
    """v141 降級後 items 非空——但「CG 為什麼空」的成因 ⛔ 不得因此消失。

    這正是 26 天沒人發現的形狀：降級讓系統看起來還活著，於是沒人去看源死了。
    """
    wl = _isolate_pool(monkeypatch)
    import l3_dispatcher.free_strength_universe as fsu
    monkeypatch.setattr(fsu, "build_free_universe", lambda pool: {"items": [
        {"symbol": s, "return_7d_pct": 1.0, "vol_24h_usd": 5e8,
         "vol_24h_vs_30d": 1.1, "oi_delta_7d_pct": 1.0, "cvd_slope_7d": 0.05,
         "top_trader_dev": 0.05, "btc_corr_30d": 0.7, "funding": 0.0001}
        for s in pool]})
    res = asyncio.run(_mgr(wl).refresh(_Src(
        {"source": "coinglass", "ts": 0, "items": [],
         "unavailable": {"AUTH_FAILED: API key invalid or expired": ["BTC"]}})))
    tel = res["universe_telemetry"]
    assert tel["universe_source"] == "okx_free_fallback"
    assert "AUTH_FAILED" in repr(tel.get("unavailable")), \
        "降級成功就把源死掉的成因吞了 ⇒ 又一次「看起來正常」"


# ─────────── ④ 影子層要「轉述」源的成因，不得改講自己的猜測 ───────────


def test_cvd_shadow_relays_the_upstream_cause():
    """v244 修完後線上實證量到的缺口。

    v244 讓影子說得出成因，但它的 helper 只看得懂 `error` 旗標與 `items` 空不空，
    於是源回「items 空 + unavailable=401」時，影子講的是自己的猜測
    「該幣未進宇宙／上游聚合掉了」——**把讀的人指去查資料契約，而真因是金鑰到期**。
    說得出話但說錯方向，比沉默更貴。源既然說了，就轉述，⛔ 不得覆蓋成自己的推測。
    """
    from l3_dispatcher import cvd_shadow as cs
    why = cs._universe_miss_reason(
        {"source": "coinglass", "ts": 0, "items": [],
         "unavailable": {"AUTH_FAILED: API key invalid or expired": ["BTC"]}})
    assert "AUTH_FAILED" in why or "invalid or expired" in why, why
    assert "未進宇宙" not in why, f"源講了真因，影子還在講自己的猜測：{why}"


def test_cvd_shadow_keeps_its_own_reason_when_source_is_silent():
    """反向側：源沒說時，影子原本的話不得消失（⛔ 不得退化成空成因）。"""
    from l3_dispatcher import cvd_shadow as cs
    why = cs._universe_miss_reason({"source": "x", "ts": 0, "items": []})
    assert why and "空" in why


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
