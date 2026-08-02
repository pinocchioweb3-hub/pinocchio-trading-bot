"""v219：SMC 圖表上「這輪沒算出來」不再被畫成「確認沒有這個結構」。

同物種第 39 次（v208/v210/v212–v218 一脈），但**首次落在使用者眼睛直接看的那張圖上**。

落點 l3_dispatcher/chart_render.py::render_smc_chart。圖上三個 SMC 圖層全部從
`smc.get(<key>) or []` 取值：

    :409  _all_fvg = smc.get("fvg") or []
    :450  _obs     = [... for o in (smc.get("order_blocks") or []) ...]
    :465  detect_structure_breaks(candles, smc.get("swing_points") or [])
    :478  swings   = sorted((smc.get("swing_points") or []), ...)
    :506  _detect_sweeps(candles, swings, n)      ← swings 為空就一個掃單標記都不畫

⇒ 分量算失敗（鍵不在）與「算過、確實沒有」（鍵在、空 list）在圖上**長得一模一樣**：
   都是那個圖層什麼都沒畫。而看圖的人只會有一種解讀——「這段行情沒有這個結構」。

判準與產出端 market_intel_mcp/smc_levels.py::compute_smc_levels() 對齊（v218 已立）：
成功一律寫鍵（沒東西就寫空 list＝答案是「沒有」），失敗才留 `<name>_error`；而
order_blocks 包在 `if swings is not None:` 底下，上游 swing 一爆就整個鍵不寫。
⇒ 鍵在＝答案（含空的）；鍵不在＝這輪沒算出來。

⛔ 邊界線：算過而確實沒有仍然是答案，圖上照舊保持安靜（反向側 D/E 兩條）。
"""
import pytest

matplotlib = pytest.importorskip("matplotlib")
from matplotlib.axes import Axes  # noqa: E402

from l3_dispatcher import chart_render  # noqa: E402

MARK = "沒算出來"


def _candles(n: int = 60) -> list[dict]:
    """單調鋸齒假 K 線——夠 render_smc_chart 跑完全程（需 ≥30 根）。"""
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


def _texts(monkeypatch) -> list[str]:
    """側錄圖上所有 ax.text() 的字串（真的跑 render，不是假想路徑）。"""
    seen: list[str] = []
    orig = Axes.text

    def spy(self, x, y, s="", *a, **k):
        seen.append(str(s))
        return orig(self, x, y, s, *a, **k)

    monkeypatch.setattr(Axes, "text", spy)
    return seen


def _render(monkeypatch, smc: dict) -> list[str]:
    seen = _texts(monkeypatch)
    out = chart_render.render_smc_chart("TESTCOIN", _candles(), smc, tf="4h")
    assert out is not None, "render 本身不該失敗（本測試測的是誠實度，不是可用性）"
    try:
        out.unlink()
    except OSError:
        pass
    return seen


def _full_smc(empty: bool = False) -> dict:
    """六個分量全部算過的 smc。empty=True ⇒ 算過但確實沒有（空 list）。"""
    if empty:
        return {"current_price": 100.0, "candle_count": 60,
                "swing_points": [], "order_blocks": [], "fvg": [],
                "bos_choch": [], "liquidity": [],
                "premium_discount": {"zone": "equilibrium"}}
    return {
        "current_price": 100.0, "candle_count": 60,
        "swing_points": [{"type": "high", "level": 118.0, "ago_bars": 8},
                         {"type": "low", "level": 104.0, "ago_bars": 20}],
        "order_blocks": [{"type": "bullish", "top": 106.0, "bottom": 104.0,
                          "ago_bars": 18, "mitigated": None}],
        "fvg": [{"type": "bullish", "top": 110.0, "bottom": 108.5,
                 "ago_bars": 6, "mitigated": None}],
        "bos_choch": [], "liquidity": [],
        "premium_discount": {"zone": "premium"},
    }


# ---------- 正向側：這輪沒算出來，圖上必須看得見 ----------

def test_a_whole_smc_failed_is_marked(monkeypatch):
    """(A) 整份 SMC 失敗。render_symbol_chart:849 對 error 的處置是 `smc = {}`，
    ⇒ 三個圖層全空、圖名仍寫著「SMC＋SNR 結構」＝整張圖宣稱這是一段乾淨無結構的行情。"""
    seen = _render(monkeypatch, {})
    assert any(MARK in t for t in seen), "整份 SMC 沒算出來，圖上卻一句話都沒說"


