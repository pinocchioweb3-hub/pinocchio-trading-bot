# -*- coding: utf-8 -*-
"""v244：影子觀測「一輪抓不到任何東西」不再折成「這一輪本來就沒東西」。

同物種第 65 次。落點：`l3_dispatcher/cvd_shadow.py`——**觀測層自己**。

量到的事實（2026-08-03 讀 `cvd_shadow.jsonl` 664 列）：
    最後一筆 captured>0 ： 2026-07-08T13:42Z（captured=20）
    第一筆 captured==0  ： 2026-07-08T14:46Z
    之後                ： 574 筆連續空輪，跨 26 天，每小時一筆
7/08 正是 CoinGlass 方案到期日。也就是說這個 worker 已經空轉 26 天，
而它每小時印出來的那一行是：

    [cvd_shadow] universe_n=20 captured=0 binance_ok=0 backfill_ok=0 span_sec=None elapsed_sec=190.4

這一行沒有一個字說得出「為什麼」。三個洞：

  ① `captured=0` 同時代表「宇宙裡沒有幣」與「源死了」。我自己在多輪稽核裡
     看過這行好幾次都沒讀成故障——它長得就像一個安靜的正常輪。
  ② `binance_ok=0` **根本不是量測結果**：syms 空 ⇒ 一次 Binance 都沒呼叫。
     0/0 被寫成跟 0/20 一模一樣，等於用「全部失敗」冒充「從沒問過」。
     （同物種更深一層：連「我沒去量」都被折成「我量到是零」。）
  ③ `_universe_factors` 把每一種失敗都收斂成 `misses.append(sym)`：
     401 / 429 / 連線例外 / 回應成功但 items 空 —— 四種處置完全不同的成因，
     在輸出上不留任何痕跡。

⛔ 不加熔斷（「連續 N 輪失敗就別再試了」）：源恢復要靠「有成功呼叫」才判得出來
   （v151 原則）。停止呼叫＝永遠不會知道它活了。每小時 20 次對死金鑰的呼叫是
   偵測恢復的代價，該付。
⛔ 反向側：一輪全成功時不得多出任何雜訊鍵（否則 sink 膨脹、⚠️ 貶值）。
⛔ 影子鐵則不動：binance_* 仍從不回寫 factors_live，本檔不碰那條線。
⛔ 成因不得捏造：講不出來就明說講不出來，不得填一個像樣的猜測。

改動前的碼會失敗在哪（非虛設檢定的證明）：
  * summary 裡根本沒有 `unavailable` / `binance_attempted` 這些鍵 → KeyError／None
  * `_universe_factors` 不收成因 → reasons 永遠是空的
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from l3_dispatcher import cvd_shadow as cs


# ─────────────────────────── 假源 ───────────────────────────


class _Src:
    """依 symbol 回不同結果：dict=直接回、Exception 實例=拋出。"""

    def __init__(self, by_sym: dict, default=None):
        self._by = by_sym
        self._default = default

    async def get_strength_universe(self, limit=1, candidate_symbols=None):
        sym = (candidate_symbols or ["?"])[0]
        r = self._by.get(sym, self._default)
        if isinstance(r, BaseException):
            raise r
        if r is None:
            return {"items": []}
        return r


def _reasons_for(src, n=4):
    reasons: dict = {}
    items = asyncio.run(cs._universe_factors(src, n, pace=0, retries=0,
                                             reasons=reasons))
    return items, reasons


# ─────────── ① 每一種失敗都要留下可分辨的成因 ───────────


def test_api_error_reason_is_captured():
    src = _Src({}, default={"error": True, "code": "401", "message": "Upgrade plan"})
    items, reasons = _reasons_for(src)
    assert items == []
    assert reasons, "全滅卻沒有任何成因 ⇒ 空轉 26 天都不會有人知道為什麼"
    assert any("401" in r or "Upgrade plan" in r for r in reasons.values()), reasons


def test_exception_reason_names_the_type():
    src = _Src({}, default=TimeoutError("boom"))
    _items, reasons = _reasons_for(src)
    assert any("TimeoutError" in r for r in reasons.values()), reasons


def test_empty_items_is_not_the_same_reason_as_api_error():
    """「回應成功但 items 空」要查資料契約，「401」要續訂——⛔ 不可同一句話。"""
    _i1, empty = _reasons_for(_Src({}, default={"items": []}))
    _i2, err = _reasons_for(_Src({}, default={"error": True, "code": "401"}))
    assert set(empty.values()) != set(err.values()), \
        f"兩種完全不同的處置給了同一句話：{empty} vs {err}"
    assert all(r.strip() for r in empty.values())


def test_no_source_gives_a_reason_too():
    """daemon 沒給 source ⇒ 這也是一種「量不到」，不得靜默回 []。"""
    reasons: dict = {}
    items = asyncio.run(cs._universe_factors(None, 5, pace=0, reasons=reasons))
    assert items == []
    assert reasons, "source 是 None 也要留痕，否則接線斷了看起來像宇宙空的"


def test_success_leaves_no_reason():
    """反向側：抓到的幣不得出現在成因表裡。"""
    src = _Src({}, default={"items": [{"symbol": "X", "cvd_slope_7d": 0.0}]})
    items, reasons = _reasons_for(src)
    assert len(items) == 4 and reasons == {}


# ─────────── ② summary：0/0 不得寫成跟 0/20 一樣 ───────────


def _cycle(src, n=4):
    return asyncio.run(cs._run_cycle(source=src, n=n, pace=0, retries=0))


def test_binance_zero_without_attempt_is_distinguishable(monkeypatch):
    """syms 空 ⇒ 一次都沒呼叫 Binance。`binance_ok=0` 不是量測結果，要說得出來。"""
    _stub_binance(monkeypatch, per_symbol_forbidden=True)
    s = _cycle(_Src({}, default={"error": True, "code": "401", "message": "Upgrade plan"}))
    assert s["captured"] == 0
    assert s.get("binance_attempted") == 0, \
        "0/0 與 0/20 在輸出上長得一樣＝用『全部失敗』冒充『從沒問過』"
    assert s.get("unavailable"), "空輪必須帶成因"
    assert "Upgrade plan" in repr(s["unavailable"])


def test_summary_reason_survives_into_the_sink(monkeypatch, tmp_path):
    """成因要真的寫進 JSONL——不然離線閘讀到的還是一堆無成因的空列。"""
    import json
    sink = tmp_path / "cvd_shadow.jsonl"
    monkeypatch.setattr(cs, "_sink_path", lambda: sink)
    _stub_binance(monkeypatch, per_symbol_forbidden=True)
    s = _cycle(_Src({}, default={"error": True, "code": "401", "message": "Upgrade plan"}))
    cs._append_jsonl(s)
    row = json.loads(sink.read_text(encoding="utf-8").strip())
    assert "Upgrade plan" in repr(row.get("unavailable"))
    assert row.get("binance_attempted") == 0


def test_full_success_adds_no_noise_keys(monkeypatch):
    """反向側守門：全成功時 ⛔ 不得多出 unavailable（sink 每小時一列，膨脹要擋）。"""
    monkeypatch.setattr(cs, "_binance_daily", _fake_daily)
    monkeypatch.setattr(cs, "_binance_cvd", _fake_cvd)
    monkeypatch.setattr(cs, "_binance_positioning", _fake_pos)
    src = _Src({}, default={"items": [{"symbol": "X", "cvd_slope_7d": 0.0}]})
    s = _cycle(src)
    assert s["captured"] == 4
    assert "unavailable" not in s, f"全成功卻多了雜訊鍵：{s.get('unavailable')}"
    assert s.get("binance_attempted") == 4


def test_reasons_are_deduped_not_one_per_symbol(monkeypatch):
    """20 幣同一個成因時，⛔ 不得在每列 sink 裡重複 20 次同一句話。"""
    _stub_binance(monkeypatch, per_symbol_forbidden=True)
    s = _cycle(_Src({}, default={"error": True, "code": "401", "message": "Upgrade plan"}), n=4)
    unavail = s["unavailable"]
    assert isinstance(unavail, dict)
    assert len(unavail) == 1, f"同一句成因應合併成一組：{unavail}"
    syms = list(unavail.values())[0]
    assert isinstance(syms, list) and len(syms) == 4


# ─────────── ③ 成因不得是空話 ───────────


@pytest.mark.parametrize("payload", [
    {"error": True, "code": "401", "message": "Upgrade plan"},
    {"items": []},
    {"nonsense": 1},
    TimeoutError("x"),
    OSError("y"),
])
def test_no_reason_is_a_placeholder(payload):
    _items, reasons = _reasons_for(_Src({}, default=payload))
    for r in reasons.values():
        assert r and r.strip(), f"空成因：{r!r}"
        assert r.strip().lower() not in ("unknown", "error", "none", "n/a", "?"), r
        assert len(r.strip()) >= 4, f"成因太短講不出東西：{r!r}"


# ─────────────────────────── 輔助 ───────────────────────────


def _stub_binance(monkeypatch, per_symbol_forbidden=False):
    """擋掉真網路。

    ⚠️ `_binance_daily(bn,"BTC")` 是**無條件**呼叫的（BTC 參考序列，by-design），
    即使 syms 空也會發生——所以它不能當「不該呼叫」的哨兵。真正該守的是
    **逐幣**呼叫（_binance_cvd / _binance_positioning）在 syms 空時一次都不該發生。
    """
    monkeypatch.setattr(cs, "_binance_daily", _fake_daily)
    if per_symbol_forbidden:
        monkeypatch.setattr(cs, "_binance_cvd", _never_called)
        monkeypatch.setattr(cs, "_binance_positioning", _never_called)


async def _never_called(*a, **k):
    raise AssertionError("syms 是空的，不該有任何逐幣 Binance 呼叫")


async def _fake_daily(bn, symbol, limit=35):
    return [100.0 + i for i in range(30)], [1e6] * 30


async def _fake_cvd(bn, symbol):
    return {"cvd": 1.0, "cvd_slope": 2.0, "cvd_slope_7d": 3.0, "series": [1] * 168}


async def _fake_pos(bn, symbol):
    return 1.5
