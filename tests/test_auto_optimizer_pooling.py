# -*- coding: utf-8 -*-
"""task#9：auto_optimizer 階層池化 + auto_param_store 解析階梯（鏡像 entry_policy task#62）。"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from l3_dispatcher import auto_param_store as aps


def test_resolution_ladder_order():
    assert aps._resolution_ladder("BTC", "price_up_oi_up") == [
        "BTC|price_up_oi_up", "*|price_up_oi_up", "*|*"]


def test_ladder_dedup_when_symbol_is_pool():
    assert aps._resolution_ladder(aps.POOL, "q") == ["*|q", "*|*"]


def test_resolve_empty_is_none(tmp_path):
    # 無覆寫 → None（inert：現 promotions=0，零行為變更）
    assert aps.resolve_tp_alloc("BTC", "price_up_oi_up",
                                active_path=str(tmp_path / "a.json")) is None


def test_resolve_falls_back_to_global_pool(tmp_path):
    p = tmp_path / "a.json"
    p.write_text(json.dumps({"buckets": {"*|*": {"tp_alloc": [0.5, 0.3, 0.2]}}}),
                 encoding="utf-8")
    assert aps.resolve_tp_alloc("BTC", "anyq", active_path=str(p)) == (0.5, 0.3, 0.2)


def test_resolve_specific_wins_over_pool(tmp_path):
    p = tmp_path / "a.json"
    p.write_text(json.dumps({"buckets": {
        "BTC|q": {"tp_alloc": [0.6, 0.2, 0.2]},
        "*|*": {"tp_alloc": [0.5, 0.3, 0.2]}}}), encoding="utf-8")
    assert aps.resolve_tp_alloc("BTC", "q", active_path=str(p)) == (0.6, 0.2, 0.2)


def test_resolve_quadrant_pool_between(tmp_path):
    # per-symbol 無、象限池有 → 取象限池（中間階）
    p = tmp_path / "a.json"
    p.write_text(json.dumps({"buckets": {
        "*|q": {"tp_alloc": [0.4, 0.3, 0.3]},
        "*|*": {"tp_alloc": [0.5, 0.3, 0.2]}}}), encoding="utf-8")
    assert aps.resolve_tp_alloc("BTC", "q", active_path=str(p)) == (0.4, 0.3, 0.3)


def test_run_optimization_creates_pooled_buckets(tmp_path):
    from l3_dispatcher import auto_optimizer
    from backtest.l2_stat_gates import TrialLedger

    def _mk(tid, q):
        snap = json.dumps({"regime_at_entry": {"oi_price_quadrant": q}})
        return {"id": tid, "symbol": "BTC", "setup": "intraday", "direction": "bull",
                "entry_price": 100.0, "stop_price": 90.0, "tp1": 110.0, "tp2": 120.0,
                "tp3": 140.0, "entry_at": 0, "exit_at": 10, "legs_hit": "tp1,stop",
                "exit_reason": "stop", "realized_r": 0.1, "pnl_usd": 10,
                "entry_filled_pct": 1.0, "plan_snapshot": snap, "tp_alloc": None}
    rows = [_mk(i, "price_up_oi_up") for i in range(4)]
    res = auto_optimizer.run_optimization(
        rows=rows, at_ms=1, ledger=TrialLedger(tmp_path / "l.jsonl"),
        active_path=str(tmp_path / "a.json"), audit_path=str(tmp_path / "au.jsonl"))
    # 1 symbol × 1 quadrant → per-symbol 1 + 象限池 1 + 全域池 1 = 3
    assert res["n_buckets"] == 3 and res["n_pooled"] == 2
    assert res["n_promoted"] == 0   # 小樣本 → L2 擋（門檻未降）
