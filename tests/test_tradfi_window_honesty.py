"""傳統金融跨資產：窗口不足時不得折成「這就是 7d／30d 的值」（v215）。

同物種第 35 次（v208/v210/v211/v212/v213/v214 同形）：抓回來的收盤價序列
比宣稱的窗口短時，舊碼用 `closes[0]` 靜默頂替 → 一個「5 天的變化」被貼上
「30d」的標籤送進使用者看的宏觀卡。分離「未知」與「答案」：窗口湊不齊 →
該欄回 None（誠實缺料），顯示層印 n/a。

⛔ 反向側守門：窗口湊得齊時，輸出必須與舊碼逐值相同（不許退化成一律不敢算）。
"""
from __future__ import annotations

import asyncio
import json

import pytest

from market_intel_mcp.sources.tradfi import TradFiSource


class _FakeResp:
    def __init__(self, payload: dict, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload)

    def json(self) -> dict:
        return self._payload


class _FakeClient:
    """只回一組固定 closes，不出網。"""

    def __init__(self, closes: list):
        self._closes = closes

    async def get(self, path: str, params: dict | None = None):
        return _FakeResp({
            "chart": {
                "error": None,
                "result": [{
                    "meta": {"currency": "USD"},
                    "timestamp": list(range(len(self._closes))),
                    "indicators": {"quote": [{"close": list(self._closes)}]},
                }],
            }
        })


def _ticker_with(closes: list) -> dict:
    src = TradFiSource()
    src._client = _FakeClient(closes)          # 繞過真實 httpx
    return asyncio.run(src.get_ticker("DX-Y.NYB"))


# --------------------------------------------------------------------------
# 正向側：窗口不足 → 誠實缺料（None），不得是一個看起來確定的數字
# --------------------------------------------------------------------------

def test_only_five_closes_must_not_report_a_30d_change():
    """5 根收盤價回不出「30 日變化」——舊碼會把 5 天的變化貼上 30d 標籤。"""
    r = _ticker_with([100.0, 101.0, 102.0, 103.0, 110.0])
    assert not r.get("error"), r
    assert r["change_30d_pct"] is None, (
        f"窗口只有 5 根卻報出 30d={r['change_30d_pct']}％＝把部分窗口折成確定的值"
    )


def test_only_five_closes_must_not_report_a_7d_change():
    r = _ticker_with([100.0, 101.0, 102.0, 103.0, 110.0])
    assert r["change_7d_pct"] is None, (
        f"窗口只有 5 根卻報出 7d={r['change_7d_pct']}％"
    )


def test_window_shortfall_is_visible_in_provenance():
    """留痕：下游要能看出這組數字是用幾根收盤價算的。"""
    r = _ticker_with([100.0, 101.0, 102.0, 103.0, 110.0])
    assert r.get("closes_n") == 5


def test_eight_closes_gives_7d_but_still_no_30d():
    """部分可讀就用可讀的算：湊得齊 7d 就要照算，30d 仍誠實缺料。"""
    closes = [100.0, 101.0, 102.0, 103.0, 104.0, 105.0, 106.0, 110.0]
    r = _ticker_with(closes)
    assert r["change_7d_pct"] is not None
    assert r["change_30d_pct"] is None


# --------------------------------------------------------------------------
# 反向側守門：窗口湊得齊時逐值與舊碼相同
# --------------------------------------------------------------------------

def test_full_window_values_unchanged():
    closes = [100.0 + i for i in range(60)]
    r = _ticker_with(closes)
    cur, p1, p7, p30 = closes[-1], closes[-2], closes[-7], closes[-30]
    assert r["change_1d_pct"] == round((cur - p1) / p1 * 100, 2)
    assert r["change_7d_pct"] == round((cur - p7) / p7 * 100, 2)
    assert r["change_30d_pct"] == round((cur - p30) / p30 * 100, 2)
    assert r["current"] == round(cur, 4)
    assert r["high_3mo"] == round(max(closes), 4)
    assert r["low_3mo"] == round(min(closes), 4)
    assert r["closes_n"] == 60


def test_exactly_30_closes_is_an_answer_not_unknown():
    """邊界：剛好湊滿即是答案，不可為保險打成未知。"""
    closes = [100.0 + i for i in range(30)]
    r = _ticker_with(closes)
    assert r["change_30d_pct"] == round(
        (closes[-1] - closes[-30]) / closes[-30] * 100, 2)


def test_insufficient_data_still_errors():
    """少於 2 根仍走既有 error 路徑（不因本次改動而變成半殘 dict）。"""
    r = _ticker_with([100.0])
    assert r.get("error")


# --------------------------------------------------------------------------
# 顯示層：None 不可讓宏觀卡整張炸掉（舊格式字串 f"{None:+.2f}" 會 TypeError）
# --------------------------------------------------------------------------

def test_synthesizer_renders_na_instead_of_crashing():
    """舊碼 f"{None:+.2f}" 會 TypeError＝整張宏觀卡炸掉，不只是這一欄壞。"""
    from l3_dispatcher import synthesizer

    tradfi = {"items": {"DX-Y.NYB": {
        "name": "美元指數 DXY", "current": 99.1,
        "change_1d_pct": 0.12, "change_7d_pct": None, "change_30d_pct": None,
    }}}
    # 其餘區塊一律標 error＝該區跳過，讓本測試只隔離 tradfi 那一行
    state = {k: {"error": True} for k in (
        "options_btc", "options_eth", "liquidations", "whales",
        "pattern_btc", "pattern_eth", "pattern_sol",
    )}
    text = synthesizer._format_data_for_prompt(state, tradfi)
    line = next(ln for ln in text.splitlines() if ln.startswith("- DX-Y.NYB"))
    assert "1d +0.12%" in line
    assert "7d n/a" in line and "30d n/a" in line
    assert "None" not in line
