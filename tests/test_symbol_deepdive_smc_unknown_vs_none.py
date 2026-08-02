"""v218（監督員 r111）：per-symbol deepdive 的 SMC 區塊，「這輪沒算出來」不再
無聲折成「確認沒有這個結構」。

同物種第 38 次。落點是 `_format_symbol_data()` 的 SMC 量化結構區塊
（synthesizer.py:818 起）——與 v217 同一個函式、不同段落。v217 治的是**數字**被
折（缺值→0），本輪治的是**整段內容消失**：讀者（LLM）看不到某段，只會有一種
解讀＝「那個結構不存在」，而它其實是「這一輪沒算出來」。

差別為什麼要緊：這份 prompt 的產出會被 `_extract_plan_block()` 抽成機器可讀的
PLAN_JSON（進場／停損／目標）。「4h 沒有未緩解的 Order Block」與「4h 的 OB 這輪
沒算出來」對停損該擺哪裡是相反的結論——前者叫人放心往下擺，後者叫人別擺。

四種下場，全部在改動前的 HEAD 上實測過（非假想路徑）：

  (A) 時框的鍵整個不在（macro.py:579 那根 1d K 線沒抓到就是這樣）⇒ 舊碼取到 `{}`，
      `{}.get("error")` 是 None ⇒ 過得了守門，印出：
          `### 1d 時框 (現價 n/a, n/a 根)`
      後面一片空白。＝對 LLM 宣稱「1d 戰略層零根 K 線、沒有任何 SMC 結構」。

  (B) 時框回 error（`insufficient_candles`）⇒ 舊碼 `continue` **無聲跳過**，
      但區塊標題仍寫著「（4h 戰術 / 1d 戰略）」＝承諾兩個時框只給一個，
      讀起來一樣是「1d 沒東西」。

  (C) 單一分量算失敗 ⇒ `compute_smc_levels()` 留下 `order_blocks_error` 而不寫
      `order_blocks` 鍵，但 synthesizer **從來不讀那六個 *_error 鍵**（本輪 grep
      實證：零處引用）⇒ 整段 OB 靜靜消失。更糟的是 OB／BoS／流動性三段包在
      `if swings is not None:` 底下，上游 swing 一爆，三段同時消失且**連
      *_error 都沒有**。

  (D) 算出來了但被過濾光：N 個 OB 全部 mitigated、或 N 個 FVG 全部位移不足 ⇒
      舊碼整段消失＝「沒有 OB／沒有缺口」。實際是有、只是都被吃過／都不夠力。
      這是答案被折成**另一個答案**，不是折成未知。

判準（與產出端對齊，寫在 `_smc_unknown_components` 的註解裡）：
`compute_smc_levels()` 成功一律寫鍵（沒東西就寫空 list＝答案是「沒有」），
失敗才留 `<name>_error` 或整個鍵不寫 ⇒ **鍵在＝答案（含空的）；鍵不在＝未知**。

⛔ 邊界線（沿用 v208/v210/v212–v217）：算過而確實沒有，仍然是答案，照舊保持安靜，
   不可為保險一律喊「沒算出來」——否則盤面乾淨的標的每天都變缺料告警。
⛔ 反向側守門：資料齊全時輸出必須與舊碼**逐字相同**。
⛔ 不可把 liquidity_sweeps／ote 納入未知判定：前者只有 4h 會被 macro.py 補寫、
   1d 永遠沒有；後者是 premium_discount 的衍生值。納入＝天天誤報。
⛔ 未碰 strength.py／eval_cvd_divergence。

測試斷言的是**可觀測輸出字串**（LLM 實際讀到的那幾行），不是新輔助函式的回傳值。
"""
from __future__ import annotations

from l3_dispatcher.synthesizer import _format_symbol_data


def _smc_section(text: str) -> str:
    """抽出 SMC 區塊全文（含標題到下一個 `## ` 為止）。"""
    seg, grab = [], False
    for ln in text.split("\n"):
        if ln.startswith("## 🔬 SMC"):
            grab = True
        elif grab and ln.startswith("## "):
            break
        if grab:
            seg.append(ln)
    assert seg, f"輸出裡找不到 SMC 區塊。全文：\n{text}"
    return "\n".join(seg)


