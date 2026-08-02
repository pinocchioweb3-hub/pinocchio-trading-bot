"""訊號預檢閘（v219）── 桶數必須算給 LLM 看，且「零桶／盤點失敗」要在程式端擋掉 actionable。

治的是什麼：
    synthesizer 的訊號模式規則寫著「獨立確認 ≥2 桶才可 actionable」，但那個「桶」的數目
    從來沒有任何地方算出來給 LLM——一條數值門檻交給讀者目測。同時，就算 LLM 讀懂了，
    prompt 也只是請求：模型仍可能回 actionable=true。把關必須落在程式端。

⛔ 本檔刻意**不**測「data_n < 模式門檻要擋卡」——那是政策變更，需要資料裁決，不是本次範圍。
   只釘死無爭議的兩種：一個桶都沒有（第二步無法執行）、盤點失敗（未知不可折成 0 或 4）。
"""
from __future__ import annotations

import pytest

from l3_dispatcher import signal_preflight as sp


def _state(**kw) -> dict:
    base = {
        "coinglass": {},
        "snapshot": {},
        "pattern": {"consensus": "bull"},
        "smc_levels": {"4h": {"current_price": 1.0, "swing_points": []}},
    }
    base.update(kw)
    return base


# ── ① 桶數盤點：量到 0 也是量到 ─────────────────────────────────────────


def test_counts_live_buckets_from_coinglass():
    v = sp.preflight_verdict(_state(coinglass={
        "oi": [1, 2, 3], "funding": 0.0001, "ls_ratio": 1.1, "cvd": [5, 6]}))
    assert v["status"] == sp.STATUS_OK
    assert v["data_n"] == 4
    assert all(b["source"] == "CoinGlass" for b in v["buckets"].values())


def test_counts_live_buckets_from_backup_snapshot():
    v = sp.preflight_verdict(_state(snapshot={
        "oi": 1_000_000, "funding": -0.00002, "top_trader_ratio": 1.4, "cvd_slope": -50.0}))
    assert v["data_n"] == 4
    assert all(b["source"] == "備援" for b in v["buckets"].values())


def test_measured_zero_is_a_real_value_not_missing():
    """量到的 0 是答案。折成「缺料」是本專案同物種的反方向。"""
    v = sp.preflight_verdict(_state(snapshot={"funding": 0.0, "cvd_slope": 0.0}))
    assert v["buckets"]["funding"]["live"] is True
    assert v["buckets"]["cvd"]["live"] is True
    assert v["data_n"] == 2


def test_empty_list_is_not_a_value():
    """CG 停權後回的是空序列＝沒量到，不可算成一個桶。"""
    v = sp.preflight_verdict(_state(coinglass={"oi": [], "cvd": []}))
    assert v["data_n"] == 0


def test_snapshot_self_reported_error_discards_that_lane():
    """快照自報壞掉時，它報的值不可採信（否則壞檔的殘值會被算成桶）。"""
    v = sp.preflight_verdict(_state(snapshot={"error": "boom", "funding": 0.001, "oi": 5}))
    assert v["data_n"] == 0
    assert v["block_actionable"] is True


# ── ② 零桶／缺型態＝硬擋，且理由必須明講是缺料 ─────────────────────────


def test_zero_buckets_blocks_actionable():
    v = sp.preflight_verdict(_state())
    assert v["data_n"] == 0
    assert v["block_actionable"] is True
    assert "不等於" in v["reason"]          # 必須明講缺料≠中性


def test_one_bucket_does_not_block():
    """⛔ 低於模式門檻不硬擋——那是政策變更，會無憑據地砍樣本數。"""
    v = sp.preflight_verdict(_state(snapshot={"funding": 0.0003}))
    assert v["data_n"] == 1
    assert v["block_actionable"] is False


def test_missing_form_blocks_even_with_full_data():
    v = sp.preflight_verdict(_state(
        coinglass={"oi": [1], "funding": 0.1, "ls_ratio": 1.0, "cvd": [2]},
        pattern={"error": "x"}, smc_levels={"4h": {"error": "swing failed"}}))
    assert v["form_ok"] is False
    assert v["block_actionable"] is True
    assert "型態" in v["reason"]


# ── ③ fail-closed：盤點不出來 ≠ 沒有數據 ───────────────────────────────


@pytest.mark.parametrize("bad", ["not-a-dict", 123, ["x"]])
def test_unreadable_state_is_unknown_not_zero(bad):
    v = sp.preflight_verdict(bad)
    assert v["status"] == sp.STATUS_UNKNOWN
    assert v["data_n"] is None, "盤點失敗被折成一個數字＝重犯同物種"
    assert v["block_actionable"] is True
    assert "盤點" in v["reason"] and "不等於" in v["reason"]


def test_wrong_type_subobject_is_unknown():
    v = sp.preflight_verdict({"coinglass": "oops", "snapshot": {}})
    assert v["status"] == sp.STATUS_UNKNOWN


def test_unknown_and_zero_have_different_wording():
    """兩種完全不同的事實不可寫出同一句話，否則區分了也讀不出來。"""
    zero = sp.render_preflight_block(sp.preflight_verdict(_state()))
    unk = sp.render_preflight_block(sp.preflight_verdict("broken"))
    assert zero != unk
    assert "盤點失敗" in unk and "盤點失敗" not in zero


# ── ④ 渲染：桶數要真的印出來 ───────────────────────────────────────────


def test_render_shows_bucket_count_and_all_four_labels():
    txt = sp.render_preflight_block(sp.preflight_verdict(_state(
        snapshot={"funding": 0.0001, "oi": 3})))
    assert "2/4" in txt
    for _k, label in sp._DATA_BUCKETS:
        assert label in txt
    assert "不要自己重數" in txt


# ── ⑤ 程式端強制（prompt 是請求，不是把關）─────────────────────────────


def test_enforce_downgrades_actionable_true():
    v = sp.preflight_verdict(_state())
    plan = sp.enforce_plan({"actionable": True, "direction": "bull"}, v)
    assert plan["actionable"] is False
    assert plan["preflight_downgraded"] is True
    assert plan["preflight_reason"]


def test_enforce_is_noop_when_gate_passes():
    v = sp.preflight_verdict(_state(coinglass={"funding": 0.1, "oi": [1]}))
    plan = sp.enforce_plan({"actionable": True}, v)
    assert plan["actionable"] is True
    assert "preflight_downgraded" not in plan


def test_enforce_tolerates_missing_plan():
    assert sp.enforce_plan(None, sp.preflight_verdict(_state())) is None
