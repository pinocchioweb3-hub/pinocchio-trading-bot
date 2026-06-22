# -*- coding: utf-8 -*-
"""task#10：mi_get_snapshot 的 _price_chg_24h_pct 純helper（與 oi_delta_pct 同 24h 窗）。
治本 deepdive 盤整象限恆 None——提供已觀測的 24h 價格方向後備。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from market_intel_mcp.server import _price_chg_24h_pct as chg


def _mk(n, start_ts, step_ms, prices):
    """造 n 根序列（舊→新），ts 等間隔，value=prices[i]。"""
    return [{"ts": start_ts + i * step_ms, "value": prices[i]} for i in range(n)]


def test_24h_change_ms_1h_series():
    # 25 根 1h（ms），24h 前=series[-25]=100，最新=110 → +10%
    base = 1_700_000_000_000
    prices = [100.0] + [0] * 23 + [110.0]   # idx0=100(24h前), idx24=110(now)
    prices = [100.0 + i * (10.0 / 24) for i in range(25)]   # 線性 100→110
    s = _mk(25, base, 3600 * 1000, prices)
    assert chg(s) == 10.0


def test_24h_change_seconds_unit():
    # ts 以「秒」為單位 → window 應自適應成 24*3600（非 ms）
    base = 1_700_000_000
    prices = [200.0 + i * (20.0 / 24) for i in range(25)]   # 200→220
    s = _mk(25, base, 3600, prices)
    assert chg(s) == 10.0


def test_negative_change():
    base = 1_700_000_000_000
    prices = [100.0 - i * (5.0 / 24) for i in range(25)]    # 100→95
    s = _mk(25, base, 3600 * 1000, prices)
    assert chg(s) == -5.0


def test_short_series_uses_oldest():
    # 序列跨度 < 24h（只 6 根 1h）→ 退用最舊一根當基準（誠實近似）
    base = 1_700_000_000_000
    s = _mk(6, base, 3600 * 1000, [100, 101, 102, 103, 104, 106])
    assert chg(s) == 6.0   # (106-100)/100*100


def test_long_series_picks_24h_ago_not_oldest():
    # 96 根 1h：應取 24h 前(series[-25])為基準，非最舊(series[0])
    base = 1_700_000_000_000
    prices = list(range(1, 97))            # 1..96；series[-25]=72, last=96
    s = _mk(96, base, 3600 * 1000, [float(p) for p in prices])
    expected = round((96 - 72) / 72 * 100, 3)
    assert chg(s) == expected


def test_safety_none_and_empty():
    assert chg(None) is None
    assert chg([]) is None
    assert chg([{"ts": 1, "value": 100.0}]) is None        # 不足 2 根
    # 基準價為 0 → 不除零
    base = 1_700_000_000_000
    s = _mk(25, base, 3600 * 1000, [0.0] + [1.0] * 24)
    assert chg(s) is None
    # 最新價缺 → None
    s2 = _mk(25, base, 3600 * 1000, [100.0] * 25)
    s2[-1]["value"] = None
    assert chg(s2) is None
