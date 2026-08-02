"""v225（監督員 r118）：Daily Macro 餵給 LLM 的那份數據表，來源**明講拉取失敗**
的分量不再靜靜消失。

同物種第 45 次。落點與 v216（同一支函式 `_format_data_for_prompt()`）相鄰但**不同
形狀**：v216 治的是「鍵在、值是 None」被折成 0；本次治的是「來源明講 error」
⇒ `if x.get("error"): continue` ⇒ 那一行／那一整段**從 prompt 裡整個不見**。

為什麼這比缺一個數字更要緊：消失之後，LLM 讀到的是

    ## 現貨倉位（SUI/WLFI）
    （下面什麼都沒有）

＝「這兩個標的這輪沒有值得講的事」，而不是「這兩個標的的數據根本沒拉到」。
更嚴重的是整段消失的那幾區（Funding 極端值／清算／鯨魚／情緒估值／週期／型態／
OKX 公告）——LLM 連「有這一區」都不知道，等於那些面向**在報告裡不存在**。
而這份 prompt 寫出去的每一行都會被 LLM 當既成事實寫進使用者每天讀的那張宏觀卡。

【可達性（實測，非假設）】2026-08-02 當下實跑 `fetch_macro_metrics`：BTC/ETH/SOL/
SUI/WLFI 五個標的**全部**回 `error="Upgrade plan"`（CoinGlass 方案 7/08 到期，
memory: coinglass-plan-outage-2026-07-08），因此把當日形狀餵進舊碼，整份數據表
只剩 141 個字元、五個標題底下**一列都沒有**、另外八區完全不存在。daily macro
當天仍照常送出（daily_macro_state.json last_sent_ts = 當日 17:40）。

⛔ 邊界線（沿用 v208/v210/v212/v213/v214/v215/v216）：
  1. 來源明講的 0 仍是答案，照印，不得加缺料標記。
  2. 資料齊全時每一行必須與舊碼**逐字相同**。
  3. 不得把來源的原始錯誤訊息（可能含 URL／憑證片段）印進 prompt——只講
     「這一格沒有數據」，不講它為什麼壞（LLM 也用不到，且 repo 是 PUBLIC）。
  4. 鍵根本不在 state（上游沒跑這一項）≠ 明講失敗 ⇒ 保持安靜，不可宣稱失敗。
     tradfi=None 同理（呼叫端可能本來就沒要拉）。
"""
from __future__ import annotations

from l3_dispatcher.synthesizer import _format_data_for_prompt

MARK = "本輪拉取失敗"


def _section(text: str, header: str) -> str:
    """取出某個 ## 標題底下、到下一個 ## 之前的那一段（含標題）。"""
    out, hit = [], False
    for ln in text.split("\n"):
        if ln.startswith("## "):
            if hit:
                break
            hit = header in ln
        if hit:
            out.append(ln)
    if not hit:
        raise AssertionError(f"輸出中找不到標題 {header!r}；全文：\n{text}")
    return "\n".join(out)


def _err(msg: str = "boom") -> dict:
    return {"error": msg}


# ══════════════════════════════════════════════════════════════════════
# 正向側：來源明講失敗 → 舊碼整段消失（以下每條在改動前的 HEAD 上都是紅的）
# ══════════════════════════════════════════════════════════════════════

def test_indicator_layer_fetch_failure_is_visible():
    """BTC/ETH/SOL 全掛（＝2026-08-02 當下實況）：舊碼標題底下一列都沒有。"""
    state = {"metrics": {s: _err() for s in ("BTC", "ETH", "SOL")}}
    sec = _section(_format_data_for_prompt(state), "加密貨幣指標層")
    for sym in ("BTC", "ETH", "SOL"):
        assert any(sym in ln and MARK in ln for ln in sec.split("\n")), sec


def test_spot_layer_fetch_failure_is_visible():
    """現貨倉位（使用者自己的倉）拉不到時，不可讀成「這輪沒事」。"""
    state = {"metrics": {"SUI": _err(), "WLFI": _err()}}
    sec = _section(_format_data_for_prompt(state), "現貨倉位")
    for sym in ("SUI", "WLFI"):
        assert any(sym in ln and MARK in ln for ln in sec.split("\n")), sec


def test_basis_fetch_failure_is_visible():
    state = {"basis_btc": _err(), "basis_eth": _err()}
    sec = _section(_format_data_for_prompt(state), "期現基差")
    assert sec.count(MARK) == 2, sec


def test_etf_fetch_failure_is_visible():
    """ETF 流向消失 ≠ 機構沒進出。"""
    state = {"etf_btc": _err(), "etf_eth": _err()}
    sec = _section(_format_data_for_prompt(state), "ETF 機構流向")
    assert sec.count(MARK) == 2, sec


def test_options_fetch_failure_is_visible():
    state = {"options_btc": _err(), "options_eth": _err()}
    sec = _section(_format_data_for_prompt(state), "期權市場 OI")
    assert sec.count(MARK) == 2, sec


