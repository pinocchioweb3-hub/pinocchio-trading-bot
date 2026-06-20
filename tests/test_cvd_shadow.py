"""task#20 第二步 影子接線：cvd_shadow 記錄併記純函式 契約測試（離線、零網路、零訊號變更）。

鎖住影子 worker 的「資料併記」不變量，避免日後改動悄悄：
  1. 把 Binance 自算 CVD 寫回了 universe/strength（影子鐵則禁止——這裡只驗 _build_item 不混欄）。
  2. Binance 缺料時捏造數字（紅線③——必須誠實 None / binance_ok=False）。
  3. 夾帶非白名單因子鍵進 sink。

全離線：不 import daemon 來源、不載 .env、不觸網（只測純函式 _build_item / _selftest）。
"""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from l3_dispatcher.cvd_shadow import _build_item, _selftest, _FACTOR_KEYS


_FACTORS = {
    "return_7d_pct": 5.0, "vol_24h_usd": 1e9, "vol_24h_vs_30d": 1.2,
    "oi_delta_7d_pct": 3.0, "cvd_slope_7d": 0.0, "top_trader_dev": 0.05,
    "btc_corr_30d": 0.7, "funding": 0.0001,
}
_CVD = {"cvd": 1234.5, "cvd_slope": 8.9, "cvd_slope_7d": 12.34,
        "series": [{"ts": i, "value": float(i)} for i in range(168)], "deltas": []}


def test_build_item_keeps_stub_and_binance_side_by_side():
    item = _build_item("BTC", _FACTORS, _CVD)
    # universe 路徑的 0.0 缺口被忠實保留（不被 Binance 值覆寫）
    assert item["stub_cvd_slope_7d"] == 0.0
    assert item["factors_live"]["cvd_slope_7d"] == 0.0
    # 該補的 Binance 值並排記錄（但活在獨立鍵，從不回寫 factors_live）
    assert item["binance_cvd_slope_7d"] == 12.34
    assert item["binance_cvd_slope"] == 8.9
    assert item["binance_cvd"] == 1234.5
    assert item["binance_bars"] == 168
    assert item["binance_ok"] is True


def test_build_item_never_overwrites_live_factor_with_binance():
    # 影子鐵則：binance 值絕不汙染 strength 排名器吃的 factors_live
    item = _build_item("ETH", _FACTORS, _CVD)
    assert item["factors_live"]["cvd_slope_7d"] != item["binance_cvd_slope_7d"]
    assert item["factors_live"]["cvd_slope_7d"] == 0.0


def test_build_item_honest_none_when_binance_missing():
    item = _build_item("SOL", _FACTORS, None)
    assert item["binance_cvd_slope_7d"] is None
    assert item["binance_cvd_slope"] is None
    assert item["binance_cvd"] is None
    assert item["binance_bars"] == 0
    assert item["binance_ok"] is False
    # 因子仍完整保留（缺的只有 Binance 那半）
    assert item["factors_live"]["return_7d_pct"] == 5.0


def test_build_item_factor_key_whitelist():
    noisy = dict(_FACTORS, secret="x", _internal=1, password="leak")
    item = _build_item("DOGE", noisy, _CVD)
    assert set(item["factors_live"].keys()) == set(_FACTOR_KEYS)
    assert "secret" not in item["factors_live"]
    assert "password" not in item["factors_live"]


def test_build_item_missing_factor_becomes_none():
    # 因子 dict 缺某鍵 → 該鍵記 None（不 KeyError、不捏造）
    partial = {"return_7d_pct": 1.0}
    item = _build_item("APT", partial, None)
    assert item["factors_live"]["vol_24h_usd"] is None
    assert item["factors_live"]["cvd_slope_7d"] is None
    assert item["stub_cvd_slope_7d"] is None


def test_module_selftest_passes():
    assert _selftest() == 0
