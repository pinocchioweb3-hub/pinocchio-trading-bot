"""v216（監督員 r109）：Daily Macro 餵給 LLM 的那份數據表，「讀不出來」不再
折成一個確定的數字、也不再讓整張卡當掉。

同物種第 36 次。這一次落在 `_format_data_for_prompt()`——它是 Daily Macro
唯一的 prompt 組裝點（synthesizer.py:1100 給 claude CLI 版、:1177 給 SDK 版
都走它），也就是說：這裡寫出去的每一行，都會被 LLM 當成**既成事實**寫進
使用者讀的那份簡報。所以這一處同時具備本物種的兩種下場：

  (A) 誤報：`.get(key, 0)` 把「這個分量這輪沒回來」寫成一個確定的 0。
      例：ETF 機構流向缺鍵 → 「7d $+0.0M」＝「機構這週沒進出」（強市場宣稱）；
          期現基差缺鍵 → 「基差 +0.0000%」＝「無基差」；
          drawdown 缺鍵 → 「距期內高 0.0%」＝「就在期內高點」。
      這正是 v212／v214 治過的形狀，只是這次的下游是 LLM 的嘴。

  (B) 當掉：`.get(key)` 無預設值直接進 `:+.2f` → None 一撞就 TypeError，
      而它不是某一行壞掉，是**整份 Daily Macro 生不出來**（漏報被換成停播）。
      這是 v215 顯示端修法的同一條理由。

⛔ 邊界線（沿用 v208/v210/v212/v213/v214/v215）：來源明講的 0 仍然是答案，
   不可為保險打成 n/a——否則正常的「資費為 0／淨多為 0」每天都變缺料告警。
⛔ 反向側守門：資料齊全時輸出必須與舊碼**逐字相同**，不許退化成一律不敢算。

這些測試刻意斷言**可觀測輸出字串**（LLM 實際會讀到的那幾行），而不是新
輔助函式的回傳值——否則把新函式刪掉測試就一起消失，等於虛設檢定。
"""
from __future__ import annotations

import pytest

from l3_dispatcher.synthesizer import _format_data_for_prompt


def _line(text: str, needle: str) -> str:
    """取出含 needle 的那一行，找不到就直接讓測試講清楚失敗原因。

    ⚠️ needle 要挑「只會出現在目標行」的字串——例如別用 "ETH"，因為
    「## 加密貨幣指標層（BTC/ETH/SOL）」那行標題也含它。
    """
    for ln in text.split("\n"):
        if needle in ln:
            return ln
    raise AssertionError(f"輸出中找不到含 {needle!r} 的行；全文：\n{text}")


def _complete_state(**overrides) -> dict:
    """一份「每個分量都有值」的 state。

    反向側守門要斷言「整份輸出不含 n/a」，就必須把**所有**會被走到的分量都
    填滿——否則漏填的那個分量在修好之後本來就該印 n/a，會把守門測試搞成假紅。
    （第一版正是漏了 options_eth，才讓三個守門測試在 HEAD 上就死在 :324。）
    """
    state = {
        "eth_btc_ratio": 0.052,
        "metrics": {
            "BTC": {"current_price": 100000, "return_7d_pct": 3.21,
                    "drawdown_from_high_pct": -4.5, "ma50": 95000},
            # ETH/SOL 明講 error ⇒ 整段跳過，不影響「不得出現 n/a」的斷言
            "ETH": {"error": "skip"}, "SOL": {"error": "skip"},
            # 現貨層固定跑 SUI/WLFI，一樣要填滿才不會誤觸「不得出現 n/a」
            "SUI": {"error": "skip"}, "WLFI": {"error": "skip"},
        },
        "extras": {"BTC": {"funding": 0.0001}},
        "basis_btc": {"basis_pct": 0.1234, "interpretation": "正價差"},
        "basis_eth": {"error": "skip"},
        "etf_btc": {"cumulative_7d_flow_usd": 250e6, "latest_24h_flow_usd": -30e6},
        "etf_eth": {"error": "skip"},
        "options_btc": {"total_oi_usd": 32.5e9, "weighted_24h_change_pct": 5.67},
        "options_eth": {"error": "skip"},
    }
    state.update(overrides)
    return state


# ══════════════════════════════════════════════════════════════════════
# (B) 當掉側：非 error 的 dict 但缺鍵 → 舊碼 TypeError/KeyError，整份簡報生不出來
#     注意 guard 是 `if o.get("error"): continue`——它只擋「明講錯誤」的 dict，
#     擋不住「鍵根本不在」的 dict，後者一路走到格式化才爆。
# ══════════════════════════════════════════════════════════════════════

def test_options_missing_change_does_not_kill_whole_prompt():
    """期權區缺 weighted_24h_change_pct：舊碼 TypeError → 整份 Daily Macro 沒了。"""
    state = {"options_btc": {"total_oi_usd": 5.0e9}}
    out = _format_data_for_prompt(state)          # 舊碼在這行就 TypeError
    ln = _line(out, "總 OI")
    assert "5.00B" in ln, ln                      # 讀得到的照樣講
    assert "+0.00%" not in ln, f"缺料被折成確定的 0：{ln}"
    assert "n/a" in ln or "讀不出來" in ln, ln


