# -*- coding: utf-8 -*-
"""v241：同一份快照，「哪些欄位我不知道」只能有一個答案。

承 v239／v240。做完引擎輸入的全面稽核（23/40 讀得到）之後，量到這件事：

    引擎問的是   snap.is_stale(f)      → 值是 None **或** 列在 stale_fields
    報表問的是   f in snap.stale_fields → 只有「來源回報失敗」那一種

同一份線上快照，前者答 11 個未知，後者答 3 個。差的 8 個是「源根本沒給這一欄」
——值是 None、但沒進 stale_fields。引擎對它們是安全的（is_stale 涵蓋 None，
STALE→不計票），但每一個拿 stale_fields 生報表的地方都少數 8 欄。

這就是同物種第 61 次：**「我不知道」有兩種寫法，其中一種在報表層被讀成「正常」。**

具體落點（scheduler.py，v239 我自己寫的那幾行）：
  * stale_count / core_stale_count  用 len(snap.stale_fields)  → 系統性低報
  * btc_gate_stale 用 "btc_gate_open" in snap.stale_fields
      → 若閘的值是 None 而沒被標 stale，引擎會 HOLD 在 btc_gate_stale（失明），
        活動檔卻記 btc_gate_stale=False（＝「閘是量出來的、確實關著」）。
        v239 的乾旱分類器會因此把**失明**講成**閘控**：fault False 而非 True、
        warn 而非 alert、24h 節流而非 6h。
      （今天還走不到：get_btc_gate 每條失敗路徑都回 error 字典，所以會被標 stale。
        但這是靠源那一端的自律撐著，不是判據本身成立。）
  * 同一組三行在 140/141/144 與 216/217 各寫一次——會走岔的形狀。

改動前的碼會失敗在哪（非虛設檢定的證明）：
  * MarketSnapshot.unknown_fields 不存在 → AttributeError
  * scheduler._snap_quality 不存在 → ImportError／AttributeError
"""
from __future__ import annotations

from dataclasses import fields as dc_fields

import pytest

from l2_trigger.types import MarketSnapshot

_META = {"symbol", "tf", "stale_fields", "sources_used"}


def _snap(**kw) -> MarketSnapshot:
    base = {"symbol": "BTC", "tf": "1h", "price": 60000.0, "ts": 1_785_000_000_000}
    base.update(kw)
    return MarketSnapshot(**base)


# ─────────────── ① 兩個判據必須合一 ───────────────

def test_unknown_fields_agrees_with_is_stale_for_every_field():
    """對每一個欄位，unknown_fields() 的成員資格必須等同 is_stale()。

    這是整條修正的不變式。任何一欄兩邊不一致，就是又生出第二個「未知」答案。
    """
    snap = _snap(oi=1.0, funding=None, cvd=None,
                 stale_fields=("liq_long", "liq_short"))
    unknown = set(snap.unknown_fields())
    for f in dc_fields(snap):
        if f.name in _META:
            continue
        assert (f.name in unknown) is snap.is_stale(f.name), \
            f"{f.name}：unknown_fields 與 is_stale 給出不同答案"


def test_none_value_not_in_stale_fields_is_still_unknown():
    """值是 None、沒列在 stale_fields ⇒ 仍然是「我不知道」。

    ⛔ 這一格不准折成「正常」。線上那 8 個衍生欄全長這樣。
    """
    snap = _snap(vol_24h_vs_30d=None, oi_delta_7d_pct=None, stale_fields=())
    unknown = set(snap.unknown_fields())
    assert "vol_24h_vs_30d" in unknown
    assert "oi_delta_7d_pct" in unknown


def test_stale_fields_alone_under_reports():
    """明確釘住「兩個答案」這件事：stale_fields 的數量會少於真實未知數。"""
    snap = _snap(cvd=None, vol_24h_vs_30d=None, higher_lows_7d=None,
                 stale_fields=("liq_long",))
    assert len(snap.stale_fields) == 1
    assert len(snap.unknown_fields()) > len(snap.stale_fields), \
        "若這兩個數字相等，代表 unknown_fields 只是 stale_fields 的別名"