def test_b_single_component_error_is_marked(monkeypatch):
    """(B) 單一分量失敗：compute_smc_levels 留 fvg_error 而不寫 fvg 鍵。"""
    smc = _full_smc()
    smc.pop("fvg")
    smc["fvg_error"] = "boom"
    seen = _render(monkeypatch, smc)
    hit = [t for t in seen if MARK in t]
    assert hit, "FVG 這輪沒算出來，圖上卻沒標"
    assert "FVG" in hit[0]
    assert "Order Block" not in hit[0], "算過的分量不該被連坐報成未知"


def test_c_swing_failure_covers_three_layers(monkeypatch):
    """(C) 上游 swing 一爆，HH/HL 標記、BoS/CHoCH、掃單標記三個圖層同時消失，
    且 order_blocks 因巢狀在 `if swings is not None:` 底下連鍵都不會寫。"""
    smc = _full_smc()
    smc.pop("swing_points")
    smc.pop("order_blocks")
    smc["swing_points_error"] = "boom"
    seen = _render(monkeypatch, smc)
    hit = [t for t in seen if MARK in t]
    assert hit, "swing 沒算出來，圖上卻沒標"
    assert "Swing" in hit[0] and "Order Block" in hit[0]


def test_d_note_names_the_downstream_layers(monkeypatch):
    """swing 失敗時，caveat 必須點名連帶消失的下游圖層（BoS／掃單），
    否則看圖的人仍會把空白讀成『沒有結構變化』。"""
    smc = _full_smc()
    smc.pop("swing_points")
    smc["swing_points_error"] = "boom"
    seen = _render(monkeypatch, smc)
    hit = [t for t in seen if MARK in t]
    assert hit
    assert "BoS" in hit[0] or "掃單" in hit[0]


def test_e_note_says_absence_is_not_evidence(monkeypatch):
    """caveat 必須講出「圖上沒畫 ≠ 沒有」，而不只是列一串分量名。"""
    seen = _render(monkeypatch, {})
    hit = [t for t in seen if MARK in t]
    assert hit
    assert "≠" in hit[0] or "不等於" in hit[0]


# ---------- 反向側：算過就是答案，圖上必須保持安靜 ----------

def test_f_all_computed_with_data_stays_silent(monkeypatch):
    """(F) 六個分量都算出來且有東西 ⇒ 圖上逐字不得多出任何缺料字樣。"""
    seen = _render(monkeypatch, _full_smc())
    assert not [t for t in seen if MARK in t], "資料齊全卻誤報缺料＝慢性假警報"


def test_g_computed_but_genuinely_empty_stays_silent(monkeypatch):
    """(G) 算過、確實沒有（空 list）＝那是答案，不是未知 ⇒ 保持安靜。
    ⛔ 這條就是不可為保險一律喊「沒算出來」的守門線：盤面乾淨的標的
       每天都會走到這條路徑。"""
    seen = _render(monkeypatch, _full_smc(empty=True))
    assert not [t for t in seen if MARK in t], "空 list 是答案『沒有』，不是未知"


def test_i_note_has_no_missing_glyphs(monkeypatch):
    """警語本身不得畫成豆腐方塊。⛔ 這條是本輪實測抓到的：初版用了 U+26A0（⚠），
    Microsoft JhengHei 缺這個字 ⇒ matplotlib 發 'missing from font' 警告、圖上出現
    空心方框。缺料警語變亂碼＝比不標更糟（看的人只會覺得圖壞了）。"""
    import warnings
    with warnings.catch_warnings(record=True) as rec:
        warnings.simplefilter("always")
        _render(monkeypatch, {})
    bad = [str(w.message) for w in rec if "missing from font" in str(w.message)]
    assert not bad, f"警語有字畫不出來：{bad}"


def test_h_untouched_keys_never_trigger(monkeypatch):
    """⛔ 圖上根本不畫的分量（ote／liquidity_sweeps）不得納入未知判定——
    前者是 premium_discount 的衍生值、後者只有 4h 會被 macro.py 補寫，
    納入＝天天誤報。本條在 v218 的 synthesizer 側已立，圖側同樣守。"""
    smc = _full_smc()
    smc.pop("premium_discount")   # 圖上不畫 → 不該因為它缺就喊缺料
    smc.pop("liquidity")          # 圖上的掃單是自算的，不讀這個鍵
    seen = _render(monkeypatch, smc)
    assert not [t for t in seen if MARK in t], "圖上不畫的分量不該觸發圖上的缺料標註"
