"""v217（監督員 r110）：per-symbol deepdive 餵給 LLM 的那份數據表，「讀不出來」
不再折成一個確定的數字、不再折出一個方向性結論、也不再讓整份交易計畫停播。

同物種第 37 次。落點是 `_format_symbol_data()`——它是 per-symbol deepdive 的
唯一 prompt 組裝點（synthesizer.py:939 `synthesize_symbol_deepdive` 走它），
而該次呼叫的產出會再經 `_extract_plan_block()` 抽出機器可讀的 PLAN_JSON
（進場/停損/目標）。也就是說：這裡寫出去的每一行，都是 LLM 寫交易計畫時
當成**既成事實**的前提。

三種下場（都在改動前的 HEAD 上實測過，非假想路徑）：

  (A) 誤報數字：`.get(key, 0)` 把「這輪沒回來」寫成一個確定的 0。
      實測舊碼輸出：
        - `- 4h: 趨勢=上升 (+0.00%)`   ＝同時宣稱有趨勢與零漲跌（自相矛盾的前提）
        - `- OI: $0  24h 變化 +0.00%`  ＝宣稱永續合約未平倉為零
        - `- Funding: ≈0.0000%/8h`     ＝宣稱資費中性
        - `- 24h 清算: 多 $5.00M  空 $0.00M` ＝宣稱空單那邊沒有人被清算
        - `- 7d 累計: $+0.0M`          ＝宣稱機構這週零進出（v216 治過的同一句話，
                                          不同呼叫端）
        - `強度 0/100`                 ＝宣稱這個 OB 毫無強度

  (B) **由折出來的 0 推出方向性結論**——本物種第一次不只是數字被折。
      `liq_24h` 缺 short 鍵時舊碼實測輸出：
        `- 近24h 清算：多 3.00M／空 0.00M USD（多頭被清算較多→下殺燃料）`
      「下殺燃料」是一句看空的市場判讀，而它整個建立在一個從缺鍵折出來的 0 上。
      這是 LLM 會直接抄進交易計畫理由的那種句子。

  (C) 停播：無預設值的 `.get(key)` 直進 `:+.2f` → None 一撞 TypeError，
      而壞掉的不是一行，是**整個標的的 deepdive prompt 生不出來**＝那一輪
      這個標的完全沒有交易計畫。HEAD 上實測有五個獨立觸發點：
      snapshot.oi / 鯨魚 net_long_pct / SMC swing distance_pct /
      OB bottom / coinglass cvd[-1]。鯨魚那三個甚至是直接下標、零守門。

⛔ 邊界線（沿用 v208/v210/v212–v216）：來源明講的 0 仍然是答案，照常印 0，
   不可為保險打成 n/a——否則「淨多為 0／ETF 淨流為 0」這種正常值每天都變缺料告警。
⛔ 反向側守門：資料齊全時輸出必須與舊碼**逐字相同**，不許退化成一律不敢算。
⛔ 未碰 strength.py／eval_cvd_divergence。

這些測試刻意斷言**可觀測輸出字串**（LLM 實際會讀到的那幾行），而不是新輔助
函式的回傳值——否則把新函式刪掉測試就一起消失，等於虛設檢定。
"""
from __future__ import annotations

import pytest

from l3_dispatcher.synthesizer import _format_symbol_data


def _line(text: str, needle: str) -> str:
    """取出含 needle 的那一行；找不到就讓測試直接講清楚失敗原因。"""
    for ln in text.split("\n"):
        if needle in ln:
            return ln
    raise AssertionError(f"輸出裡找不到含 {needle!r} 的行。全文：\n{text}")


# ─────────────────────────── (A) 誤報數字 ───────────────────────────

def test_pattern_missing_change_pct_not_folded_to_zero():
    """缺 change_pct ⇒ 不可印「(+0.00%)」（那等於宣稱該時框零漲跌）。"""
    out = _format_symbol_data("BTC", {
        "pattern": {"consensus": "bullish",
                    "by_tf": {"4h": {"trend": {"direction": "上升"}}}},
    })
    ln = _line(out, "4h: 趨勢")
    assert "+0.00%" not in ln, f"缺料被折成零漲跌：{ln}"
    assert "n/a" in ln


def test_snapshot_missing_oi_not_folded_to_zero():
    """缺 oi/oi_delta_pct ⇒ 不可印「$0」「+0.00%」。"""
    out = _format_symbol_data("BTC", {
        "snapshot": {"price": 65000, "top_trader_ratio": 1.1, "ls_ratio": 0.9},
    })
    ln = _line(out, "- OI:")
    assert "$0 " not in ln and "+0.00%" not in ln, f"缺料被折成零 OI：{ln}"
    assert "n/a" in ln


