"""v222：圖上 CoinGlass 佐證框的「這輪沒算出來」不再被寫成數字 0。

同物種第 42 次（v208/v210/v212–v221 一脈），**第二次落在使用者眼睛直接看的那張圖上**
（v219 是圖上的 SMC 圖層，本輪是圖上右上角的 CoinGlass 結構評分卡）。

落點 l3_dispatcher/chart_render.py：
    :348 _structure_scorecard_lines()
        :356  f"{(st.get('vol_24h_vs_30d') or 0):.2f}"      → 缺料寫成「量比 0.00」
        :360  f"{(cvs or 0):+.2f}　…{(tts or 0):+.2f}"       → 缺料寫成「斜率 +0.00」
        :354  量比只在 atr_pct_7d 有值時才印                  → ATR 缺料時量比整格消失
    :657 slope = overlays.get("cvd_slope") or 0             → 缺料寫成「多空均衡（+0.00）」

⇒ 折出來的不是一個中性的佔位符，是一個**有意義的讀數**：
   「量比 0.00」＝這 24h 成交量塌到零（極端訊號）；
   「大戶斜率 +0.00」＝大戶部位一動也沒動（＝明確的中性判讀）；
   「CVD 多空均衡」＝買賣力道相抵（SMC 真假突破的核心判準之一）。
   看圖的人分不出這是「量出來的中性」還是「根本沒量到」。

【可達性（實證，非假設）】上游 market_intel_mcp/sources/coinglass.py::get_structure
本身是**逐分量誠實**的：:1194-1198 先把 7 個欄位全設 None，再由 4 條獨立子請求
（price / oi / positioning / cvd）各自填得出來的那幾格——任一條掛掉、或
bars 不足門檻（vols<30 → vol_24h_vs_30d 留 None；<200 根 4h → above_4h_200ma 留
None；tseries<6 → top_trader_slope_7d 留 None），該格就保持 None。**部分缺格是
常態不是邊角**。而 chart_render.py:858 用 `{k: struct.get(k) for k in (...)}` 重組，
把 None 抹平成「鍵在、值是 None」的形狀（與 v220 在 smc_walkforward.py:169 修掉的
同一個抹平動作），佐證框再把它折成 0。

⛔ 邊界線（反向側，本來就該綠）：
   - 真的量出 0.0 仍要照印 0.00／+0.00——0.0 是答案，不是未知的代名詞。
   - 整份 structure 一格都沒有時，維持 v183 的處置＝整個框不畫（框不出現不對盤面
     做任何斷言，傷害小一級；而畫一個全是〔缺料〕的框＝天天噪音）。
   - basis／sentiment 兩區塊上游本來就有 `is not None` 守門，不得連坐。
"""
import pytest

matplotlib = pytest.importorskip("matplotlib")
from matplotlib.axes import Axes  # noqa: E402

from l3_dispatcher import chart_render  # noqa: E402
from l3_dispatcher.chart_render import _structure_scorecard_lines  # noqa: E402

MARK = "缺料"

# 7 格全滿的 structure（上游 get_structure 成功且每條子請求都活著時的形狀）
FULL_ST = {
    "atr_pct_7d": 2.8,
    "vol_24h_vs_30d": 0.62,
    "cvd_slope_7d": 0.18,
    "top_trader_slope_7d": 0.012,
    "oi_delta_7d_pct": 3.1,
    "higher_lows_7d": True,
    "above_4h_200ma": False,
}


def _st(**over) -> dict:
    d = dict(FULL_ST)
    d.update(over)
    return {"structure": d}


def _joined(overlays: dict) -> str:
    return "\n".join(_structure_scorecard_lines(overlays))