def test_stale_field_with_a_value_is_still_unknown():
    """反向：源回報失敗、但欄位剛好留著舊值 ⇒ 仍然算未知，不可因為有值就放行。"""
    snap = _snap(funding=0.0001, stale_fields=("funding",))
    assert "funding" in set(snap.unknown_fields())


def test_metadata_fields_are_not_reported_as_unknown():
    """symbol／tf／stale_fields／sources_used 是元資料，不是量測值。"""
    unknown = set(_snap().unknown_fields())
    assert not (unknown & _META)


def test_non_optional_defaults_are_not_unknown():
    """cvd_price_divergence='none'、is_hot=False 是真值不是缺料。"""
    unknown = set(_snap().unknown_fields())
    assert "cvd_price_divergence" not in unknown
    assert "is_hot" not in unknown
    assert "us_breakout_dir" not in unknown


# ─────────────── ② scheduler 的品質欄位改用同一個判據 ───────────────

def test_snap_quality_btc_gate_stale_catches_none_gate():
    """閘的值是 None、沒標 stale ⇒ btc_gate_stale 必須是 True。

    舊碼用 "btc_gate_open" in stale_fields，這格會回 False，
    於是 v239 的乾旱分類器把失明講成閘控。
    """
    from l3_dispatcher.scheduler import _snap_quality
    q = _snap_quality(_snap(btc_gate_open=None, stale_fields=()))
    assert q["btc_gate_stale"] is True


def test_snap_quality_btc_gate_measured_false_is_not_stale():
    """量到了、確實關著 ⇒ 不是 stale。⛔ 不可為了保守把兩者併成一個。"""
    from l3_dispatcher.scheduler import _snap_quality
    q = _snap_quality(_snap(btc_gate_open=False, btc_regime="risk_off",
                            stale_fields=()))
    assert q["btc_gate_stale"] is False


def test_snap_quality_core_count_unchanged_by_derived_nones():
    """資料品質告警的口徑不得因這次改動而變動。

    CORE_FIELDS 是 8 個核心行情欄；衍生欄（7d 視窗那些）本來就不在裡面。
    supervisor 的 data_quality_low 用 core_stale_count>=2 觸發，
    這次改判據不可讓它開始亂叫。
    """
    from l3_dispatcher.scheduler import _snap_quality
    q = _snap_quality(_snap(
        price=60000.0, ts=1, oi=1.0, oi_delta_pct=0.1, funding=0.0001,
        funding_predicted=0.0001, top_trader_ratio=1.0, ls_ratio=1.0,
        # 衍生欄全 None——就是線上現況
        strength_score=None, atr_pct_7d=None, vol_24h_vs_30d=None,
        cvd_slope_7d=None, top_trader_slope_7d=None, oi_delta_7d_pct=None,
        higher_lows_7d=None, price_chg_24h_pct=None,
        stale_fields=("cvd", "liq_long", "liq_short")))
    assert q["core_stale_count"] == 0, "衍生欄 None 不得灌進核心故障口徑"
    assert q["stale_count"] >= 11, "但總未知數要誠實反映，不是只有 3"


def test_snap_quality_core_count_catches_none_core_field():
    """核心欄真的是 None（連 Binance 補值都失敗）⇒ 必須計入。"""
    from l3_dispatcher.scheduler import _snap_quality
    # 先把 8 個核心欄補滿，再單獨挖掉兩個——否則測到的是 fixture 的預設 None。
    full = dict(price=60000.0, ts=1, oi=1.0, oi_delta_pct=0.1, funding=0.0001,
                funding_predicted=0.0001, top_trader_ratio=1.0, ls_ratio=1.0)
    assert _snap_quality(_snap(**full))["core_stale_count"] == 0
    full.update(funding=None, top_trader_ratio=None)
    q = _snap_quality(_snap(stale_fields=(), **full))
    assert q["core_stale_count"] == 2


@pytest.mark.parametrize("field_name", ["stale_count", "core_stale_count",
                                        "btc_gate_stale"])
def test_snap_quality_returns_all_three_keys(field_name):
    """兩處呼叫點共用同一個函式，欄位不得少——少一欄就是又一次分岔。"""
    from l3_dispatcher.scheduler import _snap_quality
    assert field_name in _snap_quality(_snap())