def test_liq_scan_missing_fields_does_not_kill_whole_prompt():
    """清算 Top5 某筆缺 total_24h/imbalance：舊碼 TypeError。"""
    state = {"liq_scan": {"items": [{"symbol": "BTC"}]}}
    out = _format_data_for_prompt(state)
    ln = _line(out, "imbalance")
    assert "+0.00" not in ln, f"缺料被折成確定的 0：{ln}"
    assert "n/a" in ln or "讀不出來" in ln, ln


def test_whales_missing_fields_does_not_kill_whole_prompt():
    """鯨魚區缺 net_long_pct/total_usd：舊碼 KeyError。"""
    state = {"whales": {"per_symbol_aggregate": [{"symbol": "ETH"}]}}
    out = _format_data_for_prompt(state)
    ln = _line(out, "淨多")
    assert "n/a" in ln or "讀不出來" in ln, ln


def test_pi_cycle_missing_distance_does_not_kill_whole_prompt():
    """Pi Cycle 缺 distance_pct：舊碼 TypeError。
    ⛔ 註：Pi Cycle 本身依 memory 定案永不用於決策，此處僅是顯示層誠實化。"""
    state = {"cycle": {"pi_cycle": {"signal": "neutral"}}}
    out = _format_data_for_prompt(state)
    ln = _line(out, "Pi Cycle")
    assert "signal=neutral" in ln, ln
    assert "n/a" in ln or "讀不出來" in ln, ln


def test_funding_outliers_missing_value_does_not_kill_whole_prompt():
    """Funding 極端值缺 funding_pct_8h：舊碼 KeyError（`h['funding_pct_8h']`）。"""
    state = {"funding_outliers": {"hottest": [{"symbol": "DOGE"}], "coldest": []}}
    out = _format_data_for_prompt(state)
    ln = _line(out, "DOGE")
    assert "+0.000%" not in ln, f"缺料被折成確定的 0：{ln}"
    assert "n/a" in ln or "讀不出來" in ln, ln


def test_pattern_sr_missing_distance_does_not_kill_whole_prompt():
    """支撐/阻力缺 distance_pct：舊碼 KeyError（`s['distance_pct']`）。"""
    state = {"pattern_btc": {"consensus": "up",
                             "by_tf": {"1h": {"trend": {"direction": "up", "change_pct": 1.5},
                                              "sr": {"supports": [{"price": 100.0}]}}}}}
    out = _format_data_for_prompt(state)
    ln = _line(out, "支撐")
    assert "100.0" in ln, ln
    assert "n/a" in ln or "讀不出來" in ln, ln


# ══════════════════════════════════════════════════════════════════════
# (A) 誤報側：缺鍵被 `.get(k, 0)` 折成確定的 0，然後被 LLM 當事實講出去
# ══════════════════════════════════════════════════════════════════════

def test_etf_missing_flow_is_not_reported_as_zero_flow():
    """ETF 缺鍵 → 舊碼印「7d $+0.0M  24h $+0.0M」＝宣稱機構這週零進出。"""
    state = {"etf_btc": {"source": "coinglass"}}
    out = _format_data_for_prompt(state)
    ln = _line(out, "BTC ETF")
    assert "+0.0M" not in ln, f"機構流向缺料被講成零流入/流出：{ln}"
    assert "n/a" in ln or "讀不出來" in ln, ln


def test_basis_missing_is_not_reported_as_zero_basis():
    """期現基差缺鍵 → 舊碼印「基差 +0.0000%」＝宣稱無基差。"""
    state = {"basis_btc": {"interpretation": ""}}
    out = _format_data_for_prompt(state)
    ln = _line(out, "- BTC: 基差")
    assert "+0.0000%" not in ln, f"基差缺料被講成 0：{ln}"
    assert "n/a" in ln or "讀不出來" in ln, ln


def test_drawdown_missing_is_not_reported_as_at_the_high():
    """metrics 缺 drawdown_from_high_pct → 舊碼印「距期內高 0.0%」＝宣稱就在高點。"""
    state = {"metrics": {"BTC": {"current_price": 100000}}}
    out = _format_data_for_prompt(state)
    ln = _line(out, "距期內高")
    assert "0.0%" not in ln, f"距高點缺料被講成就在高點：{ln}"
    assert "n/a" in ln or "讀不出來" in ln, ln


def test_pattern_trend_missing_change_is_not_reported_as_flat():
    """型態區缺 change_pct → 舊碼印「(+0.00%)」＝宣稱這個時框零漲跌。"""
    state = {"pattern_btc": {"consensus": "up",
                             "by_tf": {"4h": {"trend": {"direction": "up"}}}}}
    out = _format_data_for_prompt(state)
    ln = _line(out, "- 4h:")   # ⚠️ 用 "4h" 會撞到 ETF 那行的 "24h"
    assert "trend=up" in ln, ln
    assert "+0.00%" not in ln, f"漲跌幅缺料被講成 0：{ln}"
    assert "n/a" in ln or "讀不出來" in ln, ln


