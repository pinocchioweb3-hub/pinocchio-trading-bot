"""task#20 回測閘：Binance 自算 CVD 純函式 契約測試（離線、零網路、零真錢、零訊號變更）。

把 backtest/binance_cvd_validate.py 的核心數學不變量拉進 pytest 迴歸網，避免日後改動
（或 live 接線）悄悄弄壞 CVD 推導。鎖住三件事：
  1. cvd_slopes_from_klines 與 coinglass.get_cvd_series 同構（每根 delta_pct、短窗/7d 窗、壞根跳過）。
  2. cumsum 差分可無損還原每根 delta（時序相關性的基礎）。
  3. Pearson r 與小時桶跨單位（ms/s）對齊正確。

全離線：不 import 任何 daemon 來源、不載 .env、不觸網。
"""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backtest.binance_cvd_validate import (
    cvd_slopes_from_klines, _deltas_from_cumsum, _pearson,
    _perbar_correlation, _sign, _selftest,
)


def _row(ts, quote_vol, taker_buy_quote):
    r = [0] * 12
    r[0] = ts
    r[7] = quote_vol
    r[10] = taker_buy_quote
    return r


# ── cvd_slopes_from_klines：同構推導 ─────────────────────────────────────
def test_all_buy_slope_plus_100():
    out = cvd_slopes_from_klines([_row(i * 3600_000, 1000.0, 1000.0) for i in range(20)])
    assert abs(out["cvd_slope"] - 100.0) < 1e-6
    assert abs(out["cvd_slope_7d"] - 100.0) < 1e-6


def test_all_sell_slope_minus_100():
    out = cvd_slopes_from_klines([_row(i * 3600_000, 1000.0, 0.0) for i in range(20)])
    assert abs(out["cvd_slope"] + 100.0) < 1e-6


def test_balanced_zero():
    out = cvd_slopes_from_klines([_row(i * 3600_000, 1000.0, 500.0) for i in range(20)])
    assert abs(out["cvd_slope"]) < 1e-6 and abs(out["cvd"]) < 1e-6


def test_short_window_vs_7d_window_separation():
    rows = ([_row(i * 3600_000, 1000.0, 500.0) for i in range(8)]
            + [_row((8 + i) * 3600_000, 1000.0, 1000.0) for i in range(12)])
    out = cvd_slopes_from_klines(rows)
    assert abs(out["cvd_slope"] - 100.0) < 1e-6        # 短窗只看最後 12 根（全買）
    assert out["cvd_slope_7d"] < 100.0 - 1e-6          # 7d 窗被前段平衡拉低


def test_bad_bar_total_zero_skipped():
    rows = [_row(0, 0.0, 0.0)] + [_row(i * 3600_000, 1000.0, 1000.0) for i in range(1, 13)]
    out = cvd_slopes_from_klines(rows)
    assert abs(out["cvd_slope"] - 100.0) < 1e-6


def test_malformed_row_skipped():
    rows = [_row(1 * 3600_000, 1000.0, 1000.0), ["x", 0, 0], _row(2 * 3600_000, 1000.0, 1000.0)]
    out = cvd_slopes_from_klines(rows)
    assert abs(out["cvd_slope"] - 100.0) < 1e-6        # 壞列被 except 跳過、不崩


# ── cumsum 差分還原（時序相關性基礎）─────────────────────────────────────
def test_deltas_from_cumsum_roundtrip():
    out = cvd_slopes_from_klines([_row(i * 3600_000, 1000.0, 600.0 + i * 10) for i in range(10)])
    recon = _deltas_from_cumsum(out["series"])
    assert len(recon) == len(out["deltas"])
    for (t1, d1), (t2, d2) in zip(recon, out["deltas"]):
        assert t1 == t2 and abs(d1 - d2) < 1e-6


# ── Pearson / 跨單位對齊 ─────────────────────────────────────────────────
def test_pearson_same_opposite_constant():
    assert abs(_pearson([1, 2, 3, 4], [1, 2, 3, 4]) - 1.0) < 1e-9
    assert abs(_pearson([1, 2, 3, 4], [4, 3, 2, 1]) + 1.0) < 1e-9
    assert _pearson([1, 1, 1], [1, 2, 3]) is None      # 零變異 → None


def test_perbar_correlation_ms_s_alignment():
    h0 = 480000 * 3600        # 真實量級（>1e12 ms 才觸發毫秒偵測）
    h1 = h0 + 3600
    bn = [(h0 * 1000, 5.0), (h1 * 1000, -3.0)]   # ms
    cg = [(h0, 10.0), (h1, -6.0)]                # s（同兩根）
    r, n = _perbar_correlation(bn, cg)
    assert n == 2 and r is not None and abs(r - 1.0) < 1e-9


def test_sign_helper():
    assert _sign(5.0) == 1 and _sign(-5.0) == -1 and _sign(0.0) == 0


# ── 整批自測 smoke（與 --selftest 同一組不變量）──────────────────────────
def test_module_selftest_passes():
    assert _selftest() == 0
