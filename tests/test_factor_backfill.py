"""task#20 因子回補純函式契約測試（離線、零網路、零訊號變更）。

鎖住三個「死權重 / 反符號」因子的正確推導，避免日後改動悄悄回退：
  1. top_trader_dev＝大戶帳戶多空比 − 1（不是 ratio 本身、不是別的偏移）。
  2. btc_corr_30d **必須用日報酬**算，不可退回用價位（價位偽高相關 → 死權重沒治到）。
  3. vol_24h_vs_30d **量增 → 比值增**（修正 universe stub 把符號弄反的 bug）。
  4. 資料不足/壞值一律誠實 None，不捏造（紅線③）。

全離線：只測 backtest.factor_backfill 純函式（不 import daemon 來源、不載 .env、不觸網）。
"""
import math
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backtest.factor_backfill import (
    top_trader_dev_from_ratio,
    daily_returns,
    btc_corr_from_closes,
    vol_ratio_24h_vs_30d,
    _selftest,
)
from backtest.binance_cvd_validate import _pearson


# ════════════════════════════════════════════════════════════════════════
#  top_trader_dev
# ════════════════════════════════════════════════════════════════════════
def test_top_trader_dev_is_ratio_minus_one():
    assert top_trader_dev_from_ratio(1.5) == 0.5
    assert top_trader_dev_from_ratio(1.0) == 0.0
    assert abs(top_trader_dev_from_ratio(0.7) - (-0.3)) < 1e-9   # 大戶淨空 → 負


def test_top_trader_dev_honest_none_on_bad_input():
    assert top_trader_dev_from_ratio(None) is None
    assert top_trader_dev_from_ratio("x") is None
    assert top_trader_dev_from_ratio([]) is None


def test_top_trader_dev_breaks_dead_constant():
    # 治本核心：不同幣不同 ratio → 不同 dev（stub 是常數 0.05，z-score 全 0 = 死權重）
    devs = [top_trader_dev_from_ratio(r) for r in (1.93, 2.54, 3.50, 1.77)]
    assert len(set(devs)) == 4          # 全相異 → 截面有差異 → 權重活過來


# ════════════════════════════════════════════════════════════════════════
#  daily_returns
# ════════════════════════════════════════════════════════════════════════
def test_daily_returns_basic():
    dr = daily_returns([100, 110, 99])
    assert len(dr) == 2
    assert abs(dr[0] - 0.1) < 1e-9
    assert abs(dr[1] - (-0.1)) < 1e-9


def test_daily_returns_too_short():
    assert daily_returns([100]) == []
    assert daily_returns([]) == []
    assert daily_returns(None) == []


def test_daily_returns_honest_none_on_dirty():
    r = daily_returns([100, 0, 50])      # p0=0 那步無法算 → None
    assert r[0] == -1.0 and r[1] is None
    r2 = daily_returns([100, "bad", 120])
    assert r2[0] is None and r2[1] is None


# ════════════════════════════════════════════════════════════════════════
#  btc_corr_30d（核心：用報酬不用價位）
# ════════════════════════════════════════════════════════════════════════
def _walk(noise_fn, drift=0.0, n=30, seed=100.0):
    out = [seed]
    for i in range(n):
        out.append(out[-1] * (1 + drift + noise_fn(i)))
    return out


def test_btc_corr_perfect_sync_is_one():
    btc = _walk(lambda i: 0.02 * math.sin(i))
    assert abs(btc_corr_from_closes(btc, btc) - 1.0) < 1e-6


def test_btc_corr_inverse_is_minus_one():
    btc = _walk(lambda i: 0.02 * math.sin(i))
    inv = _walk(lambda i: -0.02 * math.sin(i))
    c = btc_corr_from_closes(inv, btc)
    assert c is not None and abs(c - (-1.0)) < 1e-6


def test_btc_corr_zero_variance_returns_none():
    # 固定 +1% 報酬 → 零變異 → Pearson 未定義 → 誠實 None（不假裝 1.0）
    flat = [100 * (1.01 ** i) for i in range(31)]
    assert btc_corr_from_closes(flat, flat) is None


def test_btc_corr_insufficient_points_returns_none():
    assert btc_corr_from_closes([100, 101, 102], [100, 99, 98]) is None


def test_btc_corr_uses_returns_not_price_levels():
    # 治本核心證明：兩條都帶上行漂移的序列 → 價位偽高相關，但報酬幾乎不相關。
    # 若 btc_corr 退回用價位，corr 會 >0.9（全幣都被 _btc_corr_score 打成同一個常數）。
    a = _walk(lambda i: 0.03 * math.sin(i), drift=0.02)
    b = _walk(lambda i: 0.03 * math.cos(i), drift=0.02)
    corr_ret = btc_corr_from_closes(a, b)
    corr_price = _pearson(a[-31:], b[-31:])
    assert corr_price > 0.9                       # 價位偽高相關
    assert abs(corr_ret) < abs(corr_price)        # 報酬才有真正的區分力


def test_btc_corr_tail_aligns_shorter_history():
    # 新上市幣歷史較短 → 尾端對齊後只要可解析配對 ≥ min_points 仍可算
    btc = _walk(lambda i: 0.02 * math.sin(i))
    short = btc[-25:]                              # 24 個報酬
    assert btc_corr_from_closes(short, btc) is not None


# ════════════════════════════════════════════════════════════════════════
#  vol_24h_vs_30d（核心：修正反符號）
# ════════════════════════════════════════════════════════════════════════
def test_vol_ratio_value():
    assert vol_ratio_24h_vs_30d(200.0, [100.0] * 30) == 2.0
    assert vol_ratio_24h_vs_30d(50.0, [100.0] * 30) == 0.5


def test_vol_ratio_sign_is_correct():
    # 量增 → 比值增（stub 的 (1−vol_change/100)/0.85 是反的）
    hi = vol_ratio_24h_vs_30d(300.0, [100.0] * 30)
    lo = vol_ratio_24h_vs_30d(80.0, [100.0] * 30)
    assert hi > lo


def test_vol_ratio_honest_none():
    assert vol_ratio_24h_vs_30d(200.0, [100.0] * 5) is None    # 不足 min_days
    assert vol_ratio_24h_vs_30d(200.0, [0.0] * 30) is None     # 均量 0 → 不除零
    assert vol_ratio_24h_vs_30d(None, [100.0] * 30) is None
    assert vol_ratio_24h_vs_30d("x", [100.0] * 30) is None


# ════════════════════════════════════════════════════════════════════════
#  模組自測（離線）
# ════════════════════════════════════════════════════════════════════════
def test_module_selftest_passes():
    assert _selftest() == 0
