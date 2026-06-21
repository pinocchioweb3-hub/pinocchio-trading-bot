# -*- coding: utf-8 -*-
"""task#5/#6：模擬盤交易 Session 訊號品質閘（min R:R）；紙上驗證 Session 不受限。"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from l3_dispatcher.demo_operator import (
    signal_rr, is_quality_signal, select_new_signals, DEMO_MIN_RR,
)


def test_signal_rr_math():
    assert signal_rr(100, 95, 110) == 2.0          # 多：reward10/risk5
    assert signal_rr(100, 105, 90) == 2.0          # 空：reward10/risk5
    assert signal_rr(100, 100, 110) is None        # 零風險距離
    assert signal_rr(100, 95, None) is None


def test_quality_gate_rejects_low_rr():
    assert not is_quality_signal({"entry_price": 100, "stop_price": 95, "tp1": 105})  # R:R 1.0<1.5
    assert is_quality_signal({"entry_price": 100, "stop_price": 95, "tp1": 110})       # R:R 2.0
    assert is_quality_signal({"entry_price": 100, "stop_price": 95, "tp1": None})      # 無 tp1→不擋


def test_select_filters_low_rr_demo_only():
    now = int(time.time() * 1000)
    rows = [
        {"id": 1, "setup": "deepdive", "status": "open", "direction": "bull",
         "entry_price": 100, "stop_price": 95, "tp1": 105, "entry_at": now},   # R:R 1.0 → demo 擋
        {"id": 2, "setup": "deepdive", "status": "open", "direction": "bull",
         "entry_price": 100, "stop_price": 95, "tp1": 120, "entry_at": now},   # R:R 4.0 → demo 過
    ]
    picked, hwm = select_new_signals(rows, 0, now)
    ids = [r["id"] for r in picked]
    assert ids == [2] and hwm == 2   # 低 R:R 被擋且 hwm 推進（不重試）


def test_threshold_is_configurable():
    assert DEMO_MIN_RR >= 1.0