def test_snapshot_missing_funding_not_folded_to_neutral():
    """缺 funding ⇒ 不可印「≈0.0000%」（那等於宣稱資費中性）。"""
    out = _format_symbol_data("BTC", {"snapshot": {"price": 1}})
    ln = _line(out, "- Funding:")
    assert "0.0000%" not in ln, f"缺料被折成中性資費：{ln}"
    assert "n/a" in ln


def test_snapshot_one_sided_liquidation_not_folded_to_zero():
    """清算只有一邊有值 ⇒ 未知那邊不可印「$0.00M」。"""
    out = _format_symbol_data("BTC", {
        "snapshot": {"price": 1, "liq_long": 5e6, "liq_short": None},
    })
    ln = _line(out, "24h 清算:")
    assert "$5.00M" in ln, f"可讀的那邊必須照算：{ln}"
    assert "空 $0.00M" not in ln, f"未知那邊被折成零清算：{ln}"
    assert "n/a" in ln


def test_etf_missing_flow_not_folded_to_zero():
    """缺 ETF 流向鍵 ⇒ 不可印「$+0.0M」（那等於宣稱機構零進出）。"""
    out = _format_symbol_data("BTC", {"etf_btc": {"some_other_key": 1}})
    ln = _line(out, "7d 累計")
    assert "+0.0M" not in ln, f"缺料被折成零流向：{ln}"
    assert "n/a" in ln


def test_order_block_missing_strength_not_folded_to_zero():
    """缺 strength ⇒ 不可印「強度 0/100」。"""
    out = _format_symbol_data("BTC", {
        "smc_levels": {"4h": {"current_price": 100, "candle_count": 300,
                              "order_blocks": [{"type": "bullish", "bottom": 90.0,
                                                "top": 95.0, "mid_distance_pct": -7.5,
                                                "ago_bars": 3}]}},
    })
    ln = _line(out, "OB:")
    assert "強度 0/100" not in ln, f"缺料被折成零強度：{ln}"
    assert "強度 n/a" in ln


# ────────────── (B) 由折出來的 0 推出方向性結論（本輪重點） ──────────────

def test_liq24h_missing_side_does_not_produce_directional_verdict():
    """清算缺一邊 ⇒ 不可推出「下殺燃料／軋空燃料」這種看多看空的判讀。"""
    out = _format_symbol_data("BTC", {
        "coinglass": {"cvd": None, "oi": None, "funding": 0.0001,
                      "ls_ratio": None, "liq_24h": {"long": 3e6}},
    })
    ln = _line(out, "近24h 清算")
    assert "下殺燃料" not in ln, f"從缺鍵折出的 0 被推成看空結論：{ln}"
    assert "軋空燃料" not in ln
    assert "多空清算均衡" not in ln, f"缺一邊卻宣稱均衡：{ln}"
    assert "判不了" in ln
    assert "3.00M" in ln, f"可讀的那邊必須照算：{ln}"


def test_liq24h_both_sides_present_still_gives_verdict():
    """反向側：兩邊都有值時，燃料方向判讀必須照舊產出（不許退化成不敢判）。"""
    out = _format_symbol_data("BTC", {
        "coinglass": {"funding": 0.0001, "liq_24h": {"long": 1e6, "short": 9e6}},
    })
    ln = _line(out, "近24h 清算")
    assert "軋空燃料" in ln, f"資料齊全卻不敢判：{ln}"
    assert "多 1.00M／空 9.00M USD" in ln


# ─────────── (C) 停播：整份 deepdive prompt 不可因 None 而生不出來 ───────────

@pytest.mark.parametrize("label,state", [
    ("snapshot.oi", {"snapshot": {"price": 65000, "oi": None, "oi_delta_pct": None}}),
    ("whale.net_long_pct", {"whales": {"per_symbol_aggregate": [
        {"symbol": "BTC", "net_long_pct": None, "long_usd": 1e6, "short_usd": 2e6}]}}),
    ("smc.swing.distance_pct", {"smc_levels": {"4h": {
        "current_price": 65000, "candle_count": 300,
        "swing_points": [{"type": "HH", "level": 66000,
                          "distance_pct": None, "ago_bars": 5}]}}}),
    ("smc.ob.bottom", {"smc_levels": {"4h": {
        "current_price": 1, "candle_count": 1,
        "order_blocks": [{"type": "bullish", "bottom": None, "top": 100,
                          "mid_distance_pct": 1.0, "ago_bars": 3}]}}}),
    ("coinglass.cvd_last", {"coinglass": {"cvd": [1.0, None], "cvd_slope": 5.0}}),
    ("empty_state", {}),
])
def test_none_anywhere_does_not_kill_whole_prompt(label, state):
    """任一欄位為 None 都不可讓整份 prompt 拋例外（漏報不可換成停播）。"""
    out = _format_symbol_data("BTC", state)
    assert isinstance(out, str) and out.startswith("# BTC 完整數據"), label


