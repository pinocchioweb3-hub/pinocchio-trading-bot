"""v226（監督員 r119）：Hourly Pulse 餵給 LLM 的那份數據表，來源拉取失敗
不再被寫成「今日 ETF 淨流入 $0.0M」這種**有意義但假的**市場宣稱。

同物種第 46 次。落點是 v225 的下一格：v225 治的是 daily macro 的
`_format_data_for_prompt()`，本次治同一支檔案的 `_format_pulse_data()`
（hourly pulse 唯一的 prompt 組裝點）。

【為什麼這一次比 v225 更要緊】v225 的下場是「整段消失」＝LLM 讀到沉默；
本次的下場是 **LLM 讀到一個具體數字**：

    ## ETF 即時
    - BTC: 今日 $+0.0M  近 3d 累計 $+0.0M
    - ETH: 今日 $+0.0M  近 3d 累計 $+0.0M

沉默還可能被 LLM 忽略，`$+0.0M` 則會被當成「今天機構沒有進出」寫進使用者
每小時讀的那張卡。**沒拉到 → 講成一個看起來像量測結果的數字**，這是所有
45 次同物種裡對讀者傷害最直接的形狀之一。

【根因是兩段程式接縫處的一個空 dict】
  macro.py:342-353  `etf_btc_today = {}`；只有成功才填值，失敗時**留空 dict**。
  synthesizer.py:559 `if d.get("error"): continue` —— 空 dict 沒有 error 旗標
                     ⇒ 守門不會觸發 ⇒ 落到 :560 `d.get("today_flow_usd", 0)`
                     ⇒ 0 ⇒ 印成 `$+0.0M`。
守門本身寫得沒錯，是**上游遞了一個它認不得的失敗表示法**給它。

【可達性（實測，非假設）】2026-08-02 15:07 UTC 實跑：
  - daemon 實際用的後端是 coinglass（run_bot.py:492 `--backend` 預設值；
    start_bot.ps1 不帶此參數）。⚠️ 裸 CLI 不設 MARKET_INTEL_BACKEND 會拿到
    mock（settings.py:19 預設），得到的錯誤碼不是線上那個——本檔的結論只依賴
    「任何 error ⇒ {} ⇒ 印 $+0.0M」這個**與錯誤碼無關**的形狀。
  - `mi_get_etf_flows("BTC"/"ETH")` 當下回 error ⇒ `etf_*_today` 實測為 `{}`
    ⇒ 把該形狀餵進改動前的 HEAD，逐字印出上面那兩行 `$+0.0M`。
  - hourly pulse 是活的：pulse_state.json ts=2026-08-02 14:56 UTC，
    macro.py:842-857 每 3600 秒 compute → synthesize → 送 Telegram。
  - CoinGlass 方案 7/08 到期（memory: coinglass-plan-outage-2026-07-08）
    ⇒ ETF 這條路徑**只有 CoinGlass 一個來源**（sources/coinglass.py:716，
    免費源沒有對應實作）。

【另外兩個同一支函式裡的破口，一併釘住】
  ② `flow_recent` 從頭到尾沒有人填過（macro.py:298 建成 `{}`、:359 原樣回傳，
     全 repo 僅此二處）⇒「## 24h 主動買賣力量」這個標題**每一輪**底下都是空的
     ＝LLM 讀到「這輪主動買賣沒什麼好講」，而事實是這一項從來沒有被計算過。
  ③ liq_today／whales_now／sentiment_now 失敗時**連標題都不印**（v225 同形）。

⛔ 邊界線（沿用 v208–v216、v225）：
  1. 來源明講的 0 仍是答案，照印，不得加缺料標記
     （ETF 真的零流入是常見且有意義的事實，誤標成缺料會反過來騙人）。
  2. 資料齊全時每一行必須與舊碼**逐字相同**。
  3. 不得把來源原始錯誤訊息（可能含 URL／憑證片段）印進 prompt；repo 是 PUBLIC。
  4. 鍵根本不在 state（上游沒跑這一項）≠ 明講失敗 ⇒ 保持安靜，不可宣稱失敗。
"""
from __future__ import annotations