def _candles(n: int = 60) -> list[dict]:
    out = []
    px = 100.0
    for i in range(n):
        px += 1.2 if (i // 5) % 2 == 0 else -0.9
        o = px
        c = px + (0.6 if i % 2 == 0 else -0.6)
        out.append({
            "ts": 1700000000000 + i * 14400000,
            "open": o, "close": c,
            "high": max(o, c) + 0.5, "low": min(o, c) - 0.5,
            "volume": 1000.0 + i * 7,
        })
    return out


def _full_smc() -> dict:
    """六個分量都算過——避免 v219 的 SMC 缺料警語混進本測試的比對。"""
    return {"current_price": 100.0, "candle_count": 60,
            "swing_points": [], "order_blocks": [], "fvg": [],
            "bos_choch": [], "liquidity": []}


def _render_texts(monkeypatch, overlays: dict) -> list[str]:
    """真的跑一次 render，側錄圖上所有文字——證明修補走到畫面上，不只是純函式。"""
    seen: list[str] = []
    orig = Axes.text

    def spy(self, x, y, s="", *a, **k):
        seen.append(str(s))
        return orig(self, x, y, s, *a, **k)

    monkeypatch.setattr(Axes, "text", spy)
    out = chart_render.render_smc_chart("TESTCOIN", _candles(), _full_smc(),
                                       tf="4h", overlays=overlays)
    assert out is not None, "render 本身不該失敗（本測試測誠實度，不是可用性）"
    try:
        out.unlink()
    except OSError:
        pass
    return seen


# ---------- 正向側：沒算出來的那一格，圖上不得寫成 0 ----------

def test_a_missing_vol_ratio_not_written_as_zero():
    """(A) 量比沒算出來（vols<30 根就會走到這條路）。
    舊碼：「ATR% 2.8　量比 0.00」＝宣稱 24h 量塌到零。"""
    txt = _joined(_st(vol_24h_vs_30d=None))
    assert "量比 0.00" not in txt, "量比沒算出來卻寫成 0.00＝一個極端讀數的假答案"
    assert MARK in txt, "量比這格沒算出來，框上卻沒標"
    assert "ATR% 2.8" in txt, "算出來的那一格不得被連坐抹掉"


def test_b_missing_top_trader_slope_not_written_as_zero():
    """(B) 大戶斜率沒算出來（positioning 子請求掛掉、或 tseries<6）。
    舊碼：「CVD斜率 +0.18　大戶斜率 +0.00」＝宣稱大戶部位一動也沒動。"""
    txt = _joined(_st(top_trader_slope_7d=None))
    assert "大戶斜率 +0.00" not in txt, "大戶斜率沒算出來卻寫成 +0.00＝明確的中性判讀"
    assert MARK in txt
    assert "+0.18" in txt, "CVD 斜率算出來了，不得被連坐"


def test_c_missing_cvd_slope_7d_not_written_as_zero():
    """(C) 反向：CVD 斜率缺、大戶斜率在。舊碼把缺的那邊寫成 +0.00。"""
    txt = _joined(_st(cvd_slope_7d=None))
    assert "CVD斜率 +0.00" not in txt
    assert MARK in txt
    assert "+0.01" in txt, "大戶斜率 0.012 算出來了，不得被連坐"


def test_d_vol_ratio_survives_when_atr_missing():
    """(D) 舊碼量比巢狀在 `if atr_pct_7d is not None` 底下 ⇒ ATR 缺料時，
    **算出來的量比整格消失**（不是折成 0，是被沒關係的另一格連坐吃掉）。"""
    txt = _joined(_st(atr_pct_7d=None))
    assert "0.62" in txt, "量比算出來了，卻因為 ATR 缺料而整格消失"
    assert MARK in txt, "ATR 這格沒算出來，框上卻沒標"


def test_e_cvd_panel_slope_unknown_is_not_balanced(monkeypatch):
    """(E) CVD 面板：序列有、斜率沒算出來（cvd 回了 series 但沒給 cvd_slope）。
    舊碼：「CVD 主動買賣淨力：多空均衡（斜率 +0.00）」＝把未知講成一個判讀。
    ⚠️ 曲線本身是真資料，警語只能否認**斜率判讀**，不得說整個面板沒資料。"""
    overlays = {"cvd": [float(i) for i in range(60)], "cvd_slope": None}
    seen = _render_texts(monkeypatch, overlays)
    cvd_lines = [t for t in seen if "CVD" in t]
    assert cvd_lines, "CVD 面板應該有一行說明"
    joined = "\n".join(cvd_lines)
    assert "多空均衡" not in joined, "斜率沒算出來卻判「多空均衡」"
    assert "+0.00" not in joined, "斜率沒算出來卻寫成 +0.00"
    assert MARK in joined


def test_f_scorecard_reaches_the_chart(monkeypatch):
    """(F) 缺料標記必須真的畫到圖上（不只是純函式回字串）。"""
    seen = _render_texts(monkeypatch, _st(vol_24h_vs_30d=None,
                                         top_trader_slope_7d=None))
    box = [t for t in seen if "結構評分" in t]
    assert box, "結構評分框沒畫出來"
    assert MARK in box[0], "框畫了，但缺料那兩格仍是 0"
    assert "0.00" not in box[0].split("量比")[-1].split("\n")[0]


# ---------- 反向側：算出來就是答案（本來就該綠） ----------

def test_g_all_present_stays_silent():
    """7 格全滿 ⇒ 框上逐字不得多出任何缺料字樣（誤報＝慢性假警報）。"""
    txt = _joined(_st())
    assert MARK not in txt
    assert "ATR% 2.8" in txt and "0.62" in txt


def test_h_genuine_zero_is_still_printed():
    """⛔ 守門線：真的量出 0.0 仍要照印——0.0 是答案，不是未知的代名詞。
    大戶斜率真的持平（+0.00）是常見盤況，天天會走到這條路徑。"""
    txt = _joined(_st(top_trader_slope_7d=0.0, vol_24h_vs_30d=0.0))
    assert MARK not in txt, "真的算出 0.0 卻被當成缺料＝把答案讀成未知（反向誤判）"
    assert "大戶斜率 +0.00" in txt
    assert "量比 0.00" in txt


def test_i_no_structure_at_all_draws_no_box():
    """⛔ 維持 v183：整份 structure 沒有 ⇒ 一行都不畫（框不出現不對盤面做斷言）。"""
    assert _structure_scorecard_lines({}) == []
    assert _structure_scorecard_lines({"structure": None}) == []


def test_j_all_fields_none_draws_no_box():
    """⛔ 7 格全 None（get_structure 回了但 4 條子請求全掛）⇒ 也不畫框。
    畫一個全是〔缺料〕的框＝天天噪音；框不出現本身不宣稱任何盤面事實。"""
    st = {k: None for k in FULL_ST}
    assert _structure_scorecard_lines({"structure": st}) == []


def test_k_basis_and_sentiment_not_collateral():
    """⛔ basis／sentiment 上游本來就有守門，不得被 structure 的缺料連坐。"""
    ov = {"structure": {k: None for k in FULL_ST},
          "basis": {"pct": -0.021, "interp": "現貨溢價"},
          "sentiment": {"fg": 41, "fg_label": "恐懼"}}
    txt = _joined(ov)
    assert "-0.021" in txt and "41" in txt
    assert MARK not in txt, "structure 全缺不該讓 basis／sentiment 被標成缺料"


def test_l_cvd_panel_genuine_zero_slope_still_balanced(monkeypatch):
    """⛔ 守門線：斜率真的算出 0.0 ⇒ 照舊判「多空均衡（+0.00）」、不標缺料。"""
    overlays = {"cvd": [float(i) for i in range(60)], "cvd_slope": 0.0}
    seen = _render_texts(monkeypatch, overlays)
    cvd_lines = [t for t in seen if "CVD" in t]
    joined = "\n".join(cvd_lines)
    assert "多空均衡" in joined, "0.0 是答案，仍該判多空均衡"
    assert MARK not in joined


def test_m_note_has_no_missing_glyphs(monkeypatch):
    """缺料字樣本身不得畫成豆腐方塊（v219 實測踩過：U+26A0 在 JhengHei 缺字）。"""
    import warnings
    with warnings.catch_warnings(record=True) as rec:
        warnings.simplefilter("always")
        _render_texts(monkeypatch, _st(vol_24h_vs_30d=None, cvd_slope_7d=None))
    bad = [str(w.message) for w in rec if "missing from font" in str(w.message)]
    assert not bad, f"缺料字樣有字畫不出來：{bad}"
