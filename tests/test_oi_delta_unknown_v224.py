"""v224：OI 24h 變化率「這輪沒算出來」不再折成 0.0（＝「OI 24h 完全沒變」）。

同物種第 44 次。落點：market_intel_mcp/sources/coinglass.py::get_oi

    delta_pct_24h = ((latest - first) / first * 100) if first else 0.0

`if first else 0.0` 把兩件相反的事寫成同一個值：
  * 首根 OI 為 0（新上市／極冷門幣／上游該欄位回 0 或 null——同支檔案的
    `_to_float()` 回 None 時 series 不 append，但**回 0.0** 的欄位會照樣進
    series）⇒ 分母為 0，根本算不出變化率 ⇒ 卻回 0.0；
  * 序列只有一根（limit=1、上游只給一根、或該幣剛開盤）⇒ latest 即 first ⇒
    數學上必得 0.0，但那不是「24h 沒變」，是**沒有 24h 這個窗**。

而 0.0 在下游是一個有意義的答案：「OI 24h 完全沒變」。

同一支檔案的反例（證明這是疏漏、不是風格）：
  * get_cvd_series（v223 已治）現在缺料回 None。
  * 免費源 sources/binance_perp.py::get_oi:185-187 一直都是誠實的：
    `if len(series) >= 2 and series[0]["value"]:` 否則 delta 留 None。
    ⇒ 同一個欄位在兩個來源之間語意不一致：Binance 說「不知道」，CoinGlass 說
    「沒變」。snapshot 走哪一條全看誰活著（server.py:391 主源／:245 備援補值）。

下游（全部**已經**有正確守門，只是被 0.0 搶先堵死）：
  * l2_trigger/signals.py:115 `if s.is_stale("oi_delta_pct")` → STALE 早退。
    is_stale 的規格是「任一欄位 None 或列在 stale_fields → True」
    （l2_trigger/types.py:122）⇒ 折成 0.0 ⇒ 早退永不觸發 ⇒
    eval_oi_trajectory 拿虛構的 0.0 去比 c.oi_rise_min_pct，engine.py:96 據此
    判 fuel 不足並寫下 `oi_fuel_insufficient(delta=0.00%)`＝一個沒發生過的量測
    寫進 HOLD 理由。
  * l3_dispatcher/regime_vector.py:86 `if oi_delta_pct is None → 不分桶`
    （v91 定案的誠實死區）⇒ 0.0 會被分進象限，餵進復盤學習欄位。
  * market_intel_mcp/wyckoff.py:94 `oi_delta_pct is not None and <= 0`
    ⇒ 0.0 成立 ⇒ 用「沒量到」去確認一個「無增量資金跟進」的空方脈絡。
  * market_intel_mcp/timeframe_nesting.py:413 同形（`< 0` 故 0.0 不觸發，
    但它旁邊的 is not None 守門一樣被架空成無效檢查）。

圖上（本次一併治，因為它是**本次改動才變得可達**的）：
  l3_dispatcher/chart_render.py:710 `oi_color = OICOL if (d24 or 0) >= 0 else DOWN`
  ——顏色在宣稱方向。d24 為 None 時 `or 0` 讓它一律染成「OI 上升」藍。
  在改動前這條到不了（來源永不回 None）；把來源改誠實之後，
  out["oi"] 與 out["oi_delta_24h"] 在 chart_render.py:871-873 是同一個 if 區塊
  設值 ⇒ 面板照畫、d24 卻是 None ⇒ **顏色開始說謊**。同一段 :715 的文字側
  早就有 `if d24 is not None` 守門，卻是靜靜不寫（讀者只看到一條藍線）。

邊界（兩側都釘住）：
  * 真的算出 0.0（首根非 0、確實兩根以上、OI 24h 剛好持平）仍必須是 0.0——
    0.0 是答案，不是未知的代名詞。
  * 變化率未知不得退化成「整個 OI 面板沒資料」：series/latest 照回、面板照畫。

執行：pytest tests/test_oi_delta_unknown_v224.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from market_intel_mcp.sources.coinglass import CoinGlassSource  # noqa: E402


def _row(ts: int, oi) -> dict:
    """一根 CoinGlass open-interest/aggregated-history bar（close = 當期 OI USD）。"""
    return {"time": ts, "close": oi}


def _call(rows: list[dict]) -> dict:
    """把 _get 換成回傳固定 rows 的假貨 → 全離線，不碰網路/不需 API key。"""
    src = CoinGlassSource.__new__(CoinGlassSource)   # 略過 __init__（要 key）

    async def _fake_get(path, params, tool=None, symbol=None):
        return {"data": rows}

    src._get = _fake_get                              # type: ignore[assignment]
    src._agg_symbol = lambda s: s                     # type: ignore[assignment]
    return asyncio.run(src.get_oi("BTC", "1h", 24))


# ============================ 正向側：未知不得寫成 0 ==========================

def test_a_first_bar_zero_delta_is_unknown_not_zero():
    """A｜首根 OI 為 0（新上市／極冷門幣）⇒ 分母為 0，變化率算不出來。

    舊碼：`if first else 0.0` ⇒ 回 0.0＝宣稱「OI 24h 完全沒變」。
    """
    rows = [_row(0, 0.0)] + [_row(i, 1_000_000.0 * i) for i in range(1, 24)]
    r = _call(rows)
    assert r.get("error") is None, r
    assert r["delta_pct_24h"] is None, (
        f"首根為 0（算不出變化率）卻回 {r['delta_pct_24h']}＝宣稱 OI 24h 沒變")


def test_b_single_bar_delta_is_unknown_not_zero():
    """B｜序列只有一根 ⇒ 沒有 24h 這個窗，不是「24h 沒變」。

    舊碼：latest 即 first ⇒ (x-x)/x*100 == 0.0，數學上必得 0，語意上是假答案。
    免費源 binance_perp.get_oi:185 一直都是 `len(series) >= 2` 才給值。
    """
    r = _call([_row(0, 5_000_000.0)])
    assert r.get("error") is None, r
    assert r["delta_pct_24h"] is None, (
        f"只有一根卻回 {r['delta_pct_24h']}＝把「沒有窗」寫成「窗內沒變」")


def test_c_all_bars_zero_delta_is_unknown():
    """C｜每一根都是 0（上游該欄位整條降級成 0）⇒ 未知，不是持平。"""
    r = _call([_row(i, 0.0) for i in range(24)])
    assert r.get("error") is None, r
    assert r["delta_pct_24h"] is None, r["delta_pct_24h"]


def test_d_provenance_bar_count_is_recorded():
    """D｜留痕：用幾根算的（＝信心度，非只有值）。與 v223 的 slope_bars 同形。"""
    rows = [_row(i, 1_000_000.0 + i) for i in range(24)]
    r = _call(rows)
    assert r.get("delta_bars") == 24, r.get("delta_bars")


def test_e_unknown_delta_still_reports_bar_count():
    """E｜未知時也要留痕（1 根＝為什麼算不出來，讀者不必回頭猜）。"""
    r = _call([_row(0, 5_000_000.0)])
    assert r["delta_pct_24h"] is None
    assert r.get("delta_bars") == 1, r.get("delta_bars")


# ============================ 反向側：0.0 仍是答案 ============================

def test_f_genuine_flat_oi_is_still_zero():
    """F｜⛔ 邊界：首根非 0、兩根以上、OI 剛好持平 ⇒ 必須維持 0.0。

    0.0 是答案，不是未知的代名詞。這條在改動前的 HEAD 上就該是綠的。
    """
    r = _call([_row(i, 7_000_000.0) for i in range(24)])
    assert r.get("error") is None, r
    assert r["delta_pct_24h"] == 0.0, r["delta_pct_24h"]


def test_g_normal_series_value_unchanged():
    """G｜⛔ 邊界：正常序列的算法一行都不許變（+50%）。"""
    r = _call([_row(0, 1_000_000.0), _row(1, 1_200_000.0), _row(2, 1_500_000.0)])
    assert r["delta_pct_24h"] == pytest.approx(50.0), r["delta_pct_24h"]
    assert r["latest"] == pytest.approx(1_500_000.0)


def test_h_unknown_delta_does_not_kill_the_series():
    """H｜⛔ 邊界：變化率未知不得退化成「整個 OI 沒資料」。

    latest/series 是另一個獨立量測，照回。（v222 學到的教訓：警語只否認
    那一格判讀，不可把整個面板抹掉。）
    """
    r = _call([_row(0, 0.0), _row(1, 900.0), _row(2, 1_100.0)])
    assert r["delta_pct_24h"] is None
    assert r["latest"] == pytest.approx(1_100.0)
    assert len(r["series"]) == 3


def test_i_error_path_untouched():
    """I｜⛔ 邊界：真的沒資料時仍回 error（未知≠空序列，兩者的出口不同）。"""
    r = _call([])
    assert r.get("error"), r


# ==================== 下游：既有的 STALE 守門這下才生效 ======================

def test_j_none_activates_the_existing_l2_stale_guard():
    """J｜本次修法的目的：把契約餵對，讓 l2_trigger 既有的守門自己生效。

    ⛔ 一行都不碰 signals.py／strength.py。這條在 HEAD 上就是綠的——
    守門一直都在，只是上游永不回 None ⇒ 它是死碼。
    """
    from l2_trigger.signals import eval_oi_trajectory
    from l2_trigger.types import MarketSnapshot, SignalState, TriggerConfig

    cfg = TriggerConfig(setup_name="test")
    s = MarketSnapshot(symbol="X", ts=1, price=1.0, oi_delta_pct=None)
    r = eval_oi_trajectory(s, cfg)
    assert r.state == SignalState.STALE, r
    # 對照：0.0 會被當成一個真實讀數拿去比門檻（＝舊碼下場）
    s0 = MarketSnapshot(symbol="X", ts=1, price=1.0, oi_delta_pct=0.0)
    r0 = eval_oi_trajectory(s0, cfg)
    assert r0.state != SignalState.STALE, "0.0 是答案，不該被當未知"


# ============ 圖上：本次改動才變得可達的「顏色在宣稱方向」 ==================

matplotlib = pytest.importorskip("matplotlib")
from matplotlib.axes import Axes  # noqa: E402

from l3_dispatcher import chart_render  # noqa: E402


def _candles(n: int = 60) -> list[dict]:
    out = []
    px = 100.0
    for i in range(n):
        px += 1.2 if (i // 5) % 2 == 0 else -0.9
        o = px
        c = px + (0.6 if i % 2 == 0 else -0.6)
        out.append({"ts": 1700000000000 + i * 14400000,
                    "open": o, "close": c,
                    "high": max(o, c) + 0.5, "low": min(o, c) - 0.5,
                    "volume": 1000.0 + i * 7})
    return out


def _full_smc() -> dict:
    return {"current_price": 100.0, "candle_count": 60,
            "swing_points": [], "order_blocks": [], "fvg": [],
            "bos_choch": [], "liquidity": []}


def _render(monkeypatch, d24):
    """真的跑一次 render，側錄 OI 面板的線色與圖上文字。"""
    overlays = {"oi": [1_000_000.0 + i * 10 for i in range(60)],
                "oi_delta_24h": d24}
    # (axes, color) 逐筆記；ylabel 在 plot **之後**才設，故渲染完再依 axes 過濾。
    drawn: list[tuple] = []
    texts: list[str] = []
    orig_plot, orig_text = Axes.plot, Axes.text

    def plot_spy(self, *a, **k):
        drawn.append((self, k.get("color")))
        return orig_plot(self, *a, **k)

    def text_spy(self, x, y, s="", *a, **k):
        texts.append(str(s))
        return orig_text(self, x, y, s, *a, **k)

    monkeypatch.setattr(Axes, "plot", plot_spy)
    monkeypatch.setattr(Axes, "text", text_spy)
    out = chart_render.render_smc_chart("TESTCOIN", _candles(), _full_smc(),
                                        tf="4h", overlays=overlays)
    assert out is not None, "render 本身不該失敗（本測試測誠實度，不是可用性）"
    colors = [c for ax, c in drawn if ax.get_ylabel() == "OI"]
    assert colors, "OI 面板沒被畫出來＝這條測試本身失效（不可讀成通過）"
    try:
        out.unlink()
    except OSError:
        pass
    return colors, texts


def test_k_unknown_delta_does_not_paint_the_rising_color(monkeypatch):
    """K｜d24 未知 ⇒ 線色不得是「OI 上升」藍，也不得是「下降」紅。

    舊碼 `(d24 or 0) >= 0` ⇒ None 一律染成上升色＝顏色在宣稱方向。
    """
    colors, _ = _render(monkeypatch, None)
    assert chart_render.OICOL not in colors, (
        "OI 24h 沒算出來卻把線畫成『上升』色＝顏色在替讀者下結論")
    assert chart_render.DOWN not in colors, colors


def test_l_unknown_delta_says_so_on_the_chart(monkeypatch):
    """L｜d24 未知 ⇒ 圖上要說「沒算出來」，不是靜靜不寫。

    舊碼 :715 `if d24 is not None` 是靜默略過 ⇒ 讀者只看到一條無標註的線，
    分不出「這輪沒算」與「這格本來就不重要」。
    """
    _, texts = _render(monkeypatch, None)
    joined = "\n".join(texts)
    assert chart_render._SC_MISSING in joined and "OI 24h" in joined, joined


def test_m_rising_and_falling_colors_unchanged(monkeypatch):
    """M｜⛔ 邊界：有值時的顏色語意一行不變（HEAD 上就綠）。"""
    up_colors, _ = _render(monkeypatch, 3.2)
    assert chart_render.OICOL in up_colors, up_colors
    down_colors, _ = _render(monkeypatch, -3.2)
    assert chart_render.DOWN in down_colors, down_colors


def test_n_genuine_zero_still_reads_as_rising_side(monkeypatch):
    """N｜⛔ 邊界：真的 0.0（持平）仍走 >= 0 那側、且數字照印。"""
    colors, texts = _render(monkeypatch, 0.0)
    assert chart_render.OICOL in colors, colors
    joined = "\n".join(texts)
    assert "OI 24h +0.0%" in joined, joined