import datetime as dt

from l3_dispatcher.synthesizer import _format_pulse_data

MARK = "本輪拉取失敗"

_TS = dt.datetime(2026, 8, 2, 15, 0, tzinfo=dt.timezone.utc)


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


def _full_state() -> dict:
    """一份每一項都成功的 pulse_state（反向側的基準）。"""
    return {
        "ts": _TS,
        "price_deltas": {"BTC": {"current": 63035.0, "change_1h_pct": -0.1,
                                 "change_24h_pct": 0.5, "change_3d_pct": 1.2,
                                 "change_1w_pct": -2.0,
                                 "high_24h": 63500.0, "low_24h": 62500.0}},
        "flow_recent": {"BTC": {"cvd_slope_24h": 0.42, "buy_sell_ratio_24h": 1.08}},
        "liq_today": {"items": [{"symbol": "BTC", "total_24h": 12_000_000,
                                 "imbalance": 0.45}]},
        "etf_btc_today": {"today_flow_usd": 120_000_000,
                          "cumulative_3d_flow_usd": 300_000_000},
        "etf_eth_today": {"today_flow_usd": -20_000_000,
                          "cumulative_3d_flow_usd": -50_000_000},
        "funding_changes": {"BTC": {"current": 0.0001, "predicted": 0.00012,
                                    "change_24h_pct_points": 0.00002}},
        "whales_now": {"per_symbol_aggregate": [{"symbol": "BTC",
                                                 "net_long_pct": 62.0,
                                                 "total_usd": 900_000_000}]},
        "sentiment_now": {"fear_greed_now": 55, "fear_greed_label": "Neutral"},
    }


# ══════════════════════════════════════════════════════════════════════
# 正向側：拉取失敗被講成數字／被靜靜吃掉（以下每條在改動前的 HEAD 上都是紅的）
# ══════════════════════════════════════════════════════════════════════

def test_etf_empty_dict_is_not_reported_as_zero_flow():
    """⭐ 本次核心：macro.py 失敗時遞來的空 dict，舊碼印成 `今日 $+0.0M`。

    這是 2026-08-02 當下**線上每小時都在發生**的實況形狀。
    """
    st = _full_state()
    st["etf_btc_today"] = {}
    st["etf_eth_today"] = {}
    sec = _section(_format_pulse_data(st), "ETF")
    assert "$+0.0M" not in sec, f"把沒拉到講成零流入：\n{sec}"
    for sym in ("BTC", "ETH"):
        assert any(sym in ln and MARK in ln for ln in sec.split("\n")), sec


def test_etf_explicit_error_is_not_reported_as_zero_flow():
    """來源明講 error 時同樣不得折成 $0.0M。"""
    st = _full_state()
    st["etf_btc_today"] = {"error": "boom"}
    sec = _section(_format_pulse_data(st), "ETF")
    assert "BTC" in sec and MARK in sec, sec
    assert "$+0.0M" not in sec.split("\n")[1], sec


def test_price_delta_failure_is_visible():
    """價格分量失敗：舊碼整列消失（標題底下可能一列都沒有）。"""
    st = _full_state()
    st["price_deltas"] = {"BTC": {"error": True}}
    sec = _section(_format_pulse_data(st), "即時價格")
    assert any("BTC" in ln and MARK in ln for ln in sec.split("\n")), sec


def test_funding_failure_is_visible():
    st = _full_state()
    st["funding_changes"] = {"BTC": {"error": True}}
    sec = _section(_format_pulse_data(st), "Funding")
    assert any("BTC" in ln and MARK in ln for ln in sec.split("\n")), sec


def test_liquidation_failure_keeps_section_and_says_so():
    """舊碼：liq 失敗 ⇒ 連「## 今日清算」這個標題都不存在。"""
    st = _full_state()
    st["liq_today"] = {"error": "boom"}
    sec = _section(_format_pulse_data(st), "清算")
    assert MARK in sec, sec


