# -*- coding: utf-8 -*-
"""v213：山寨 Top20 深度卡「這輪讀不出來／窗口不足」不得折成一個看起來確定的數字。

同物種（未知／不完整 → 被折成一個看起來完整的事實）第 33 次。落點
l3_dispatcher/alt20_watch.py，三處同形，全部落在**印給人看且被計分**的數字上
（紅線③相鄰：使用者會照這張卡親手買現貨）：

  1) `_okx_pub` 任何失敗都回 `[]`——與交易所明講「沒有」同一個出口。
  2) 日 K 只回 40 根也照算 `ma200`，卻以「Mayer」（定義＝200 日均）與
     「距ATH」（定義＝歷史最高）兩個滿窗標籤印出去。
  3) 資費／OI 這輪讀不出來 → 合流分那一顆星無聲記 0，與「資費為正＝確實不加分」
     得到完全相同的 ★☆ 顯示。

本檔同時守反向側：滿窗且無缺料時，分數／價值帶／星等必須與舊碼逐字相同
（避免修補退化成「一律不敢算」）。
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from l3_dispatcher import alt20_watch as aw


class _Resp:
    def __init__(self, payload, status_code=200):
        self._p = payload
        self.status_code = status_code

    def json(self):
        if isinstance(self._p, Exception):
            raise self._p
        return self._p


class _FakeClient:
    """依 path 關鍵字回不同 payload；值可以是 _Resp 或 Exception（＝傳輸層炸掉）。"""

    def __init__(self, candles=None, funding=None, oi=None):
        self._by_kind = {"candles": candles, "funding-rate": funding,
                         "open-interest": oi}

    async def get(self, url):
        for kind, resp in self._by_kind.items():
            if kind in url:
                if isinstance(resp, Exception):
                    raise resp
                return resp
        return _Resp({"code": "0", "data": []})


def _candles(n: int, base: float = 100.0):
    """回 n 根日 K（OKX 格式，最新在前）：[ts, o, h, l, c, vol, ...]"""
    rows = []
    for i in range(n):
        c = base + i * 0.1
        rows.append([str(1_700_000_000 + i), str(c), str(c * 1.02),
                     str(c * 0.98), str(c), "1000", "1000", "1000"])
    return list(reversed(rows))       # 最新在前


def _ok(data):
    return _Resp({"code": "0", "data": data})


def _read(client, sym="ETH"):
    return asyncio.run(aw.read_symbol(client, sym))


# ── 1. _okx_pub：未知 vs 確認沒有，必須是兩個不同的答案 ──────────────────


def test_okx_pub_transport_error_is_unknown_not_empty():
    """連線炸掉 ≠ 交易所說沒有。回 None＝未知。"""
    got = asyncio.run(aw._okx_pub(_FakeClient(candles=RuntimeError("boom")),
                                  "/api/v5/market/candles?x=1"))
    assert got is None


def test_okx_pub_api_error_code_is_unknown():
    """code!=0（限流／參數錯）不可折成空清單。"""
    client = _FakeClient(candles=_Resp({"code": "50011", "msg": "Too Many Requests",
                                        "data": []}))
    got = asyncio.run(aw._okx_pub(client, "/api/v5/market/candles?x=1"))
    assert got is None


def test_okx_pub_http_500_is_unknown():
    client = _FakeClient(candles=_Resp({"data": []}, status_code=500))
    got = asyncio.run(aw._okx_pub(client, "/api/v5/market/candles?x=1"))
    assert got is None


def test_okx_pub_confirmed_empty_stays_empty():
    """⛔ 邊界線：{"code":"0","data":[]} 是交易所明講「沒有」＝確定，
    不可為保險一起打成未知（否則正常空回應每輪都變告警＝慢性假警報）。"""
    got = asyncio.run(aw._okx_pub(_FakeClient(candles=_ok([])),
                                  "/api/v5/market/candles?x=1"))
    assert got == []


# ── 2. 殘缺窗口不得掛滿窗標籤（Mayer=200日均／ATH=歷史最高）──────────────


def test_truncated_window_row_does_not_claim_mayer():
    """只有 40 根日 K：算得出來的是「40 日均」，不是 Mayer（200 日均）。"""
    r = _read(_FakeClient(candles=_ok(_candles(40)), funding=_ok([]), oi=_ok([])))
    assert r is not None
    row = aw.render_row(r)
    assert "僅40日" in row, f"殘缺窗口未在人看得到的地方講明，實得：{row}"
    assert r["mayer"] is None, "不足 200 根卻仍給出一個 Mayer 數字"


def test_truncated_window_zone_is_not_a_value_verdict():
    """40 日均算出來的「深度價值帶」是誤報：價值帶的定義本身就掛在 200 日均上。"""
    r = _read(_FakeClient(candles=_ok(_candles(40)), funding=_ok([]), oi=_ok([])))
    assert r["zone"] == "資料不足", f"殘缺窗口被判成價值帶：{r['zone']}"


def test_high_label_never_claims_all_time_high():
    """limit=300 ⇒ 拿到的至多是 300 日高。印「距ATH」是把「我只看到 300 天」
    折成「這就是歷史最高」——滿窗時也一樣是誤述。"""
    r = _read(_FakeClient(candles=_ok(_candles(300)), funding=_ok([]), oi=_ok([])))
    row = aw.render_row(r)
    assert "距ATH" not in row, f"仍宣稱 ATH（歷史最高），實得：{row}"
    assert "距300日高" in row, f"未講明是幾日高，實得：{row}"


def test_candles_unreadable_still_returns_none():
    """反向側：日K 這輪整個讀不出來 → 照舊回 None（會被日報的 N/20 計數看見）。"""
    assert _read(_FakeClient(candles=RuntimeError("boom"))) is None


def test_too_few_candles_still_returns_none():
    """反向側：< 30 根照舊回 None（行為不變）。"""
    r = _read(_FakeClient(candles=_ok(_candles(10)), funding=_ok([]), oi=_ok([])))
    assert r is None


# ── 3. 資費／OI 讀不出來 ≠ 這顆星不成立 ────────────────────────────────


def test_funding_unreadable_is_not_a_confirmed_zero_star():
    """資費這輪讀不出來，與「資費為正＝確實不加分」不可得到同一個 ★☆ 顯示。"""
    r = _read(_FakeClient(candles=_ok(_candles(250)),
                          funding=RuntimeError("boom"), oi=_ok([])))
    row = aw.render_row(r)
    assert "?" in row, f"合流分未標示為下限（與確定的 0 星同形），實得：{row}"
    assert "funding" in r.get("data_gaps", []), "資費讀取失敗未留痕"


def test_oi_unreadable_is_not_a_confirmed_zero_star():
    r = _read(_FakeClient(candles=_ok(_candles(250)), funding=_ok([]),
                          oi=_Resp({"data": []}, status_code=500)))
    row = aw.render_row(r)
    assert "?" in row, f"合流分未標示為下限，實得：{row}"
    assert "oi" in r.get("data_gaps", []), "OI 讀取失敗未留痕"


def test_confirmed_positive_funding_is_not_a_gap():
    """⛔ 反向側邊界：交易所確實回了一個正資費 ⇒ 那顆星就是確定不成立，
    不可標成未確認（否則每輪正常卡都掛問號＝慢性假警報）。"""
    r = _read(_FakeClient(candles=_ok(_candles(250)),
                          funding=_ok([{"fundingRate": "0.0001"}]),
                          oi=_ok([["t", "100"], ["t", "90"]])))
    assert "?" not in aw.render_row(r)
    assert r.get("data_gaps", []) == [], f"確定的答案被誤標成缺料：{r.get('data_gaps')}"


def test_confirmed_empty_funding_is_not_a_gap():
    """⛔ 邊界線續：{"code":"0","data":[]}＝交易所確認這檔沒有資費資料，屬確定。"""
    r = _read(_FakeClient(candles=_ok(_candles(250)), funding=_ok([]), oi=_ok([])))
    assert "?" not in aw.render_row(r)
    assert r.get("data_gaps", []) == [], f"確認沒有被誤標成未知：{r.get('data_gaps')}"


# ── 4. 反向側總守門：滿窗且無缺料時，分數與顯示必須與舊碼相同 ─────────


def test_full_window_no_gap_scores_exactly_as_before():
    """250 根日K（價在 200 日均之上／資費為正／OI 下滑）⇒ 四項全不成立＝0 星，
    且全部是**確定**的 0，不掛任何問號。"""
    r = _read(_FakeClient(candles=_ok(_candles(250)),
                          funding=_ok([{"fundingRate": "0.0005"}]),
                          oi=_ok([["t", "100"], ["t", "90"]])))   # 末筆＝最新 ⇒ OI 下滑
    assert r["score"] == 0
    assert r["zone"] in ("深度價值", "價值", "中性", "過熱")
    row = aw.render_row(r)
    assert "★" not in row and "?" not in row
    assert "☆☆☆☆" in row
    assert r.get("unresolved", 0) == 0


def test_full_window_value_zone_still_computed():
    """反向側：價跌破 200 日均時，價值帶與星等照舊算得出來（不因修補而不敢判）。"""
    rows = _candles(250)                      # 最新在前；_candles 是遞增序列
    latest = list(rows[0])
    latest[4] = "50.0"                        # 收盤砸到 200 日均之下
    latest[2], latest[3] = "51.0", "49.0"
    rows[0] = latest
    r = _read(_FakeClient(candles=_ok(rows),
                          funding=_ok([{"fundingRate": "-0.0002"}]),
                          oi=_ok([["t", "100"], ["t", "110"]])))   # 末筆＝最新 ⇒ OI 回升
    assert r["mayer"] is not None and r["mayer"] < 1
    assert r["zone"] in ("深度價值", "價值")
    assert r["score"] == 4, f"滿窗全命中卻不是 4 星：{r['score']}"
    assert r.get("unresolved", 0) == 0


# ── 5. 日報表頭要把「這張卡有多少不確定」講出來 ────────────────────────


def test_digest_gap_summary_speaks_up():
    reads = [
        {"data_gaps": ["candles_short"], "unresolved": 2},
        {"data_gaps": ["funding"], "unresolved": 1},
        {"data_gaps": [], "unresolved": 0},
    ]
    line = aw.summarize_data_gaps(reads)
    assert line and "1" in line
    assert "下限" in line or "不足" in line


def test_digest_gap_summary_silent_when_clean():
    """反向側：全部滿窗無缺料時不得多印一行雜訊。"""
    assert aw.summarize_data_gaps([{"data_gaps": [], "unresolved": 0}]) is None
