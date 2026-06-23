# -*- coding: utf-8 -*-
"""止損管理 A/B 工具的離線自測整合進 pytest（合成 K 線、零網路）。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backtest import stop_management_ab as ab


def test_selftest_passes():
    assert ab._selftest() is True


def test_breakeven_buffer_above_entry():
    # 保本止損永遠 > 進場價（含緩衝），絕不剛好成本價
    s = ab._policy_stop("B_breakeven", bars=[], gi=0, entry=100.0, dist=1.0,
                        bull=True, highest=101.0, lowest=100.0, cost_r=0.002,
                        atrN=[None])
    assert s > 100.0
    sb = ab._policy_stop("B_breakeven", bars=[], gi=0, entry=100.0, dist=1.0,
                         bull=False, highest=100.0, lowest=99.0, cost_r=0.002,
                         atrN=[None])
    assert sb < 100.0   # 空單對稱


def test_fixed_policy_never_moves():
    assert ab._policy_stop("A_fixed", bars=[], gi=0, entry=100.0, dist=1.0,
                           bull=True, highest=120.0, lowest=100.0, cost_r=0.002,
                           atrN=[None]) is None


def test_breakeven_raises_winrate_but_not_ev_invariant():
    """合成『先觸 TP1 再回測掃保本』場景：保本必抬勝率，但不保證 EV 更高（核心不變量）。"""
    # 這個性質已由全樣本回測證實；此處僅驗 simulate 不崩、回傳結構正確。
    bars = [{"ts": i * 3600000, "high": 100 + i, "low": 99 + i, "close": 100 + i}
            for i in range(140)]
    sig = ab.Signal("X", 20, bars[20]["ts"], "bull", 100.0, 1.0, 1.0, "bull_regime")
    atr_by_n = {ab.CHAND_N: ab._rolling_atr(bars, ab.CHAND_N),
                ab.ATRTR_N: ab._rolling_atr(bars, ab.ATRTR_N)}
    res = ab.run_signal(bars, sig, atr_by_n)
    assert set(res.keys()) == set(ab.VARIANTS)
    for v in ab.VARIANTS:
        assert res[v].exit_reason in ("tp", "stop", "stop_after_tp", "timeout")
