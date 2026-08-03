# -*- coding: utf-8 -*-
"""v246：第三源「問不到」不再寫成「這幣不在 CoinGlass 上」。

同物種第 67 次。落點：`l3_dispatcher/convergence_shadow.py` +
`l3_dispatcher/macro.py` 的焦點橫幅。**這是本物種目前最嚴重的一次**——
前面幾次是「該說話時沉默」，這一次是**把假話寫進觀測 sink**。

`_coinglass_focus_map()` 的 docstring 自己就寫著：

    缺料/失敗/未覆蓋的幣不入 map（不在 map = CoinGlass 未覆蓋該幣）

「不在 map」＝「未覆蓋」是**定義式的折疊**：把「我沒問到」直接定義成
「市場上沒有」。外加整段 `except Exception: return {}` ⇒ 一個例外讓整張表消失，
於是每一檔都變成「CoinGlass 未覆蓋」。下游 `triple_present: false` 是一個
**關於市場的事實主張**，而真相是我們連問都沒問到。

量到的下場（2026-08-03 讀 sink）：
    convergence_shadow.jsonl        1066 輪 / 12,792 筆焦點紀錄
                                    triple_present=true 的筆數：**0**
    convergence_shadow.jsonl.1      13,920 筆中 11,564 筆 true
                                    最後一筆 true：2026-07-08T13:49Z
7/08 正是 CoinGlass 方案到期日（與 cvd_shadow 同一刻）。

而它有一個**使用者看得見的**消費端：`macro.py:1328` 的 deepdive 焦點橫幅
`if it.get("triple_present")`——26 天來每一筆都是 false ⇒ 橫幅被濾成空字串 ⇒
**這個功能靜默熄燈 26 天，畫面上什麼都沒有，也沒有任何一個字說為什麼。**

⛔ 邊界：
  * 影子鐵則不動：`strength_multiplier_SHADOW` 仍永不施用於 strength/fire。
  * `presence_index.triple_present`（另一條線，由三源 presence 算出）⛔ 不碰——
    那個 False 是真的量到「Binance 沒這幣」，不是問不到。同名不同物。
  * 橫幅在「確實問到、確實沒有共現」時仍回 ""（⛔ 不製造 ⚠️ 雜訊，否則符號貶值）。
  * ⛔ 不得把 unknown 當成 true 混進 n_triple_confirmed 去墊高數字。

改動前的碼會失敗在哪（非虛設檢定的證明）：
  * `_coinglass_focus_map` 只回一個 dict，沒有第二個回傳值 → 解包就炸
  * focus 項的 `triple_present` 恆為 bool，永遠不會是 None
  * summary 沒有 `n_triple_unknown` / `cg_unavailable`
  * 橫幅在問不到時回 ""，不會出現任何成因字樣
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from l3_dispatcher import convergence_shadow as cvs


class _Src:
    """假 source：payload 直接當 get_strength_universe 的回應；BaseException 則拋出。"""

    def __init__(self, payload):
        self._p = payload

    async def get_strength_universe(self, limit, candidate_symbols=None):
        if isinstance(self._p, BaseException):
            raise self._p
        return dict(self._p)


def _map(payload, syms=("BTC", "ETH")):
    return asyncio.run(cvs._coinglass_focus_map(_Src(payload), list(syms)))


_ITEM = {"symbol": "BTC", "funding": 0.0001, "vol_24h_usd": 5e8}
_UNAVAIL = {"AUTH_FAILED: API key invalid or expired": ["BTC", "ETH"]}


# ─────────── ① 「問不到」與「問到了、沒有」必須分得開 ───────────


def test_upstream_unavailable_is_reported_not_swallowed():
    _m, reasons = _map({"source": "coinglass", "ts": 0, "items": [],
                        "unavailable": dict(_UNAVAIL)})
    assert reasons, "上游明講了成因,卻被吞成一張空表 ⇒ 下游只能讀成「這些幣不存在」"
    assert "AUTH_FAILED" in repr(reasons)


def test_error_response_is_reported():
    _m, reasons = _map({"error": True, "code": "RATE_LIMITED",
                        "message": "CoinGlass rate limit hit"})
    assert "RATE_LIMITED" in repr(reasons)


def test_exception_does_not_silently_empty_the_table():
    """`except Exception: return {}` ⇒ 一個例外讓每一檔都變成「CG 未覆蓋」。"""
    _m, reasons = _map(TimeoutError("boom"))
    assert reasons and "TimeoutError" in repr(reasons)


def test_genuine_absence_is_not_an_unavailability():
    """反向側：上游好好回答了、就是沒這幣 ⇒ ⛔ 不得謊報成故障。"""
    m, reasons = _map({"source": "coinglass", "ts": 0, "items": [dict(_ITEM)]})
    assert "BTC" in m
    assert not reasons, f"問到了卻報成故障：{reasons}"


# ─────────── ② triple_present 要有第三種狀態 ───────────


_PRESENCE = {
    "BTC": {"exchanges_present": ["okx", "binance"], "liquidity_tier": "deep",
            "liquidity_depth_usd": 2e7, "presence_score": 1.0},
    "ETH": {"exchanges_present": ["okx", "binance"], "liquidity_tier": "deep",
            "liquidity_depth_usd": 1e7, "presence_score": 1.0},
}


def _cycle_focus(monkeypatch, payload):
    """跑一輪,只取 focus 與 summary（擋掉所有外部 I/O)。"""
    import l3_dispatcher.presence_index as pi
    import market_intel_mcp.sources.binance_perp as bnm
    import market_intel_mcp.sources.hyperliquid as hlm

    async def _presence(**_kw):
        return {k: dict(v) for k, v in _PRESENCE.items()}

    async def _no_hl(_hl):
        return {}

    async def _no_bn(_bn, _s):
        return None

    monkeypatch.setattr(pi, "_load_okx_snapshot", lambda: {})
    monkeypatch.setattr(pi, "collect_presence_universe", _presence)
    monkeypatch.setattr(bnm, "get_binance_perp", lambda *a, **k: object())
    monkeypatch.setattr(hlm, "HyperliquidSource", lambda *a, **k: object())
    monkeypatch.setattr(cvs, "_hl_funding_map", _no_hl)
    monkeypatch.setattr(cvs, "_binance_funding", _no_bn)
    return asyncio.run(cvs._run_cycle(source=_Src(payload)))


def test_unknown_is_not_written_as_false(monkeypatch):
    """核心：問不到時 ⛔ 不得寫 false（那是一個關於市場的事實主張）。"""
    s = _cycle_focus(monkeypatch, {"source": "coinglass", "ts": 0, "items": [],
                                   "unavailable": dict(_UNAVAIL)})
    for it in s["focus"]:
        assert it["triple_present"] is not False, \
            f"{it['symbol']}：問不到卻宣稱「CoinGlass 上沒有這幣」"
        assert it["triple_present"] is None


def test_unknown_never_inflates_the_confirmed_count(monkeypatch):
    """⛔ 反向:未知不得混進 n_triple_confirmed 墊高數字。"""
    s = _cycle_focus(monkeypatch, {"source": "coinglass", "ts": 0, "items": [],
                                   "unavailable": dict(_UNAVAIL)})
    assert s["n_triple_confirmed"] == 0
    assert s.get("n_triple_unknown") == 2, \
        "0 confirmed 同時代表「都沒共現」與「都沒問到」——要分得出來"
    assert "AUTH_FAILED" in repr(s.get("cg_unavailable"))


def test_confirmed_path_unchanged(monkeypatch):
    """反向側：CG 活著時行為完全不變（true/false 照舊、⛔ 不多雜訊鍵）。"""
    s = _cycle_focus(monkeypatch, {"source": "coinglass", "ts": 0,
                                   "items": [dict(_ITEM)]})
    got = {it["symbol"]: it["triple_present"] for it in s["focus"]}
    assert got == {"BTC": True, "ETH": False}, got
    assert s["n_triple_confirmed"] == 1
    assert not s.get("n_triple_unknown")
    assert "cg_unavailable" not in s


# ─────────── ③ 橫幅熄燈不得沉默 ───────────


def _banner(monkeypatch, tmp_path, rec):
    import l3_dispatcher.macro as mc
    import botpaths
    monkeypatch.setattr(botpaths, "data_dir", lambda: tmp_path)
    (tmp_path / "convergence_shadow.jsonl").write_text(
        json.dumps(rec, ensure_ascii=False) + "\n", encoding="utf-8")
    return mc._shadow_convergence_focus_line()


def test_banner_says_why_it_is_dark(monkeypatch, tmp_path):
    """熄燈 26 天沒人知道為什麼 ⇒ 問不到就要講出來。"""
    line = _banner(monkeypatch, tmp_path, {
        "focus": [{"symbol": "BTC", "triple_present": None,
                   "convergence_score": 0.9}],
        "n_triple_unknown": 1,
        "cg_unavailable": {"AUTH_FAILED: API key invalid or expired": ["BTC"]}})
    assert line.strip(), "問不到時橫幅回空字串 ⇒ 功能靜默熄燈,這正是要治的病"
    assert "AUTH_FAILED" in line or "讀不到" in line or "無法確認" in line, line


def test_banner_never_claims_convergence_when_unknown(monkeypatch, tmp_path):
    """⛔ 紅線③：問不到時絕不得出現「方向一致」這種事實主張。"""
    line = _banner(monkeypatch, tmp_path, {
        "focus": [{"symbol": "BTC", "triple_present": None,
                   "convergence_score": 0.9}],
        "cg_unavailable": {"AUTH_FAILED: x": ["BTC"]}})
    assert "方向一致" not in line, line
    assert "BTC" not in line, f"未確認的幣不得被列進焦點榜：{line}"


def test_banner_silent_on_genuine_no_convergence(monkeypatch, tmp_path):
    """反向側：確實問到了、就是沒共現 ⇒ 仍回 ""（⛔ 不製造 ⚠️ 雜訊）。"""
    line = _banner(monkeypatch, tmp_path, {
        "focus": [{"symbol": "BTC", "triple_present": False,
                   "convergence_score": 0.1}]})
    assert line == ""


def test_banner_unchanged_when_healthy(monkeypatch, tmp_path):
    line = _banner(monkeypatch, tmp_path, {
        "focus": [{"symbol": "BTC", "triple_present": True,
                   "convergence_score": 0.9},
                  {"symbol": "ETH", "triple_present": True,
                   "convergence_score": 0.95}]})
    assert "ETH" in line and "BTC" in line and "方向一致" in line


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