def test_whales_failure_keeps_section_and_says_so():
    st = _full_state()
    st["whales_now"] = {"error": "boom"}
    sec = _section(_format_pulse_data(st), "鯨魚")
    assert MARK in sec, sec


def test_sentiment_failure_keeps_section_and_says_so():
    st = _full_state()
    st["sentiment_now"] = {"error": "boom"}
    sec = _section(_format_pulse_data(st), "情緒")
    assert MARK in sec, sec


def test_empty_section_is_not_left_as_a_bare_header():
    """⭐ `flow_recent` 從來沒有人填過 ⇒ 標題底下永遠是空的。

    空標題 ＝「這輪沒什麼好講」，但事實是這一項根本沒被算過。
    """
    st = _full_state()
    st["flow_recent"] = {}
    sec = _section(_format_pulse_data(st), "主動買賣力量")
    body = [ln for ln in sec.split("\n")[1:] if ln.strip()]
    assert body, f"標題底下一列都沒有：\n{sec}"


def test_missing_numeric_key_is_not_folded_to_zero():
    """分量本身沒失敗、但缺某個數字鍵：舊碼 `.get(k, 0)` 折成 0。

    `CVD 24h 斜率=+0.000` ＝「多空力量相抵」，是一個沒發生過的量測。
    """
    st = _full_state()
    st["flow_recent"] = {"BTC": {"buy_sell_ratio_24h": 1.08}}  # 缺 cvd_slope_24h
    sec = _section(_format_pulse_data(st), "主動買賣力量")
    assert "+0.000" not in sec, f"缺的鍵被折成 0：\n{sec}"


# ══════════════════════════════════════════════════════════════════════
# 反向側：守門不得誤傷（以下每條在改動前的 HEAD 上就必須是綠的）
# ══════════════════════════════════════════════════════════════════════

def test_real_zero_etf_flow_is_still_printed_as_zero():
    """⛔ 來源明講「今日零流入」是答案，不是缺料——誤標會反過來騙人。"""
    st = _full_state()
    st["etf_btc_today"] = {"today_flow_usd": 0, "cumulative_3d_flow_usd": 0}
    sec = _section(_format_pulse_data(st), "ETF")
    btc = [ln for ln in sec.split("\n") if ln.startswith("- BTC")][0]
    assert "$+0.0M" in btc, btc
    assert MARK not in btc, btc


def test_full_data_rows_are_unchanged():
    """資料齊全時，每一列都必須與舊碼逐字相同（不得有任何缺料標記）。"""
    out = _format_pulse_data(_full_state())
    assert MARK not in out, out
    assert "- BTC: $63035.0  1h=-0.10% 24h=+0.50% 3d=+1.20% 1w=-2.00%  24h 高/低: $63500.0/$62500.0" in out, out
    assert "- BTC: CVD 24h 斜率=+0.420  taker buy/sell=1.08" in out, out
    assert "- BTC: 今日 $+120.0M  近 3d 累計 $+300.0M" in out, out
    assert "- ETH: 今日 $-20.0M  近 3d 累計 $-50.0M" in out, out


def test_raw_error_text_never_leaks_into_prompt():
    """⛔ repo 是 PUBLIC：來源原始錯誤字串可能含 URL／憑證片段。"""
    secret = "https://open-api.coinglass.com/x?key=SECRET123"
    st = _full_state()
    st["etf_btc_today"] = {"error": secret}
    st["liq_today"] = {"error": secret}
    st["whales_now"] = {"error": secret}
    out = _format_pulse_data(st)
    assert "SECRET123" not in out and "coinglass.com" not in out, out


def test_absent_key_stays_quiet():
    """鍵根本不在 state（上游沒跑這一項）≠ 明講失敗 ⇒ 不得宣稱失敗。"""
    st = _full_state()
    del st["whales_now"]
    del st["sentiment_now"]
    out = _format_pulse_data(st)
    assert "鯨魚" not in out, out
    assert "情緒" not in out, out
