# -*- coding: utf-8 -*-
"""task#5：止損距離推導槓桿（清算>止損）+ 名目封頂。研究 w7r04t691 落地。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from l2_trigger.leverage import (
    leverage_for_stop, compute_position, LEV_CEILING, LEV_FLOOR, LIQ_BUFFER_MULT,
)


def test_liquidation_always_beyond_stop():
    # 核心安全不變量：未夾到天花板時，清算緩衝(100/lev) ≥ 止損 × buffer
    for sl in [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 8.0, 10.0, 12.0, 15.0]:
        lev = leverage_for_stop(sl)
        assert LEV_FLOOR <= lev <= LEV_CEILING
        if lev < LEV_CEILING:
            assert (100.0 / lev) >= sl * LIQ_BUFFER_MULT - 1e-6, f"sl={sl} lev={lev}"


def test_tight_stop_high_leverage():
    # 緊止損 → 高資金效率（明顯高於舊固定 5x）
    assert leverage_for_stop(1.0) == LEV_CEILING
    assert leverage_for_stop(2.0) > 5


def test_wide_stop_lowers_leverage():
    # 寬止損 → 自動降槓桿（避免清算先於止損）
    assert leverage_for_stop(10.0) <= 7
    assert leverage_for_stop(20.0) <= 4


def test_degenerate_inputs_floor():
    assert leverage_for_stop(0) == LEV_FLOOR
    assert leverage_for_stop(None) == LEV_FLOOR
    assert leverage_for_stop(-3) == LEV_FLOOR


def test_notional_cap_engages_on_tight_stop():
    # 止損 0.5%（極緊）→ 名目爆量 25000；封頂 1250 → realized risk 下降、capped=True
    out = compute_position(entry=100.0, stop=99.5, risk_usd=125.0,
                           leverage=20, max_notional_usd=1250.0)
    assert out["capped"] is True
    assert out["notional_usd"] == 1250.0
    assert out["realized_risk_usd"] < 125.0
    assert abs(out["margin_usd"] - 1250.0 / 20) < 0.01


def test_no_cap_backward_compat():
    out = compute_position(entry=100.0, stop=96.0, risk_usd=125.0, leverage=10)
    assert out["capped"] is False
    assert out["realized_risk_usd"] == 125.0
    assert abs(out["margin_usd"] - out["notional_usd"] / 10) < 0.01


def test_efficiency_vs_old_5x_margin():
    # 同止損下，stop-aware 槓桿的保證金應顯著低於舊 5x（解 51008 餘額不足）
    sl_pct = 3.0
    entry, stop = 100.0, 97.0  # 3% 止損
    lev_new = leverage_for_stop(sl_pct)
    new = compute_position(entry, stop, 125.0, lev_new)
    old = compute_position(entry, stop, 125.0, 5)
    assert new["margin_usd"] < old["margin_usd"]  # 更省保證金
