# -*- coding: utf-8 -*-
"""v243：ETF overlay「抓取死了」不再折成「報告上本來就沒有這一行」。

同物種第 64 次（第 63 次是 r135，落在測試自己身上）。承 v242（情緒源）——
這是**同一天、同一把金鑰失效**的另一個受害者。

`fetch_etf_overlay()` 的每一條失敗路徑都回同一個 `None`：

    ① 沒有 CG 金鑰
    ② 連線層例外
    ③ 單一資產：回應沒有 data／窗口不足 5 日／例外  → `continue`（該資產靜默消失）
    ④ 四個資產全滅 → parts 空 → `return None`

而消費端是 `collect_bottom_inputs()` 裡的 `if etf: overlay["🏦"] = etf`——
None 就是**那一行不存在**。於是報告上「看不到 ETF 淨流」同時代表
「這個源沒資料」和「這個源死了」，讀報告的人分不出來。

2026-08-03 唯讀稽核量到 `etf_overlay=None`。我當時把它歸成「非 CG 的免費源、
另有成因」——讀碼後推翻：它打的是 `open-api-v4.coinglass.com/api/etf/...`，
帶同一把 `COINGLASS_API_KEY`，就是同一個 401。**分組是我的推測，不是量測。**

值得記著的是這個函式已經對過兩次：第 255 行「未知≠零淨流」不拿 0 充數、
v212 不把殘缺窗口折成「30 日」。**洞只剩最外層那個 None。**

⛔ 邊界（v238 已立）：「沒接這個源」不是「量不到」。沒有 CG 金鑰時仍回 None、
   報告上不多一列假警訊——否則 ⚠️ 這個符號會貶值，等於用另一種方式製造失明。

改動前的碼會失敗在哪（非虛設檢定的證明）：
  * 全滅時回 None，`s is None` → 下面每一個「要說出成因」的 assert 都掛
  * 部分成功時 missing 名單根本不存在 → 沒有 ⚠️ 未取得那一段
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from l3_dispatcher import bottom_feeds as bf


class _Resp:
    def __init__(self, payload, status_code=200):
        self._p = payload
        self.status_code = status_code

    def json(self):
        return self._p


class _CG:
    """假 CoinGlass client：by_coin 給 payload（list=正常 data；dict=整包 body）。"""

    def __init__(self, by_coin: dict):
        self._by_coin = by_coin

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, path):
        coin = path.split("/")[3]
        v = self._by_coin.get(coin, [])
        if isinstance(v, BaseException):
            raise v
        if isinstance(v, dict):
            return _Resp(v, v.pop("_status", 200))
        return _Resp({"data": v})


@pytest.fixture(autouse=True)
def _no_cache(monkeypatch):
    monkeypatch.setattr(bf, "_cached", lambda key: None)
    monkeypatch.setattr(bf, "_put", lambda key, data: None)
    monkeypatch.setenv("COINGLASS_API_KEY", "dummy")


def _flows(n: int, per_day: float = 10e6) -> list:
    return [{"flow_usd": per_day} for _ in range(n)]


_401 = {"_status": 401, "code": "API_ERROR", "msg": "Upgrade plan"}


# ─────────────── ① 全滅：必須留下一行，並說出成因 ───────────────


def test_all_assets_401_says_so(monkeypatch):
    """線上此刻的形狀：四個資產全 401。"""
    monkeypatch.setattr(bf.httpx, "Client", lambda *a, **k: _CG(
        {c: dict(_401) for c in ("bitcoin", "ethereum", "xrp", "solana")}))
    s = bf.fetch_etf_overlay()
    assert s is not None, "全部讀不到 → 整行從報告上消失，讀報告的人以為這源本來就沒資料"
    assert "讀不到" in s
    assert "Upgrade plan" in s, f"成因沒帶出來：{s}"


def test_connection_layer_failure_is_named(monkeypatch):
    """連線層整個炸掉（DNS／TLS／代理）——不可與「沒資料」同一個下場。"""
    def _boom(*a, **k):
        raise OSError("dns fail")
    monkeypatch.setattr(bf.httpx, "Client", _boom)
    s = bf.fetch_etf_overlay()
    assert s is not None and "讀不到" in s
    assert "OSError" in s, f"連線層成因被吞掉：{s}"


def test_per_asset_exception_is_named(monkeypatch):
    monkeypatch.setattr(bf.httpx, "Client", lambda *a, **k: _CG(
        {c: TimeoutError("t") for c in ("bitcoin", "ethereum", "xrp", "solana")}))
    s = bf.fetch_etf_overlay()
    assert s is not None and "TimeoutError" in s


def test_short_window_reason_differs_from_api_error(monkeypatch):
    """「只有 3 天」與「401」是兩種處置，⛔ 不可給同一句話。"""
    monkeypatch.setattr(bf.httpx, "Client", lambda *a, **k: _CG(
        {c: _flows(3) for c in ("bitcoin", "ethereum", "xrp", "solana")}))
    short = bf.fetch_etf_overlay()
    monkeypatch.setattr(bf.httpx, "Client", lambda *a, **k: _CG(
        {c: dict(_401) for c in ("bitcoin", "ethereum", "xrp", "solana")}))
    err = bf.fetch_etf_overlay()
    assert short != err
    assert "3" in short and "5" in short, f"窗口不足要講清楚差多少：{short}"


def test_failure_line_carries_no_fabricated_figure(monkeypatch):
    """⛔ v212 的鐵則不得倒退：失敗那一行不准出現任何看似淨流的數字。"""
    monkeypatch.setattr(bf.httpx, "Client", lambda *a, **k: _CG(
        {c: _flows(3) for c in ("bitcoin", "ethereum", "xrp", "solana")}))
    s = bf.fetch_etf_overlay()
    assert "M" not in s.replace("Metrics", ""), f"失敗行裡出現了金額：{s}"
    assert "5日+" not in s and "30日+" not in s


# ─────────────── ② 半死：活著的照報，死掉的要具名 ───────────────


def test_partial_failure_reports_both_sides(monkeypatch):
    monkeypatch.setattr(bf.httpx, "Client", lambda *a, **k: _CG(
        {"bitcoin": _flows(30), "ethereum": _flows(30),
         "xrp": dict(_401), "solana": dict(_401)}))
    s = bf.fetch_etf_overlay()
    assert "BTC" in s and "ETH" in s, "活著的兩個要照報"
    assert "XRP" in s and "SOL" in s, "死掉的兩個不得靜默消失"
    assert "未取得" in s and "Upgrade plan" in s


def test_full_success_line_is_unchanged(monkeypatch):
    """反向側守門：四個都活 ⇒ 不得多出任何 ⚠️ 雜訊（否則符號會貶值）。"""
    monkeypatch.setattr(bf.httpx, "Client", lambda *a, **k: _CG(
        {c: _flows(30) for c in ("bitcoin", "ethereum", "xrp", "solana")}))
    s = bf.fetch_etf_overlay()
    assert s.startswith("ETF淨流(")
    assert "未取得" not in s and "讀不到" not in s
    for tag in ("BTC", "ETH", "XRP", "SOL"):
        assert f"{tag} 5日" in s


# ─────────────── ③ 「不適用」不得標成「量不到」 ───────────────


def test_no_api_key_stays_silent(monkeypatch):
    """沒設 CG 金鑰＝這個源沒接（by-design），⛔ 不得在報告上多一列假警訊。"""
    monkeypatch.setenv("COINGLASS_API_KEY", "")
    assert bf.fetch_etf_overlay() is None


# ─────────────── ④ 失敗不進每日快取（否則恢復要等 20 小時才看得到） ───────────────


def test_failure_is_not_cached(monkeypatch):
    puts = []
    monkeypatch.setattr(bf, "_put", lambda key, data: puts.append((key, data)))
    monkeypatch.setattr(bf.httpx, "Client", lambda *a, **k: _CG(
        {c: dict(_401) for c in ("bitcoin", "ethereum", "xrp", "solana")}))
    bf.fetch_etf_overlay()
    assert puts == [], "把失敗字串寫進 20 小時快取 → 源恢復了報告還在說它死著"


def test_success_is_still_cached(monkeypatch):
    puts = []
    monkeypatch.setattr(bf, "_put", lambda key, data: puts.append((key, data)))
    monkeypatch.setattr(bf.httpx, "Client", lambda *a, **k: _CG(
        {c: _flows(30) for c in ("bitcoin", "ethereum", "xrp", "solana")}))
    bf.fetch_etf_overlay()
    assert len(puts) == 1 and puts[0][0] == "etf_multi"


# ─────────────── ⑤ 這一行要真的走到報告上 ───────────────


def test_collect_bottom_inputs_surfaces_the_failure_line(monkeypatch):
    monkeypatch.setattr(bf, "fetch_coinmetrics_btc", lambda p: {})
    monkeypatch.setattr(bf, "fetch_macro_background", lambda: {})
    monkeypatch.setattr(bf, "fetch_stablecoin_momentum_30d", lambda: None)
    monkeypatch.setattr(bf, "fetch_fng_avg30", lambda: None)
    monkeypatch.setattr(bf.httpx, "Client", lambda *a, **k: _CG(
        {c: dict(_401) for c in ("bitcoin", "ethereum", "xrp", "solana")}))
    _inputs, _bg, overlay = bf.collect_bottom_inputs(None, None, None)
    assert "🏦" in overlay, "失敗行沒進 overlay ⇒ 等於還是消失"
    assert "Upgrade plan" in overlay["🏦"]
