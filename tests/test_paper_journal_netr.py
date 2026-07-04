# -*- coding: utf-8 -*-
"""v118 淨值口徑：compute_net_r 純函式 + net_r 欄遷移。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from l3_dispatcher.paper_journal import compute_net_r


def test_fee_math_r_units():
    """entry=100, stop=96(R距=4)：費用R = 2×0.0005×100/4 = 0.025R。tp3 無滑價。"""
    net = compute_net_r(2.0, 100, 96, "tp3", fee_rate=0.0005, stop_slip_r=0.05)
    assert net == round(2.0 - 0.025, 4)


def test_stop_adds_slippage():
    """stop 出場加滑價 0.05R：-1.0 − 0.025 − 0.05 = -1.075。"""
    net = compute_net_r(-1.0, 100, 96, "stop", fee_rate=0.0005, stop_slip_r=0.05)
    assert net == -1.075


def test_timeout_no_slip():
    net = compute_net_r(0.5, 100, 96, "timeout", fee_rate=0.0005, stop_slip_r=0.05)
    assert net == round(0.5 - 0.025, 4)


def test_tight_stop_costs_more_r():
    """止損越近，同樣費用吃掉越多 R（entry=100, stop=99.5 → 費用R=0.2）——誠實反映。"""
    net = compute_net_r(1.0, 100, 99.5, "tp1", fee_rate=0.0005, stop_slip_r=0.05)
    assert net == round(1.0 - 0.2, 4)


def test_degenerate_honest_none():
    assert compute_net_r(1.0, 100, 100, "tp1") is None      # 零風險距離
    assert compute_net_r(1.0, None, 96, "tp1") is None      # 缺進場價
    assert compute_net_r(1.0, 0, 96, "tp1") is None         # 非法價