# ══════════════════════════════════════════════════════════════════════
# 反向側守門：改動前後都必須綠。來源明講的 0 仍是答案；資料齊全時逐字不變。
# ══════════════════════════════════════════════════════════════════════

def test_genuine_zero_stays_a_definite_zero():
    """⛔ 邊界線：來源真的回 0，那就是「確定為 0」，不可為保險打成 n/a。"""
    state = _complete_state(
        basis_btc={"basis_pct": 0.0, "interpretation": "平水"},
        etf_btc={"cumulative_7d_flow_usd": 0.0, "latest_24h_flow_usd": 0.0},
        whales={"per_symbol_aggregate": [{"symbol": "ETH", "net_long_pct": 0.0,
                                          "total_usd": 0.0}]},
        options_btc={"total_oi_usd": 0.0, "weighted_24h_change_pct": 0.0},
    )
    out = _format_data_for_prompt(state)
    assert "基差 +0.0000%" in _line(out, "- BTC: 基差")
    assert "7d $+0.0M" in _line(out, "BTC ETF")
    assert "淨多 +0%" in _line(out, "淨多")
    assert "24h +0.00%" in _line(out, "總 OI")
    assert "n/a" not in out, f"真實的 0 被誤打成未知：\n{out}"


def test_full_data_output_is_byte_identical_to_old_behaviour():
    """⛔ 反向側守門：資料齊全時，每一行都必須與舊碼逐字相同。"""
    state = _complete_state(
        funding_outliers={"hottest": [{"symbol": "DOGE", "funding_pct_8h": 0.123}],
                          "coldest": [{"symbol": "PEPE", "funding_pct_8h": -0.456}]},
        liq_scan={"items": [{"symbol": "BTC", "total_24h": 120e6, "imbalance": 0.42}]},
        whales={"per_symbol_aggregate": [{"symbol": "ETH", "net_long_pct": 63.4,
                                          "total_usd": 88e6}]},
        cycle={"pi_cycle": {"distance_pct": -22.5, "signal": "neutral"}},
        pattern_btc={"consensus": "up",
                     "by_tf": {"1h": {"trend": {"direction": "up", "change_pct": 1.25},
                                      "sr": {"supports": [{"price": 99000,
                                                           "distance_pct": -1.0}]}}}},
    )
    out = _format_data_for_prompt(state)
    assert "- BTC: $100000  7d +3.2%  距期內高 -4.5%  50d MA $95000" in out
    assert "- BTC: 基差 +0.1234%  (正價差)" in out
    assert "- BTC ETF: 7d $+250.0M  24h $-30.0M" in out
    assert "- 過熱 Top 5: DOGE=+0.123%" in out
    assert "- 過冷 Top 5: PEPE=-0.456%" in out
    assert "- BTC: $120.0M  imbalance +0.42" in out
    assert "- ETH: 淨多 +63%  總倉 $88.0M" in out
    assert "- BTC: 總 OI $32.50B  24h +5.67%" in out
    assert "- Pi Cycle: 距 350d×2 -22.5%  signal=neutral" in out
    assert "- 1h: trend=up (+1.25%)" in out
    assert "   支撐: $99000(-1.0%)" in out
    assert "n/a" not in out, f"資料齊全卻出現 n/a：\n{out}"


def test_explicit_error_dicts_are_still_skipped_entirely():
    """⛔ 反向側守門：明講 error 的分量仍然整段跳過，不因本次改動變成 n/a 洗版。"""
    state = {
        "eth_btc_ratio": 0.052,
        "metrics": {"BTC": {"error": "boom"}, "ETH": {"error": "boom"},
                    "SOL": {"error": "boom"}, "SUI": {"error": "boom"},
                    "WLFI": {"error": "boom"}},
        "basis_btc": {"error": "boom"}, "basis_eth": {"error": "boom"},
        "etf_btc": {"error": "boom"}, "etf_eth": {"error": "boom"},
        "liq_scan": {"error": "boom"},
        "whales": {"error": "boom"},
        "options_btc": {"error": "boom"}, "options_eth": {"error": "boom"},
        "cycle": {"error": "boom"},
        "funding_outliers": {"error": "boom"},
        "pattern_btc": {"error": "boom"},
    }
    out = _format_data_for_prompt(state)
    assert "boom" not in out
    assert "n/a" not in out, f"error dict 被改成缺料 n/a：\n{out}"


def test_empty_state_produces_a_prompt_instead_of_an_exception():
    """整份 state 空（上游全掛）→ 仍要生得出 prompt，而不是整份簡報停播。"""
    out = _format_data_for_prompt({})
    assert isinstance(out, str) and len(out) > 0