# ─────────────────── 反向側：有值時逐字相同、明講的 0 照印 ───────────────────

def test_source_stated_zero_is_still_printed_as_zero():
    """⛔ 邊界線：來源明講的 0 是答案，不可為保險打成 n/a。"""
    out = _format_symbol_data("BTC", {
        "snapshot": {"price": 65000, "oi": 0, "oi_delta_pct": 0.0},
        "etf_btc": {"cumulative_7d_flow_usd": 0, "latest_24h_flow_usd": 0},
    })
    oi_ln = _line(out, "- OI:")
    assert "$0" in oi_ln and "+0.00%" in oi_ln, f"明講的 0 被打成 n/a：{oi_ln}"
    assert "n/a" not in oi_ln
    etf_ln = _line(out, "7d 累計")
    assert "$+0.0M" in etf_ln and "n/a" not in etf_ln, f"明講的 0 被打成 n/a：{etf_ln}"


def test_zero_liquidation_both_sides_is_an_answer_not_a_gap():
    """來源明講兩邊清算都是 0 ⇒ 是答案，不可判成「數據源停權中」。

    這是舊碼 `if _ll or _ls2` 的反方向錯誤：把答案折成未知。
    """
    out = _format_symbol_data("BTC", {
        "snapshot": {"price": 1, "liq_long": 0, "liq_short": 0},
    })
    ln = _line(out, "24h 清算")
    assert "停權中" not in ln, f"明講的 0 被判成缺料：{ln}"
    assert "多 $0.00M" in ln and "空 $0.00M" in ln


def test_full_data_output_is_byte_identical_to_old_behaviour():
    """反向側總守門：資料齊全時，每一行都必須與舊碼逐字相同。"""
    out = _format_symbol_data("BTC", {
        "pattern": {"consensus": "bullish",
                    "by_tf": {"4h": {"trend": {"direction": "上升", "change_pct": 3.21},
                                     "sr": {"supports": [{"price": 64000, "distance_pct": -1.5}],
                                            "resistances": [{"price": 67000, "distance_pct": 3.1}]}}}},
        "snapshot": {"price": 65000, "oi": 1234567, "oi_delta_pct": 2.5,
                     "funding": 0.0001, "top_trader_ratio": 1.1, "ls_ratio": 0.9,
                     "liq_long": 5e6, "liq_short": 3e6},
        "etf_btc": {"cumulative_7d_flow_usd": 12e6, "latest_24h_flow_usd": -3e6},
        "whales": {"per_symbol_aggregate": [
            {"symbol": "BTC", "net_long_pct": 12.0, "long_usd": 5e6, "short_usd": 2e6}]},
        "smc_levels": {"4h": {
            "current_price": 65000, "candle_count": 300,
            "swing_points": [{"type": "HH", "level": 66000,
                              "distance_pct": 1.54, "ago_bars": 5}],
            "order_blocks": [{"type": "bullish", "bottom": 64000.0, "top": 64500.0,
                              "mid_distance_pct": -1.15, "ago_bars": 3,
                              "strength": 82}],
            "liquidity": [{"type": "BSL", "level": 67000, "distance_pct": 3.08,
                           "ago_bars": 12}]},
            # ⚠️ 1d 必須一起填滿：`_format_symbol_data` 對 4h/1d 兩個時框無條件迭代，
            #    少填一個就會生出一段空的 1d 區塊，讓「資料齊全」這個前提不成立。
            "1d": {"current_price": 65000, "candle_count": 300,
                   "swing_points": [{"type": "HL", "level": 61000,
                                     "distance_pct": -6.15, "ago_bars": 9}]}},
    })
    assert "- 4h: 趨勢=上升 (+3.21%)" in out
    assert "   支撐: $64000(-1.5%)" in out
    assert "   阻力: $67000(3.1%)" in out
    assert "- OI: $1,234,567  24h 變化 +2.50%（備援源）" in out
    assert "- Funding: +0.0100%/8h（備援源）" in out
    assert "- 24h 清算: 多 $5.00M  空 $3.00M" in out
    assert "- 7d 累計: $+12.0M" in out
    assert "- 24h: $-3.0M" in out
    assert "- 淨多倉位百分比: +12%" in out
    assert "- 多倉 $5.0M  空倉 $2.0M" in out
    assert "  - HH @ $66000 (+1.54%, 5 根前)" in out
    assert "  - bullish OB: $64000.00 – $64500.00 (-1.15%, 3 根前, 強度 82/100)" in out
    assert "  - BSL @ $67000 (+3.08%, 12 根前)" in out
    assert "n/a" not in out, "資料齊全卻出現 n/a：" + out
