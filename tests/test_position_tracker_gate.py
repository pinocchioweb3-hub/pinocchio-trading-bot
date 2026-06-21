# -*- coding: utf-8 -*-
"""task#4(B) 持倉快照變化指紋閘測試（降噪：純價格漂移不重推）。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from l3_dispatcher.macro import (
    _r_bucket, _position_fingerprint, _should_push_positions, _POS_HEARTBEAT_MS,
)

P = {"_kind": "paper", "symbol": "BTC", "direction": "bull",
     "entry_price": 100.0, "stop_price": 90.0, "legs_hit": []}


def test_r_bucket_bands():
    assert _r_bucket(1.2) == "tp1+"
    assert _r_bucket(0.7) == "half"
    assert _r_bucket(0.2) == "up"
    assert _r_bucket(-0.3) == "dn"
    assert _r_bucket(-0.9) == "near_sl"


def test_pure_price_drift_same_fingerprint():
    # 105→0.5R 與 107→0.7R 同屬 "half" 檔 → 指紋相同 → 不重推
    assert _position_fingerprint([P], {"BTC": 105.0}) == _position_fingerprint([P], {"BTC": 107.0})


def test_cross_r_bucket_changes_fingerprint():
    # 105→0.5R(half) vs 111→1.1R(tp1+) → 指紋不同 → 應重推
    assert _position_fingerprint([P], {"BTC": 105.0}) != _position_fingerprint([P], {"BTC": 111.0})


def test_new_leg_changes_fingerprint():
    p2 = {**P, "legs_hit": ["tp1"]}
    assert _position_fingerprint([P], {"BTC": 105.0}) != _position_fingerprint([p2], {"BTC": 105.0})


def test_added_position_changes_fingerprint():
    q = {"_kind": "paper", "symbol": "ETH", "direction": "bear",
         "entry_price": 50.0, "stop_price": 55.0, "legs_hit": []}
    fp1 = _position_fingerprint([P], {"BTC": 105.0})
    fp2 = _position_fingerprint([P, q], {"BTC": 105.0, "ETH": 48.0})
    assert fp1 != fp2


def test_order_independent_fingerprint():
    q = {"_kind": "paper", "symbol": "ETH", "direction": "bear",
         "entry_price": 50.0, "stop_price": 55.0, "legs_hit": []}
    pr = {"BTC": 105.0, "ETH": 48.0}
    assert _position_fingerprint([P, q], pr) == _position_fingerprint([q, P], pr)


def test_missing_price_marked():
    assert "noprice" in _position_fingerprint([P], {})


def test_gate_pushes_on_change():
    assert _should_push_positions("fpB", "fpA", 1000, 0) is True


def test_gate_skips_unchanged_within_heartbeat():
    assert _should_push_positions("fpA", "fpA", 1000, 0) is False


def test_gate_heartbeat_forces_push():
    assert _should_push_positions("fpA", "fpA", _POS_HEARTBEAT_MS + 1, 0) is True
