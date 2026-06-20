"""復盤引擎 step8（task#53）── auto_optimizer 編排層測試。

覆蓋：小樣本→0 晉升＋活躍表恆空（紅線③誠實答案）、同質大樣本 L2 仍擋、分桶
(symbol×quadrant)、帳本跨日冪等（n_trials 不灌水）、quadrant 解析、grid 跳過與
champion 同分配者、帳本鏈完整。全離線、注入 rows 繞過 DB、暫存帳本/覆寫檔。
"""
import json
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from l3_dispatcher import auto_optimizer as ao
from l3_dispatcher import auto_param_store as aps
from l3_dispatcher.champion_challenger import champion_alloc
from backtest.l2_stat_gates import TrialLedger


def _champ_consistent_r():
    """以預設 champion(0.5/0.3/0.2) 回放 tp1,tp2,stop 的帳本一致 R。"""
    a1, a2, _ = champion_alloc().tp_alloc
    rem = 1.0 - a1 - a2
    return round(a1 * 1.0 + a2 * 2.0 + rem * (-1.0), 6)


def _mk(tid, r, *, q="price_up_oi_up", sym="BTC", snapshot=True):
    snap = (json.dumps({"regime_at_entry": {"oi_price_quadrant": q}})
            if snapshot else None)
    return {"id": tid, "symbol": sym, "setup": "intraday", "direction": "bull",
            "entry_price": 100.0, "stop_price": 90.0, "tp1": 110.0, "tp2": 120.0,
            "tp3": 140.0, "entry_at": 0, "exit_at": 10, "legs_hit": "tp1,tp2,stop",
            "exit_reason": "stop", "realized_r": r, "pnl_usd": r * 100,
            "entry_filled_pct": 1.0, "plan_snapshot": snap, "tp_alloc": None}


def _env(td):
    return {"active_path": Path(td) / "active.json",
            "audit_path": Path(td) / "audit.jsonl",
            "ledger": TrialLedger(Path(td) / "ledger.jsonl")}


def test_module_selftest_passes():
    assert ao._selftest() is True


def test_small_sample_zero_promoted_active_empty():
    r = _champ_consistent_r()
    with tempfile.TemporaryDirectory() as td:
        e = _env(td)
        rows = [_mk(i, r) for i in range(10)]      # <30 → minTRL fail-closed
        res = ao.run_optimization(rows=rows, at_ms=1, **e)
        assert res["n_promoted"] == 0
        assert not e["active_path"].exists()
        assert aps.resolve_tp_alloc("BTC", "price_up_oi_up",
                                    active_path=e["active_path"]) is None


def test_homogeneous_large_sample_zero_promoted():
    """每筆 R 相同 → 離散=0 → 即使 ≥30 筆，L2 仍擋（誠實不誤晉升）。"""
    r = _champ_consistent_r()
    with tempfile.TemporaryDirectory() as td:
        e = _env(td)
        rows = [_mk(100 + i, r) for i in range(40)]
        res = ao.run_optimization(rows=rows, at_ms=1, **e)
        assert res["n_promoted"] == 0
        assert aps.resolve_tp_alloc("BTC", "price_up_oi_up",
                                    active_path=e["active_path"]) is None


def test_grouping_by_symbol_and_quadrant():
    r = _champ_consistent_r()
    with tempfile.TemporaryDirectory() as td:
        e = _env(td)
        rows = ([_mk(200 + i, r, q="price_up_oi_up", sym="BTC") for i in range(3)] +
                [_mk(300 + i, r, q="price_down_oi_up", sym="BTC") for i in range(3)] +
                [_mk(400 + i, r, q="price_up_oi_up", sym="ETH") for i in range(3)])
        res = ao.run_optimization(rows=rows, at_ms=1, **e)
        assert res["n_buckets"] == 3
        keys = {b["bucket"] for b in res["buckets"]}
        assert keys == {"BTC|price_up_oi_up", "BTC|price_down_oi_up", "ETH|price_up_oi_up"}


def test_quadrant_parsing():
    assert ao._quadrant_of(_mk(1, 0.0, q="price_up_oi_up")) == "price_up_oi_up"
    assert ao._quadrant_of(_mk(2, 0.0, snapshot=False)) == "unknown"
    assert ao._quadrant_of({"plan_snapshot": "{bad json"}) == "unknown"
    assert ao._quadrant_of({}) == "unknown"


def test_ledger_idempotent_across_runs():
    """同 rows 重跑兩次 → 帳本族群數不變（穩定 trial_id，n_trials 不灌水）。"""
    r = _champ_consistent_r()
    with tempfile.TemporaryDirectory() as td:
        e = _env(td)
        rows = [_mk(i, r) for i in range(40)]
        ao.run_optimization(rows=rows, at_ms=1, **e)
        n1 = e["ledger"].count_trials("BTC|price_up_oi_up")
        ao.run_optimization(rows=rows, at_ms=2, **e)   # 不同 at_ms，但 trial_id 應穩定
        n2 = e["ledger"].count_trials("BTC|price_up_oi_up")
        assert n1 == n2 and n1 >= 1          # 重跑未新增 distinct trial
        # 家族大小＝grid 中與 champion 相異者數（預設 champion 0.5/0.3/0.2 → 4 個皆相異）
        assert n1 == len(ao.CANDIDATE_GRID)


def test_candidate_grid_skips_champion_equal_override():
    """若某桶覆寫＝grid 內某配置，該配置應被跳過（不自我比較）。"""
    r = _champ_consistent_r()
    with tempfile.TemporaryDirectory() as td:
        e = _env(td)
        # 先把 BTC|price_up_oi_up 覆寫成 grid 的 balanced(0.4/0.3/0.3)
        grid_member = next(p for p in ao.CANDIDATE_GRID if tuple(p.tp_alloc) == (0.4, 0.3, 0.3))
        e["active_path"].write_text(json.dumps({"version": 1, "updated_at_ms": 0,
            "buckets": {"BTC|price_up_oi_up": {"tp_alloc": list(grid_member.tp_alloc)}}}),
            encoding="utf-8")
        rows = [_mk(i, r) for i in range(5)]
        res = ao.run_optimization(rows=rows, at_ms=1, **e)
        b = res["buckets"][0]
        # champion 變成覆寫值；evaluated 不含與 champion 同分配者
        assert b["champion_alloc"] == [0.4, 0.3, 0.3]
        for chal_pol, _v in b["evaluated"]:
            assert tuple(chal_pol.tp_alloc) != (0.4, 0.3, 0.3)


def test_ledger_chain_intact_after_run():
    r = _champ_consistent_r()
    with tempfile.TemporaryDirectory() as td:
        e = _env(td)
        rows = [_mk(i, r) for i in range(40)]
        ao.run_optimization(rows=rows, at_ms=1, **e)
        ok, detail = e["ledger"].verify_chain()
        assert ok, detail


def test_empty_rows_returns_none_report():
    with tempfile.TemporaryDirectory() as td:
        e = _env(td)
        res = ao.run_optimization(rows=[], at_ms=1, **e)
        assert res["n_buckets"] == 0
        assert ao.render_report(res, active_path=e["active_path"]) is None