_FULL_4H = {
    "current_price": 65000, "candle_count": 300,
    "swing_points": [{"type": "high", "level": 66000, "distance_pct": 1.54, "ago_bars": 5}],
    "order_blocks": [{"type": "bullish", "bottom": 64000.0, "top": 64500.0,
                      "mid_distance_pct": -1.15, "ago_bars": 3, "strength": 82}],
    "fvg": [{"type": "bullish", "bottom": 63000.0, "top": 63500.0,
             "mid_distance_pct": -2.7, "ago_bars": 7, "significant": True}],
    "bos_choch": [{"type": "BOS", "direction": "bull", "level": 66000, "ago_bars": 4}],
    "liquidity": [{"type": "high_liquidity", "level": 67000,
                   "distance_pct": 3.08, "ago_bars": 12}],
    "premium_discount": {"zone": "discount", "swing_low": 61000, "swing_high": 67000,
                         "equilibrium": 64000, "price_position": 0.42},
}


# ───────────────── (A) 時框的鍵不在 ⇒ 不可生出空區塊 ─────────────────

def test_missing_timeframe_does_not_emit_empty_block():
    """1d 鍵不存在 ⇒ 不可印出『### 1d 時框 (現價 n/a, n/a 根)』後接一片空白。"""
    sec = _smc_section(_format_symbol_data("BTC", {"smc_levels": {"4h": _FULL_4H}}))
    assert "(現價 n/a, n/a 根)" not in sec, f"缺整個時框卻印出空區塊：\n{sec}"
    assert "1d 時框" in sec, f"1d 整個消失，但標題仍承諾『1d 戰略』：\n{sec}"
    assert "這輪沒有這個時框的資料" in sec
    assert "不等於 1d 沒有 SMC 結構" in sec


def test_errored_timeframe_is_stated_not_silently_dropped():
    """1d 回 error ⇒ 不可無聲跳過（標題仍承諾 1d 戰略）。"""
    sec = _smc_section(_format_symbol_data("BTC", {"smc_levels": {
        "4h": _FULL_4H,
        "1d": {"error": "insufficient_candles", "needed": 30, "got": 12}}}))
    assert "1d 時框" in sec, f"errored 時框被無聲跳過：\n{sec}"
    assert "這輪算不出來" in sec
    assert "insufficient_candles" in sec, "應講明算不出來的原因"
    assert "不等於 1d 沒有 SMC 結構" in sec


# ────────────── (C) 單一分量算失敗 ⇒ 不可靜靜消失 ──────────────

def test_component_error_is_reported_not_silently_absent():
    """order_blocks 算爆 ⇒ 必須明講「這輪沒算出來」，不可讓 OB 段直接消失。"""
    sec = _smc_section(_format_symbol_data("BTC", {"smc_levels": {"4h": {
        "current_price": 65000, "candle_count": 300,
        "swing_points": _FULL_4H["swing_points"],
        "order_blocks_error": "ValueError: boom",
    }}}))
    assert "沒算出來" in sec, f"分量算爆卻無聲消失：\n{sec}"
    assert "Order Block" in sec
    assert "ValueError: boom" in sec, "有錯誤字串就該講出來"
    assert "不是確認沒有" in sec


def test_swing_failure_reports_all_three_dependent_components():
    """swing 一爆 ⇒ OB／BoS／流動性三段同時消失且連 *_error 都沒有，必須全部點名。"""
    sec = _smc_section(_format_symbol_data("BTC", {"smc_levels": {"4h": {
        "current_price": 65000, "candle_count": 300,
        "swing_points_error": "KeyError: HighLow",
    }}}))
    for label in ("Swing 點", "Order Block", "FVG",
                  "結構變化(BoS/CHoCH)", "流動性區域", "溢價/折價區間"):
        assert label in sec, f"連坐消失的分量 {label} 沒被點名：\n{sec}"
    assert "不可讀成該結構不存在" in sec


