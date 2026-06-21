# -*- coding: utf-8 -*-
"""task#5：demo 路徑(atr=None)槓桿資金效率治本——解 51008 餘額不足。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from l4_execution.demo_trader import choose_safe_leverage, max_safe_leverage
from l2_trigger.leverage import compute_position


def test_unknown_atr_now_efficient_not_5x():
    # demo 路徑 atr=None：舊版硬退守 5x；新版用止損推導效率，明顯更高
    lev = choose_safe_leverage("BTC", 100.0, 97.0, atr_pct_7d=None)  # 3% 止損
    assert lev > 5


def test_always_liquidation_safe():
    # 不論選出多少，都不超過 mmr-aware 清算安全上限（清算永不先於止損）
    for entry, stop in [(100, 97), (100, 98), (100, 95), (50, 49), (3.0, 2.91)]:
        lev = choose_safe_leverage("BTC", entry, stop, None)
        assert 1 <= lev <= max_safe_leverage(entry, stop)


def test_symbol_override_still_wins():
    lev = choose_safe_leverage("WLFI", 100.0, 97.0, None)
    assert lev <= 5  # WLFI override=5（再被安全上限夾）


def test_explicit_tier_respected():
    lev = choose_safe_leverage("BTC", 100.0, 97.0, None, tier_leverage=8)
    assert lev <= 8


def test_margin_drops_vs_old_5x():
    # 同一 3% 止損：新效率槓桿的保證金顯著低於舊 5x（這就是解 51008 的關鍵）
    entry, stop, risk = 100.0, 97.0, 125.0
    lev_new = choose_safe_leverage("BTC", entry, stop, None)
    m_new = compute_position(entry, stop, risk, lev_new)["margin_usd"]
    m_old = compute_position(entry, stop, risk, 5)["margin_usd"]
    assert m_new < m_old
    assert m_new <= m_old / 2  # 至少省一半保證金
