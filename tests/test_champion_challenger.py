"""復盤引擎 step7（task#52）── champion/challenger 離線回放測試。

覆蓋：忠實回放（tp/stop/timeout/filled 各腿型）、self-check 抓竄改、compare_allocation
不誤晉升（同質樣本→L2 擋）、不可回放→None、紅線③範圍誠實（不假裝能算 level 變更）。
全離線、零網路、零 DB（合成資料）。
"""
import sys
import tempfile
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from l3_dispatcher import champion_challenger as cc


def _mk(tid, direction, entry, stop, tp1, tp2, tp3, legs, realized_r, filled=1.0):
    return {"id": tid, "symbol": "TST", "setup": "intraday", "direction": direction,
            "entry_price": entry, "stop_price": stop, "tp1": tp1, "tp2": tp2, "tp3": tp3,
            "entry_at": 0, "exit_at": 10, "legs_hit": legs,
            "exit_reason": legs.split(",")[-1], "realized_r": realized_r,
            "pnl_usd": realized_r * 100, "entry_filled_pct": filled}


def test_module_selftest_passes():
    assert cc._selftest() is True


def test_champion_replay_reproduces_ledger():
    champ = cc.champion_alloc()
    a1, a2, _ = champ.tp_alloc
    rem = 1.0 - a1 - a2
    r = round(a1 * 1.0 + a2 * 2.0 + rem * (-1.0), 6)
    t = _mk(1, "bull", 100, 90, 110, 120, 140, "tp1,tp2,stop", r)
    assert abs(cc.replay_trade_r(t, champ) - r) < 1e-6


def test_challenger_alloc_faithful_recombination():
    chal = cc.AllocPolicy("c", (0.4, 0.3, 0.3))
    t = _mk(1, "bull", 100, 90, 110, 120, 140, "tp1,tp2,stop", 0.0)
    expect = round(0.4 * 1.0 + 0.3 * 2.0 + 0.3 * (-1.0), 6)
    assert abs(cc.replay_trade_r(t, chal) - expect) < 1e-6


def test_immediate_stop_is_minus_one_any_alloc():
    t = _mk(2, "bull", 100, 90, 110, 120, 140, "stop", -1.0)
    for pol in (cc.champion_alloc(), cc.AllocPolicy("c", (0.4, 0.3, 0.3))):
        assert abs(cc.replay_trade_r(t, pol) + 1.0) < 1e-6


def test_bear_direction_replay():
    # 空單：entry=100 stop=110 (sl=10); tp1=90(+1R) tp2=80(+2R)
    champ = cc.champion_alloc()
    a1, a2, _ = champ.tp_alloc
    rem = 1.0 - a1 - a2
    r = round(a1 * 1.0 + a2 * 2.0 + rem * (-1.0), 6)
    t = _mk(3, "bear", 100, 110, 90, 80, 60, "tp1,tp2,stop", r)
    assert abs(cc.replay_trade_r(t, champ) - r) < 1e-6


def test_filled_pct_scales_r():
    champ = cc.champion_alloc()
    a1, a2, a3 = champ.tp_alloc
    r = round((a1 * 1.0 + a2 * 2.0 + a3 * 4.0) * 0.7, 6)
    t = _mk(4, "bull", 100, 90, 110, 120, 140, "tp1,tp2,tp3", r, filled=0.7)
    assert abs(cc.replay_trade_r(t, champ) - r) < 1e-6


def test_unverifiable_bad_sl_returns_none():
    t = _mk(5, "bull", 100, 100, 110, 120, 140, "tp1,stop", 0.0)  # sl_dist=0
    assert cc.replay_trade_r(t, cc.champion_alloc()) is None


def test_self_check_detects_tampered_realized_r():
    champ = cc.champion_alloc()
    a1, a2, _ = champ.tp_alloc
    rem = 1.0 - a1 - a2
    r = round(a1 * 1.0 + a2 * 2.0 + rem * (-1.0), 6)
    good = _mk(1, "bull", 100, 90, 110, 120, 140, "tp1,tp2,stop", r)
    bad = dict(good, id=2, realized_r=r + 1.0)
    n_chk, n_mis, ids = cc._self_check([good, bad])
    assert n_chk == 2 and n_mis == 1 and 2 in ids


def test_alloc_policy_validation():
    with pytest.raises(ValueError):
        cc.AllocPolicy("x", (0.5, 0.3))          # 段數錯
    with pytest.raises(ValueError):
        cc.AllocPolicy("x", (0.5, 0.3, 0.3))     # 總和 != 1.0
    with pytest.raises(ValueError):
        cc.AllocPolicy("x", (-0.1, 0.6, 0.5))    # 負值


def test_compare_allocation_homogeneous_not_promoted():
    """同質樣本（每筆 R 相同）→ 離散=0 → L2 擋 → 不誤晉升（誠實）。"""
    champ = cc.champion_alloc()
    a1, a2, _ = champ.tp_alloc
    rem = 1.0 - a1 - a2
    r = round(a1 * 1.0 + a2 * 2.0 + rem * (-1.0), 6)
    sample = [_mk(100 + i, "bull", 100, 90, 110, 120, 140, "tp1,tp2,stop", r)
              for i in range(40)]
    chal = cc.AllocPolicy("c", (0.4, 0.3, 0.3))
    with tempfile.TemporaryDirectory() as td:
        from backtest.l2_stat_gates import TrialLedger
        led = TrialLedger(Path(td) / "trial_ledger.jsonl")
        v = cc.compare_allocation(sample, chal, bucket_key="TST|bull", ledger=led)
        assert v.self_check_ok is True
        assert v.n_aligned == 40
        assert v.promote is False                # 統計上未證實 → 不晉升
        ok, _ = led.verify_chain()
        assert ok


def test_compare_allocation_small_sample_fail_closed():
    """對齊樣本 <30 → minTRL fail-closed → 一律不晉升（現況的誠實答案）。"""
    champ = cc.champion_alloc()
    a1, a2, _ = champ.tp_alloc
    rem = 1.0 - a1 - a2
    r = round(a1 * 1.0 + a2 * 2.0 + rem * (-1.0), 6)
    sample = [_mk(i, "bull", 100, 90, 110, 120, 140, "tp1,tp2,stop", r) for i in range(10)]
    chal = cc.AllocPolicy("c", (0.4, 0.3, 0.3))
    with tempfile.TemporaryDirectory() as td:
        from backtest.l2_stat_gates import TrialLedger
        led = TrialLedger(Path(td) / "trial_ledger.jsonl")
        v = cc.compare_allocation(sample, chal, bucket_key="TST|bull", ledger=led)
        assert v.promote is False