# ────────────── (D) 被過濾光 ≠ 沒有 ──────────────

def test_all_mitigated_order_blocks_is_not_reported_as_no_ob():
    """N 個 OB 全部已 mitigated ⇒ 不可整段消失（那讀起來＝沒有 OB）。"""
    sec = _smc_section(_format_symbol_data("BTC", {"smc_levels": {"4h": {
        "current_price": 65000, "candle_count": 300,
        "swing_points": _FULL_4H["swing_points"], "fvg": [], "bos_choch": [],
        "liquidity": [], "premium_discount": _FULL_4H["premium_discount"],
        "order_blocks": [{"type": "bullish", "bottom": 1.0, "top": 2.0,
                          "mid_distance_pct": -1.0, "ago_bars": 3,
                          "strength": 50, "mitigated": True}],
    }}}))
    assert "全部已 mitigated" in sec, f"OB 全被吃掉卻整段消失：\n{sec}"
    assert "不是沒有 OB" in sec


def test_all_insignificant_fvgs_is_not_reported_as_no_fvg():
    """N 個 FVG 全部位移不足 ⇒ 不可整段消失。"""
    sec = _smc_section(_format_symbol_data("BTC", {"smc_levels": {"4h": {
        "current_price": 65000, "candle_count": 300,
        "swing_points": _FULL_4H["swing_points"], "order_blocks": [],
        "bos_choch": [], "liquidity": [],
        "premium_discount": _FULL_4H["premium_discount"],
        "fvg": [{"type": "bullish", "bottom": 1.0, "top": 2.0,
                 "mid_distance_pct": -1.0, "ago_bars": 7, "significant": False}],
    }}}))
    assert "全部位移不足" in sec, f"FVG 全被濾掉卻整段消失：\n{sec}"
    assert "不是沒有缺口" in sec


# ─────────── 反向側：算過而確實沒有 ⇒ 保持安靜，不可喊缺料 ───────────

def test_computed_but_genuinely_empty_stays_silent():
    """⛔ 邊界線：六個分量都算過、都是空的 ⇒ 是答案，不可報成「沒算出來」。"""
    sec = _smc_section(_format_symbol_data("BTC", {"smc_levels": {"4h": {
        "current_price": 65000, "candle_count": 300,
        "swing_points": [], "order_blocks": [], "fvg": [], "bos_choch": [],
        "liquidity": [], "premium_discount": {},
    }}}))
    assert "沒算出來" not in sec, f"算過而確實沒有被誤報成缺料：\n{sec}"
    assert "全部已 mitigated" not in sec, "本來就沒有 OB，不該講被吃掉"
    assert "全部位移不足" not in sec


def test_full_data_smc_section_is_byte_identical_to_old_behaviour():
    """反向側總守門：兩個時框都齊全時，SMC 區塊每一行都與舊碼逐字相同。"""
    sec = _smc_section(_format_symbol_data("BTC", {"smc_levels": {
        "4h": _FULL_4H,
        "1d": dict(_FULL_4H,
                   swing_points=[{"type": "low", "level": 61000,
                                  "distance_pct": -6.15, "ago_bars": 9}]),
    }}))
    assert "### 4h 時框 (現價 $65000, 300 根)" in sec
    assert "### 1d 時框 (現價 $65000, 300 根)" in sec
    assert "  - high @ $66000 (+1.54%, 5 根前)" in sec
    assert "  - bullish OB: $64000.00 – $64500.00 (-1.15%, 3 根前, 強度 82/100)" in sec
    assert "  - bullish FVG: $63000.00 – $63500.00 (-2.70%, 7 根前)" in sec
    assert "  - BOS bull @ $66000 (4 根前)" in sec
    assert "  - high_liquidity @ $67000 (+3.08%, 12 根前)" in sec
    assert "折價區（偏找多、不追空）" in sec
    assert "沒算出來" not in sec, f"資料齊全卻報缺料：\n{sec}"
    assert "n/a" not in sec, f"資料齊全卻出現 n/a：\n{sec}"
