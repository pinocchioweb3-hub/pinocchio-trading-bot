# -*- coding: utf-8 -*-
"""v242：情緒源掛掉時，必須說出「哪一個子請求死了、為什麼」。

同物種第 62 次，這次落在**源那一層**（v238 治的是卡片層，源本身仍在說謊）。

`CoinGlassSource.get_sentiment()` 同時打兩個端點（Fear-Greed、AHR999），
兩邊各有五條會讓結果消失的路徑：

    ① 例外（gather return_exceptions=True → 拿到 Exception 物件）
    ② 回了 error 字典（401 Upgrade plan 就是這一條）
    ③ 連上了但 data 是空的
    ④ data_list 空清單
    ⑤ 值解析不出 float

**五條路徑的結果完全一樣**：那個鍵不出現，函式回

    {"source": "coinglass"}

——沒有 error 旗標、沒有成因、也分不出是哪一個端點死的。呼叫端拿到這個字典，
`bool(sentiment)` 是 True、`sentiment.get("error")` 是 None，於是「源整個掛了」
被讀成「源活著，只是沒有讀數」。

線上實測（2026-08-03，CG 停權後）：這個函式此刻回的就是 {"source": "coinglass"}。
稽核腳本因此只能寫「值=None」，講不出是 401 還是空清單——而兩者的處置天差地遠
（前者要續訂或換源，後者要查端點契約）。

⛔ 本輪只治可見性：不加 top-level error 旗標（那會改變 checks.py 的 `_sent_ok`
   分支）、不改任何閾值、不改 label 邏輯。唯一可觸及的行為差異是 AHR999 剛好
   等於 0.0 時（舊碼因 falsy 而寫 None，新碼誠實寫 0.0）——該值在現實中不存在。

改動前的碼會失敗在哪（非虛設檢定的證明）：
  * out 裡沒有 "unavailable" 這個鍵 → KeyError／assert 失敗
  * CoinGlassSource._fetch_failure_reason 不存在 → AttributeError
  * checks.py 的未知列 note 不含成因 → assert 失敗
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from l2_trigger.types import TriggerDecision, TriggerAction, SignalState
from l3_dispatcher.checks import cross_check_fire
from market_intel_mcp.sources.coinglass import CoinGlassSource
from tests import fixtures as F
from tests.fixtures import _replace


# ─────────────── 工具：把 _get 換成腳本化的回應 ───────────────

_FG_PATH = "/api/index/fear-greed-history"
_AH_PATH = "/api/index/ahr999"

# 真的能解析成功的兩份 payload（形狀取自 get_sentiment 的解析碼）
OK_FG = {"data": {"data_list": [30, 31, 32, 33, 34, 35, 36]}}
OK_AH = {"data": [{"ahr999_value": "0.87"}]}


def _sentiment(fg, ah) -> dict:
    """fg／ah 可以是 dict（直接回）或 Exception 實例（拋出）。"""
    src = CoinGlassSource()

    async def fake_get(path, params=None, tool=None, **kw):
        r = fg if path == _FG_PATH else ah
        if isinstance(r, BaseException):
            raise r
        return r

    src._get = fake_get                     # type: ignore[method-assign]
    return asyncio.run(src.get_sentiment())


def _err(msg: str, code: str = "UPSTREAM_ERROR") -> dict:
    return {"error": True, "code": code, "message": msg}


# ─────────────── ① 兩邊都死：各自的成因都要說出來 ───────────────


def test_both_subfetches_fail_reports_each_cause():
    """這就是線上此刻的形狀：兩個端點都 401。"""
    out = _sentiment(_err("Upgrade plan to access", "AUTH_ERROR"),
                     _err("Upgrade plan to access", "AUTH_ERROR"))
    un = out.get("unavailable")
    assert un, "兩個子請求全掛，函式卻沒留下任何成因 → 呼叫端只能猜"
    assert "fear_greed" in un and "ahr999" in un
    assert "Upgrade plan" in un["fear_greed"]
    assert "Upgrade plan" in un["ahr999"]


def test_exception_is_named_not_swallowed():
    """gather(return_exceptions=True) 會把例外當值回來——不可當成「沒資料」。"""
    out = _sentiment(TimeoutError("read timed out"), OK_AH)
    assert "TimeoutError" in out["unavailable"]["fear_greed"]
    assert out.get("ahr999_now") == 0.87, "另一邊活著就該照常給值"


# ─────────────── ② 半死：不可把活著的那半也一起靜音 ───────────────


def test_partial_failure_only_names_the_dead_one():
    out = _sentiment(OK_FG, _err("rate limited", "RATE_LIMIT"))
    assert out.get("fear_greed_now") == 36
    assert set(out["unavailable"]) == {"ahr999"}, \
        "活著的那一半不該出現在 unavailable 裡"


def test_all_ok_has_no_unavailable_key():
    """全部讀到 ⇒ 不留 unavailable 鍵，讓下游的 truthy 判斷維持乾淨。"""
    out = _sentiment(OK_FG, OK_AH)
    assert "unavailable" not in out
    assert out["fear_greed_now"] == 36
    assert out["ahr999_now"] == 0.87


# ─────────────── ③ 空回應 ≠ API 失敗：兩者必須可分辨 ───────────────


def test_empty_payload_is_distinguishable_from_api_error():
    """端點回 200 但沒有資料列——這跟金鑰到期是完全不同的處置。"""
    empty = _sentiment({"data": {"data_list": []}}, {"data": []})
    errored = _sentiment(_err("Upgrade plan to access", "AUTH_ERROR"),
                         _err("Upgrade plan to access", "AUTH_ERROR"))
    assert empty["unavailable"]["fear_greed"] != errored["unavailable"]["fear_greed"], \
        "「回了空清單」與「401」給同一句話 ⇒ 等於沒說"
    assert empty["unavailable"]["ahr999"] != errored["unavailable"]["ahr999"]


def test_unparseable_value_is_reported_not_silently_none():
    """值在、但解析不出 float ⇒ 這是資料契約問題，不是「沒資料」。"""
    out = _sentiment({"data": {"data_list": ["n/a"]}}, {"data": [{"ahr999_value": "n/a"}]})
    assert "fear_greed_now" not in out, "解析失敗不得寫成 None 假裝有這一欄"
    assert out["unavailable"]["fear_greed"]
    assert out["unavailable"]["ahr999"]


def test_every_reason_is_a_nonempty_string():
    """⛔ 成因不得是空字串／None／'unknown'——那只是換一種方式沉默。"""
    for fg, ah in [(_err("boom"), _err("boom")),
                   (RuntimeError("x"), RuntimeError("x")),
                   ({"data": {}}, {"data": None}),
                   ({"data": {"data_list": []}}, {"data": []})]:
        un = _sentiment(fg, ah).get("unavailable") or {}
        assert set(un) == {"fear_greed", "ahr999"}, f"{fg!r} 少報了一邊"
        for k, v in un.items():
            assert isinstance(v, str) and len(v.strip()) >= 4 and v.strip() != "unknown", \
                f"{k} 的成因是空話：{v!r}"


def test_source_key_and_shape_unchanged():
    """回傳仍是 dict、仍帶 source，且⛔不得新增 top-level error 旗標。

    加 error 會翻掉 checks.py 的 `_sent_ok` 分支——那是行為改動，不是可見性。
    """
    out = _sentiment(_err("Upgrade plan"), _err("Upgrade plan"))
    assert out["source"] == "coinglass"
    assert "error" not in out


# ─────────────── ④ 成因要一路傳到卡片上（不然沒人看得到） ───────────────


def _fire_bull(snap) -> TriggerDecision:
    return TriggerDecision(
        action=TriggerAction.FIRE, direction=SignalState.BULL,
        setup_name="test_setup", confirmed=(), composite_score=0.0,
        snapshot=snap, reason="test")


def _sui():
    return _replace(F.sui_intraday_fire_bull(), btc_gate_open=True)


def _row(res, name):
    return next((c for c in res.checks if c["name"] == name), None)


def test_card_shows_the_cause_when_sentiment_is_dead():
    res = asyncio.run(cross_check_fire(
        _fire_bull(_sui()),
        sentiment={"source": "coinglass",
                   "unavailable": {"fear_greed": "AUTH_ERROR: Upgrade plan to access",
                                   "ahr999": "AUTH_ERROR: Upgrade plan to access"}},
        liq_scan={"items": [{"symbol": "SUI", "imbalance": 0.0}]}))
    row = _row(res, "sentiment_check")
    assert row is not None and row.get("unknown") is True
    assert "Upgrade plan" in row["note"], \
        f"卡上只寫「讀不到」，成因在源那層被丟掉了：{row['note']}"


def test_card_still_works_without_the_new_key():
    """向後相容：舊形狀（沒有 unavailable）不得炸，也不得憑空生出成因。"""
    res = asyncio.run(cross_check_fire(
        _fire_bull(_sui()),
        sentiment={"source": "coinglass"},
        liq_scan={"items": [{"symbol": "SUI", "imbalance": 0.0}]}))
    row = _row(res, "sentiment_check")
    assert row is not None and row.get("unknown") is True
    assert row.get("delta", 0) == 0


def test_card_scoring_untouched_by_the_cause_string():
    """⛔ 可見性改動不得動到分數：未知列仍 delta=0、pass=True。"""
    res = asyncio.run(cross_check_fire(
        _fire_bull(_sui()),
        sentiment={"source": "coinglass",
                   "unavailable": {"fear_greed": "x: y", "ahr999": "x: y"}},
        liq_scan={"items": [{"symbol": "SUI", "imbalance": 0.0}]}))
    row = _row(res, "sentiment_check")
    assert row["delta"] == 0 and row["pass"] is True
