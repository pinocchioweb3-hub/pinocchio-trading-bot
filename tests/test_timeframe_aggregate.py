"""#34 補層：timeframe_aggregate 純函式測試（8H/5D 由低框合併）。離線、零網路。"""
import pytest

from market_intel_mcp.timeframe_aggregate import (
    aggregate_by_factor,
    to_8h,
    to_5d,
    fill_missing_tfs,
    _infer_base_ms,
)

_4H_MS = 4 * 3600 * 1000
_1D_MS = 24 * 3600 * 1000


def _mk(n, base_ms, start_ts=0, vol=10.0):
    """造 n 根升序 candle，等距 base_ms。價走 i 線性，便於斷言。"""
    return [
        {
            "ts": start_ts + i * base_ms,
            "open": float(i),
            "high": float(i) + 1.0,
            "low": float(i) - 1.0,
            "close": float(i) + 0.5,
            "volume": vol,
            "volume_usd": vol * 100,
            "confirm": True,
        }
        for i in range(n)
    ]


def test_factor2_count_and_ohlc():
    c = _mk(6, _4H_MS)
    agg = aggregate_by_factor(c, 2)
    assert len(agg) == 3
    # 第一根 = 合併 i=0,1：open=首(0)、close=末(1.5)、high=max(2)、low=min(-1)
    assert agg[0]["open"] == 0.0
    assert agg[0]["close"] == 1.5
    assert agg[0]["high"] == 2.0
    assert agg[0]["low"] == -1.0


def test_volume_summed():
    c = _mk(4, _4H_MS, vol=10.0)
    agg = aggregate_by_factor(c, 2)
    assert agg[0]["volume"] == 20.0
    assert agg[0]["volume_usd"] == 2000.0


def test_factor5_for_5d():
    c = _mk(10, _1D_MS)
    agg = aggregate_by_factor(c, 5)
    assert len(agg) == 2
    assert agg[0]["open"] == 0.0
    assert agg[0]["close"] == 4.5   # i=0..4 末根 close=4.5
    assert agg[1]["open"] == 5.0


def test_partial_last_bucket_kept():
    # 5 根、factor 2 → 桶 [0,1],[2,3],[4] → 3 根（末桶只有 1 根）
    c = _mk(5, _4H_MS)
    agg = aggregate_by_factor(c, 2)
    assert len(agg) == 3
    assert agg[-1]["open"] == 4.0
    assert agg[-1]["close"] == 4.5


def test_factor_le_1_returns_copy():
    c = _mk(4, _4H_MS)
    out = aggregate_by_factor(c, 1)
    assert out == c
    assert out is not c          # 淺拷貝 list、非同一物件
    out.append({"ts": 999})      # 改返回 list 的長度
    assert len(c) == 4           # 不影響原 list 結構（淺拷貝語意）


def test_empty_and_none():
    assert aggregate_by_factor([], 2) == []
    assert aggregate_by_factor(None, 2) == []


def test_single_candle_returns_as_is():
    c = _mk(1, _4H_MS)
    assert aggregate_by_factor(c, 2) == c


def test_out_of_order_input_sorted():
    c = _mk(4, _4H_MS)
    shuffled = [c[2], c[0], c[3], c[1]]
    agg = aggregate_by_factor(shuffled, 2)
    assert len(agg) == 2
    assert agg[0]["ts"] < agg[1]["ts"]
    assert agg[0]["open"] == 0.0    # 仍以真實最舊根為首


def test_epoch_bucket_alignment_8h():
    # 2024-01-01 00:00 UTC = 1704067200000 ms，恰為 8H epoch 桶邊界
    t0 = 1704067200000
    c = [
        {"ts": t0 + 0 * _4H_MS, "open": 1, "high": 2, "low": 0, "close": 1.5},  # 00:00
        {"ts": t0 + 1 * _4H_MS, "open": 1.5, "high": 3, "low": 1, "close": 2},  # 04:00
        {"ts": t0 + 2 * _4H_MS, "open": 2, "high": 4, "low": 1.5, "close": 3},  # 08:00
        {"ts": t0 + 3 * _4H_MS, "open": 3, "high": 5, "low": 2, "close": 4},    # 12:00
    ]
    agg = aggregate_by_factor(c, 2)
    assert len(agg) == 2
    # 第一桶 = 00:00+04:00（落 UTC 00–08）；ts 為桶內首根
    assert agg[0]["ts"] == t0
    assert agg[0]["high"] == 3      # max(2,3)
    assert agg[1]["ts"] == t0 + 2 * _4H_MS


def test_confirm_passthrough_last():
    c = _mk(4, _4H_MS)
    c[1]["confirm"] = False  # 桶內末根
    agg = aggregate_by_factor(c, 2)
    assert agg[0]["confirm"] is False


def test_to_8h_to_5d_wrappers():
    assert len(to_8h(_mk(6, _4H_MS))) == 3
    assert len(to_5d(_mk(10, _1D_MS))) == 2


def test_infer_base_ms_median():
    c = _mk(5, _4H_MS)
    assert _infer_base_ms(c) == _4H_MS
    assert _infer_base_ms([]) is None
    assert _infer_base_ms(_mk(1, _4H_MS)) is None


def test_fill_missing_adds_8h_and_5d():
    by_tf = {"4h": _mk(6, _4H_MS), "1d": _mk(10, _1D_MS)}
    out = fill_missing_tfs(by_tf)
    assert "8h" in out and len(out["8h"]) == 3
    assert "5d" in out and len(out["5d"]) == 2
    # 不改輸入
    assert "8h" not in by_tf and "5d" not in by_tf


def test_fill_missing_does_not_overwrite_existing():
    existing_8h = _mk(2, 8 * 3600 * 1000)
    by_tf = {"4h": _mk(6, _4H_MS), "8h": existing_8h}
    out = fill_missing_tfs(by_tf)
    assert out["8h"] is existing_8h   # 既有 8h 不被覆蓋


def test_fill_missing_skips_when_insufficient():
    # 4h 只有 1 根（<2）→ 不補 8h；無 1d → 不補 5d
    out = fill_missing_tfs({"4h": _mk(1, _4H_MS)})
    assert "8h" not in out
    assert "5d" not in out


def test_fill_missing_tolerates_wrapped_and_error_forms():
    by_tf = {
        "4h": {"candles": _mk(6, _4H_MS)},
        "1d": {"error": "boom"},        # error 形態 → 不補 5d
    }
    out = fill_missing_tfs(by_tf)
    assert "8h" in out and len(out["8h"]) == 3
    assert "5d" not in out


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
