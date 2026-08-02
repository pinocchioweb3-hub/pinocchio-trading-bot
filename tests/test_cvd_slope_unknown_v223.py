"""v223：CVD 斜率「一根有量的 bar 都沒有」不再折成 0.0（＝「多空均衡」）。

同物種第 43 次。這次落在**訊號數學的輸入端**：coinglass.py::get_cvd_series
在 delta_pcts 為空時回 `0.0`，而 0.0 在下游是一個有意義的答案（買賣力道相抵）。

為什麼要緊（實測的下游路徑，全部**已經**有正確守門、只是被 0.0 搶先堵死）：
  * l2_trigger/types.py:122 `is_stale()` 的規格就是「任一欄位 None 或列在
    stale_fields → True」。折成 0.0 ⇒ is_stale('cvd_slope') 為 False ⇒
    signals.py:45 的 STALE 早退**不會**觸發 ⇒ eval_cvd_divergence 對著虛構值
    給出正式判讀；cvd_slope_7d 同理讓 eval_cvd_silent_accumulation 給出
    「確認沒有默默吸籌」的判讀。⛔ 這兩支是訊號數學核心，本次一行都不碰——
    修法是把契約餵對，讓它們**既有的**守門自己生效。
  * chart_render.py:686-699（v222 才做的〔缺料〕分支）與 :382 的佐證框：
    來源永不回 None ⇒ 那兩個誠實分支是**到不了的死碼**，圖上照樣寫
    「多空均衡（斜率 +0.00）」。
  * wyckoff.py:94 `cvd_slope is not None and cvd_slope <= 0` ⇒ 0.0 讓它成立，
    等於用「沒量到」去確認一個空方脈絡。
  * get_structure:1256 → server.py snapshot → fire_queue → plan_snapshot 的
    cvd_slope_7d 學習欄位（與 v221 同一種傷害：餵優化器的欄位被寫成假答案）。

可達性（非假設）：delta_pcts 只在 `total > 0` 時才 append，而 :563-564 的
`_to_float(...) or 0.0` 會把**解析不出來的欄位**折成 0.0 ⇒ 上游一改欄位名/回
null，每一根都變成 total==0 ⇒ delta_pcts 全空 ⇒ 斜率 0.0，且 :572 的
「no usable taker volume rows」PARSE_ERROR 因為 series 恆非空而是**死碼**、
永遠不會回報。這正是 v220 OKX 新聞源那條（整條源 32h 沒送出過一則、日誌全程
無聲）的同一種下場。

邊界（兩側都釘住）：
  * 真的算出 0.0（有量的 bar、買賣剛好相抵）仍必須是 0.0——0.0 是答案，
    不是未知的代名詞。
  * 部分 bar 有量 ⇒ 用那些算，不因其他 bar 沒量就判未知。

執行：pytest tests/test_cvd_slope_unknown_v223.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from market_intel_mcp.sources.coinglass import CoinGlassSource


def _row(ts: int, buy, sell) -> dict:
    """一根 CoinGlass aggregated-taker-buy-sell-volume bar。buy/sell 傳 None
    ＝欄位存在但值為 null（上游降級最常見的形）。"""
    return {"time": ts,
            "aggregated_buy_volume_usd": buy,
            "aggregated_sell_volume_usd": sell}


def _call(rows: list[dict]) -> dict:
    """把 _get 換成回傳固定 rows 的假貨 → 全離線，不碰網路/不需 API key。"""
    src = CoinGlassSource.__new__(CoinGlassSource)   # 略過 __init__（要 key）

    async def _fake_get(path, params, tool=None, symbol=None):
        return {"data": rows}

    src._get = _fake_get                              # type: ignore[assignment]
    src._agg_symbol = lambda s: s                     # type: ignore[assignment]
    return asyncio.run(src.get_cvd_series("BTC", "1h", 168))


# ---------------------------------------------------------------- 正向側 ----
def test_a_all_rows_zero_volume_slope_is_unknown_not_zero():
    """A｜每一根都沒量（值為 0）⇒ 斜率是未知，不是「多空均衡」。"""
    r = _call([_row(i, 0.0, 0.0) for i in range(20)])
    assert r.get("error") is None, r
    assert r["cvd_slope"] is None, f"沒量到卻回 {r['cvd_slope']}＝宣稱多空均衡"
    assert r["cvd_slope_7d"] is None, r["cvd_slope_7d"]


def test_b_fields_null_slope_is_unknown_not_zero():
    """B｜欄位回 null（上游改欄位名/降級）⇒ 未知，不是均衡。

    這是最險的一條：`or 0.0` 讓每一根都變成「成交量為 0」，
    整條源壞掉的下場卻是一個看起來正常的讀數。"""
    r = _call([_row(i, None, None) for i in range(20)])
    assert r.get("error") is None, r
    assert r["cvd_slope"] is None, r["cvd_slope"]
    assert r["cvd_slope_7d"] is None, r["cvd_slope_7d"]


def test_c_field_renamed_slope_is_unknown():
    """C｜欄位整個改名（鍵不存在）⇒ 未知。"""
    rows = [{"time": i, "buy_vol": 100.0, "sell_vol": 50.0} for i in range(20)]
    r = _call(rows)
    assert r.get("error") is None, r
    assert r["cvd_slope"] is None, r["cvd_slope"]
    assert r["cvd_slope_7d"] is None, r["cvd_slope_7d"]


def test_d_unknown_slope_marks_is_stale_true():
    """D｜未知斜率必須讓 l2_trigger 既有的 is_stale() 判 True。

    這是本次修補的**目的地**：signals.py:45 靠 is_stale 早退成 STALE，
    而 is_stale 只認 None。⛔ 不碰 signals.py，只把契約餵對。"""
    from l2_trigger.types import MarketSnapshot

    r = _call([_row(i, None, None) for i in range(20)])
    snap = MarketSnapshot(symbol="BTC", ts=1, price=100.0,
                    cvd_slope=r["cvd_slope"], cvd_slope_7d=r["cvd_slope_7d"])
    assert snap.is_stale("cvd_slope") is True
    assert snap.is_stale("cvd_slope_7d") is True


def test_e_unknown_slope_wyckoff_does_not_warn_fake_breakout():
    """E｜未知斜率不得被 Wyckoff 當成「CVD 未同升」的確認。

    wyckoff.py:94 是 `cvd_slope is not None and cvd_slope <= 0`——折成 0.0 會
    讓它成立，於是一個放量突破（SOS）被掛上「⚠️ 疑似假突破」的警語，而那個
    警語的**唯一依據是一次根本沒發生的量測**。這比數字被折更糟：它是一句
    對盤面的斷言，且方向與真實盤面相反。"""
    from market_intel_mcp.wyckoff import classify_wyckoff

    r = _call([_row(i, None, None) for i in range(20)])
    assert r["cvd_slope"] is None

    # 前段低 → 箱體 → 最後三根放量收在箱頂之上 ⇒ bias=bull 且 SOS 成立，
    # 這正是 :93-95 那條 effort-vs-result 檢查唯一會跑到的路徑。
    candles = ([{"open": 90.0, "high": 92.0, "low": 88.0,
                 "close": 90.0, "volume": 1000.0} for _ in range(20)]
               + [{"open": 100.0, "high": 104.0, "low": 96.0,
                   "close": 100.0, "volume": 1000.0} for _ in range(35)]
               + [{"open": 105.0, "high": 112.0, "low": 104.0,
                   "close": 111.0, "volume": 5000.0} for _ in range(3)])

    faked = classify_wyckoff(candles, cvd_slope=0.0)
    unknown = classify_wyckoff(candles, cvd_slope=r["cvd_slope"])
    # 測試前提：這組 K 線確實走到那條分支（否則本測試等於沒驗到東西）
    assert faked.get("bias") == "bull" and faked.get("phase") == "Phase D/E", faked
    assert "疑似假突破" in faked["narrative"], faked["narrative"]
    # 本體：沒量到 ⇒ 不得生出那句警語
    assert "疑似假突破" not in unknown["narrative"], unknown["narrative"]


def test_f_unknown_slope_regime_bucket_is_none():
    """F｜復盤分桶不得把未知記成 'flat'（＝一個 regime 觀測值）。"""
    from l3_dispatcher.regime_vector import classify_cvd_state

    r = _call([_row(i, None, None) for i in range(20)])
    assert classify_cvd_state(r["cvd_slope"], "none") is None
    assert classify_cvd_state(0.0, "none") == "flat"   # 對照組：真的 0 是答案


def test_g_structure_propagates_unknown_not_zero():
    """G｜get_structure 的 cvd_slope_7d（餵 plan_snapshot 學習欄位）同樣是未知。"""
    src = CoinGlassSource.__new__(CoinGlassSource)
    cvd_r = _call([_row(i, None, None) for i in range(20)])
    assert cvd_r["cvd_slope_7d"] is None
    # get_structure:1256 就是 out['cvd_slope_7d'] = cvd_r.get('cvd_slope_7d')
    out = {"cvd_slope_7d": None}
    if isinstance(cvd_r, dict) and not cvd_r.get("error"):
        out["cvd_slope_7d"] = cvd_r.get("cvd_slope_7d")
    assert out["cvd_slope_7d"] is None, out


def test_h_slope_bars_provenance_exposed():
    """H｜留痕：算斜率用了幾根 bar 要看得見（信心度，非只有值）。"""
    r_none = _call([_row(i, None, None) for i in range(20)])
    r_ok = _call([_row(i, 100.0, 50.0) for i in range(20)])
    assert r_none["slope_bars"] == 0, r_none.get("slope_bars")
    assert r_ok["slope_bars"] == 20, r_ok.get("slope_bars")


# ---------------------------------------------------------------- 反向側 ----
# 以下四條在**改動前的 HEAD 上就必須是綠的**——它們釘住「算出來了就是答案」，
# 防止這次修補把真實讀數也一起判成未知。
def test_i_real_zero_slope_stays_zero():
    """I｜有量、買賣剛好相抵 ⇒ 斜率就是 0.0，不得判未知。"""
    r = _call([_row(i, 100.0, 100.0) for i in range(20)])
    assert r["cvd_slope"] == 0.0, r["cvd_slope"]
    assert r["cvd_slope_7d"] == 0.0, r["cvd_slope_7d"]
    assert r["cvd_slope"] is not None


def test_j_partial_volume_uses_the_bars_that_have_it():
    """J｜只有部分 bar 有量 ⇒ 用那幾根算，不因其他根沒量就判未知。"""
    rows = [_row(i, None, None) for i in range(10)]
    rows += [_row(10 + i, 150.0, 50.0) for i in range(5)]   # delta_pct = +50
    r = _call(rows)
    assert r["cvd_slope"] == 50.0, r["cvd_slope"]
    assert r["slope_bars"] == 5, r.get("slope_bars")


def test_k_all_buy_slope_is_plus_100():
    """K｜全主動買進 ⇒ +100（既有語意不得被改動）。"""
    r = _call([_row(i, 100.0, 0.0) for i in range(20)])
    assert abs(r["cvd_slope"] - 100.0) < 1e-9, r["cvd_slope"]


def test_l_series_still_returned_when_slope_unknown():
    """L｜斜率未知時**曲線仍要回**——v222 圖上的警語只否認斜率判讀，
    不可退化成「整個面板沒資料」。"""
    r = _call([_row(i, None, None) for i in range(20)])
    assert r.get("error") is None
    assert isinstance(r.get("series"), list) and len(r["series"]) == 20
    assert r["cvd_slope"] is None


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
