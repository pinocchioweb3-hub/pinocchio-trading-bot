# -*- coding: utf-8 -*-
"""v212：熊底資料層「窗口殘缺」不得折成「這就是 N 日的值」。

同物種（未知／不完整 → 被折成一個看起來完整的事實）第 32 次。落點：
  1) fetch_fng_avg30：只抓到 5 天也照算平均、照樣以 `fng_avg30` 進 compute_bottom_score 計分。
  2) fetch_etf_overlay：只有 7 天資料也印「30日 +X M」給人看（紅線③相鄰：對外呈現的數字）。

本檔同時守反向側：窗口完整時必須照舊算得出來（避免修補變成「一律不敢算」）。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from l3_dispatcher import bottom_feeds as bf


class _Resp:
    def __init__(self, payload):
        self._p = payload

    def json(self):
        return self._p


class _FakeCGClient:
    """假的 CoinGlass client（context manager），依 coin 回不同長度的 flow-history。"""

    def __init__(self, by_coin: dict):
        self._by_coin = by_coin

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, path):
        coin = path.split("/")[3]
        return _Resp({"data": self._by_coin.get(coin, [])})


@pytest.fixture(autouse=True)
def _no_cache(monkeypatch):
    """繞開每日快取，讓每個案例都真的走抓取路徑。"""
    monkeypatch.setattr(bf, "_cached", lambda key: None)
    monkeypatch.setattr(bf, "_put", lambda key, data: None)


def _fng_payload(n: int) -> dict:
    return {"data": [{"value": str(20 + (i % 3))} for i in range(n)]}


# ── fng_avg30：進計分的那個數字 ────────────────────────────────────────────


def test_fng_partial_window_is_not_folded_into_avg30(monkeypatch):
    """只回 5 天 → 不可回一個「30 日均」；缺料就是 None（compute_bottom_score 有 present_mass 誠實處理）。"""
    monkeypatch.setattr(bf.httpx, "get", lambda *a, **k: _Resp(_fng_payload(5)))
    assert bf.fetch_fng_avg30() is None


def test_fng_29_days_still_not_avg30(monkeypatch):
    """差一天也算殘缺——邊界不可放水（29 天平均冒充 30 天均一樣是造假）。"""
    monkeypatch.setattr(bf.httpx, "get", lambda *a, **k: _Resp(_fng_payload(29)))
    assert bf.fetch_fng_avg30() is None


def test_fng_missing_data_key_is_none(monkeypatch):
    """回應裡沒有 data（限流／錯誤 body）＝未知，不可折成「沒有任何值」再算成平均。"""
    monkeypatch.setattr(bf.httpx, "get", lambda *a, **k: _Resp({"metadata": {"error": "rate limited"}}))
    assert bf.fetch_fng_avg30() is None


def test_fng_full_window_still_computes(monkeypatch):
    """反向側守門：完整窗口（API 給 45 天）必須照舊算出前 30 天均，值要對。"""
    payload = _fng_payload(45)
    monkeypatch.setattr(bf.httpx, "get", lambda *a, **k: _Resp(payload))
    expect = round(sum(int(x["value"]) for x in payload["data"][:30]) / 30, 1)
    assert bf.fetch_fng_avg30() == expect


# ── ETF overlay：印給人看的那個數字 ───────────────────────────────────────


def _flows(n: int, per_day: float = 10e6) -> list:
    return [{"flow_usd": per_day} for _ in range(n)]


def test_etf_partial_window_must_not_print_a_bare_30d_figure(monkeypatch):
    """只有 7 天資料 → 不可印出看似完整的「30日 +70M」；必須標明資料不足。"""
    monkeypatch.setenv("COINGLASS_API_KEY", "dummy")
    monkeypatch.setattr(bf.httpx, "Client",
                        lambda *a, **k: _FakeCGClient({"bitcoin": _flows(7)}))
    s = bf.fetch_etf_overlay()
    assert s is not None and "BTC" in s
    assert "5日" in s, "5 日窗口是完整的，仍應照報"
    assert "30日+70M" not in s.replace(",", "") and "30日資料不足" in s, \
        f"殘缺窗口被折成完整的 30 日數字：{s}"


def test_etf_full_window_reports_both(monkeypatch):
    """反向側守門：滿 30 天必須照舊同時報 5 日與 30 日。"""
    monkeypatch.setenv("COINGLASS_API_KEY", "dummy")
    monkeypatch.setattr(bf.httpx, "Client",
                        lambda *a, **k: _FakeCGClient({"ethereum": _flows(30)}))
    s = bf.fetch_etf_overlay()
    assert s is not None and "ETH" in s and "5日" in s and "30日" in s
    assert "資料不足" not in s


def test_etf_too_short_window_is_skipped(monkeypatch):
    """連 5 日窗口都湊不齊（3 天）→ 該資產整個不報，不可拿 3 天冒充 5 日。"""
    monkeypatch.setenv("COINGLASS_API_KEY", "dummy")
    monkeypatch.setattr(bf.httpx, "Client",
                        lambda *a, **k: _FakeCGClient({"bitcoin": _flows(3)}))
    assert bf.fetch_etf_overlay() is None


def test_etf_data_key_missing_is_not_zero_flow(monkeypatch):
    """data 讀不出來＝未知，不可折成「淨流為零」——該資產直接不報。"""
    monkeypatch.setenv("COINGLASS_API_KEY", "dummy")

    class _NoData(_FakeCGClient):
        def get(self, path):
            return _Resp({"code": "50011", "msg": "rate limit"})

    monkeypatch.setattr(bf.httpx, "Client", lambda *a, **k: _NoData({}))
    assert bf.fetch_etf_overlay() is None