def test_whole_section_failures_still_appear_with_a_marker():
    """這八區在舊碼是**連標題都不見**——LLM 連「有這一區」都不知道。"""
    state = {
        "funding_outliers": _err(), "liq_scan": _err(), "whales": _err(),
        "sentiment": _err(), "cycle": _err(), "okx_news": _err(),
        "pattern_btc": _err(),
    }
    out = _format_data_for_prompt(state)
    for header in ("Funding 極端值", "24h 清算", "鯨魚", "情緒/估值",
                   "BTC 週期指標", "OKX 官方公告", "BTC 多時框型態"):
        assert MARK in _section(out, header), f"{header} 沒有缺料標記：\n{out}"


def test_tradfi_ticker_failure_is_visible():
    """個別 ticker 掛掉：舊碼那一列直接消失（整張跨資產表看起來就是「只有這幾檔」）。"""
    tradfi = {"items": {"^GSPC": {"name": "S&P 500", "current": 5000,
                                  "change_1d_pct": 0.5, "change_7d_pct": 1.0,
                                  "change_30d_pct": 2.0},
                        "^VIX": _err()}}
    sec = _section(_format_data_for_prompt({}, tradfi), "傳統金融")
    assert "5000" in sec, sec
    assert any("^VIX" in ln and MARK in ln for ln in sec.split("\n")), sec


def test_error_message_text_is_never_leaked_into_the_prompt():
    """⛔ 只講「沒有數據」，不講來源的原始錯誤字串（PUBLIC repo／對 LLM 無用）。"""
    state = {"metrics": {"BTC": {"error": "https://secret.example/x?key=AAA"}},
             "cycle": {"error": "Upgrade plan"}}
    out = _format_data_for_prompt(state)
    assert "secret.example" not in out and "key=AAA" not in out, out
    assert "Upgrade plan" not in out, out
    assert MARK in out


# ══════════════════════════════════════════════════════════════════════
# 反向側守門：改動前後都必須綠
# ══════════════════════════════════════════════════════════════════════

def test_full_data_output_is_byte_identical_and_carries_no_marker():
    """⛔ 資料齊全時逐字不變，且一個缺料標記都不許出現。"""
    state = {
        "eth_btc_ratio": 0.052,
        "metrics": {"BTC": {"current_price": 100000, "return_7d_pct": 3.21,
                            "drawdown_from_high_pct": -4.5, "ma50": 95000}},
        "extras": {"BTC": {"funding": 0.0001}},
        "basis_btc": {"basis_pct": 0.1234, "interpretation": "正價差"},
        "etf_btc": {"cumulative_7d_flow_usd": 250e6, "latest_24h_flow_usd": -30e6},
        "options_btc": {"total_oi_usd": 32.5e9, "weighted_24h_change_pct": 5.67},
        "liq_scan": {"items": [{"symbol": "BTC", "total_24h": 120e6,
                                "imbalance": 0.42}]},
        "whales": {"per_symbol_aggregate": [{"symbol": "ETH", "net_long_pct": 63.4,
                                             "total_usd": 88e6}]},
    }
    out = _format_data_for_prompt(state)
    assert "- BTC: $100000  7d +3.2%  距期內高 -4.5%  50d MA $95000" in out
    assert "- BTC: 基差 +0.1234%  (正價差)" in out
    assert "- BTC ETF: 7d $+250.0M  24h $-30.0M" in out
    assert "- BTC: 總 OI $32.50B  24h +5.67%" in out
    assert "- BTC: $120.0M  imbalance +0.42" in out
    assert "- ETH: 淨多 +63%  總倉 $88.0M" in out
    assert MARK not in out, f"資料齊全卻報缺料：\n{out}"


def test_genuine_zero_never_becomes_a_fetch_failure():
    """⛔ 來源明講的 0 是答案，不可被標成拉取失敗。"""
    state = {"etf_btc": {"cumulative_7d_flow_usd": 0.0, "latest_24h_flow_usd": 0.0},
             "whales": {"per_symbol_aggregate": [{"symbol": "ETH", "net_long_pct": 0.0,
                                                  "total_usd": 0.0}]}}
    out = _format_data_for_prompt(state)
    assert "7d $+0.0M" in out and "淨多 +0%" in out
    assert MARK not in out, out


def test_absent_keys_stay_silent():
    """⛔ 鍵根本不在（上游沒跑這一項）≠ 明講失敗 ⇒ 不可宣稱拉取失敗。"""
    out = _format_data_for_prompt({})
    assert isinstance(out, str) and len(out) > 0
    assert MARK not in out, out


def test_tradfi_none_stays_silent():
    """⛔ tradfi=None ＝呼叫端可能本來就沒要拉 ⇒ 不宣稱失敗。"""
    out = _format_data_for_prompt({}, None)
    assert "傳統金融" not in out, out
