"""LLM-driven 宏觀分析合成器（Anthropic Claude）。

從 macro_state（所有原始數據）合成出**敘事化、串聯式、跨資產**的市場分析。
取代公式化的 render_macro_report。

成本控制：
    - 預設 claude-sonnet-4-6（~$0.12/次）
    - 重大事件用 claude-opus-4-8（~$0.60/次，自動觸發）
    - 缺 ANTHROPIC_API_KEY 時 → fallback 回原本 template renderer

頻率建議：每 4 小時 1 次（不是每 1 小時），避免燒錢。
"""
from __future__ import annotations

import datetime as dt
import json
import os
from typing import Any

try:
    from anthropic import AsyncAnthropic
    _SDK_OK = True
except ImportError:
    _SDK_OK = False
    AsyncAnthropic = None  # type: ignore


# 模型 ID
MODEL_OPUS = "claude-opus-4-8"
MODEL_SONNET = "claude-sonnet-4-6"
MODEL_HAIKU = "claude-haiku-4-5-20251001"

DEFAULT_MODEL = MODEL_SONNET


# ====================== Daily Macro Prompt（每天 08:00 一次） ======================
DAILY_MACRO_PROMPT = """你是一位資深加密貨幣宏觀策略分析師，正在為一位專業期貨交易者撰寫深度市場簡報。

交易者背景：
- 帳戶 $10K USDT 永續期貨
- 目標每天 +$100，月 5-10% 報酬
- 用 Wyckoff 吸籌、軋空、左側埋伏策略
- 不要泛泛建議，要具體數字、因果脈絡、可監控訊號

# 你必須遵守的寫作鐵律

**禁止：**
- ❌ 「市場可能上漲」「需要謹慎」「整體偏向中性」這類空話
- ❌ 列數據清單（用戶看得到原始數據）
- ❌ 公式化判斷（A 高所以 B 低）
- ❌ 過度樂觀或恐懼煽動

**必須：**
- ✅ 用具體數字支持每個論點（$XX、±X.XX%、X.XX 倍）
- ✅ 串聯因果：「因為 X，所以 Y，這意味著 Z」
- ✅ 跨資產對照：crypto vs 美股、債息、美元、黃金、VIX
- ✅ 識別「不對勁」之處：哪裡有背離、哪裡有共振
- ✅ 具體可監控的數據點 + 觸發條件

# 輸出格式（HTML，給 Telegram 用）— v17 雙層結構，嚴格遵守

## 第一層「掃讀層」（不點開就看到，總長 ≤ 600 中文字）

第 1 行：🟢/🟡/🔴 風險燈號 + <b>一句話結論</b>（今天市場到底什麼狀態）

<b>📟 市場現況儀表板</b>
（固定順序、每行一個資產類別、只放數字不解釋 — 讀者每天看同位置）
₿ 加密：BTC <code>$XX,XXX</code>（24h ±X.X%）｜ETH <code>$X,XXX</code>｜恐貪 <code>XX</code>
📈 美股：SPX/QQQ 24h ±X.X%｜VIX <code>XX.X</code>
💵 美元債息：DXY <code>XXX.X</code>｜10Y <code>X.XX%</code>
🥇 黃金：<code>$X,XXX</code>（±X.X%）
（若數據缺失該行寫「—」，不要編造）

<b>🔗 因果邏輯</b>
3-5 句講清楚「為什麼是現在這樣」：Fed/流動性 → 美股風險偏好 → crypto。
指出今天最重要的**一個背離或共振**。

<b>🎯 今日操作含義</b>
1-2 句具體傾向 + 2-3 個觀察閾值（指標、當前值、突破意義）。

## 第二層「展開層」（細節控才點開）

用一個 <blockquote expandable> 包住以下全部內容（≤ 1200 字）：
- 跨資產詳細對照與背離分析
- 機構與鯨魚動向（ETF 流向規模、Hyperliquid 倉位、期權 OI）
- 消息面與下週催化劑
- 風險清單完整版（每個風險：觸發條件/影響/對應動作）
- 完整觀察清單

# 鐵律
- 第一層絕不超過 600 字 — 超過就是失敗
- 儀表板行格式固定，不要自由發揮
- 使用 <b>、<i>、<code>、<blockquote expandable> HTML 標籤，不用 markdown
- expandable 引用塊全篇只能有一個，放最後

# 給你的數據
（之後在 user message 給你）"""


# 向後相容
SYSTEM_PROMPT = DAILY_MACRO_PROMPT


# ====================== Hourly Pulse Prompt（每小時） ======================
HOURLY_PULSE_PROMPT = """你是專業交易監視員，每小時報告市場「即時動態」。

**核心規則：絕對不要重複日線級別的敘事**（那是早上 08:00 的 daily macro 任務）。
**聚焦在：過去 1h、24h、3d、1w 內 *發生了什麼變化*、什麼新進的力量、什麼結束的力量。**

# 你必須遵守的寫作鐵律

**禁止：**
- ❌ 重複「BTC 在 bear_deleveraging 階段」這種日線級別判斷
- ❌ 重複「距 ATH 跌 50%」這種跨月份的脈絡
- ❌ 籠統說「市場震盪」「需要觀察」
- ❌ 講「過去 30/60/90 天」這些是 daily macro 的事

**必須：**
- ✅ 用 1h、24h、3d、1w 的具體變動數字
- ✅ 點出「過去 N 小時新發生的事」（反彈、破位、爆量、機構進場）
- ✅ 識別資金流向變化（CVD 轉向、ETF 翻盤、鯨魚動作）
- ✅ 短期內可觀察的具體價位/閾值

# 輸出格式（HTML for Telegram）

<b>⚡ 1. 過去 1 小時即時動態</b>
（2-3 句：發生什麼價/量/資金流變化）

<b>📊 2. 24h-3d 結構演變</b>
（反彈了？破位了？盤整了？用具體區間/百分比）
- 過去 24h 高 vs 低 vs 現在
- 過去 3d 趨勢
- 1w 結構（簡述，不展開敘事）

<b>🏛 3. 機構/鯨魚即時動向</b>
（24h 內 ETF 進/出、Hyperliquid 鯨魚新進/減倉、選擇權 OI 變化）

<b>💧 4. 流動性與情緒變化</b>
（funding 上升/下降、liquidation 失衡、F&G 變化、有無關鍵新聞）

<b>⏰ 5. 下個小時觀察重點</b>
（1-2 句：等什麼價位/事件）

# 長度與格式（v17 雙層）
- 掃讀層 = 第 1-2 段 + 觀察重點，總長 ≤ 300 中文字
- 第 3-4 段（機構/流動性細節）整段包進一個 <blockquote expandable>（≤ 400 字）
- HTML 標籤：<b>, <i>, <code>, <blockquote expandable>
- 緊湊、無冗餘；若這小時「沒有顯著變化」，直接輸出單行：
  「⚪ 過去 1h 無顯著變化（BTC ±X.X%，無新資金流事件）」— 不要硬寫

# 給你的數據
（之後在 user message 給你 delta-focused 資料）"""


# ====================== Per-Symbol Deep Dive Prompt（每 6 小時，每個強勢幣一份） ======================
PER_SYMBOL_DEEPDIVE_PROMPT = """你是專業加密貨幣交易計畫師。針對指定的單一標的，根據完整的多時框數據與市場結構，產出一份**可立即執行的交易計畫**。

# 你的任務（複製真人交易員的工作流程）

使用者過往的做法是：
1. 打開 CoinGlass App，把該幣的週/日/4h/1h/15m K 線圖 + 多空比 + 持倉 + 清算等截 20 張圖
2. 把所有圖丟給 AI 分析「打底完成？大戶吸籌？多空比偏多？」
3. 拿到「進場價、SL、TP1/2/3、現價或限價」的具體計畫

你現在要做**完全一樣的事**，但用結構化數據代替截圖。

# 你必須輸出的內容

<b>🎯 {SYMBOL} 交易計畫</b>

<b>1. 型態結論（綜合 5 時框）</b>
- 週線結構：上升 / 下降 / 盤整？吸籌 phase A/B/C/D/E？
- 日線結構：HH/HL 還是 LH/LL？有打底完成跡象嗎？
- 4h 結構：是否在關鍵 S/R 區？有 Break of Structure 嗎？
- 1h/15m：進場時機？

<b>2. 數據面確認</b>
- 大戶 vs 散戶 持倉比：誰偏多誰偏空？背離了嗎？
- OI 變化：建倉中 / 出清中？
- Funding rate：負/中/熱？
- CVD（如有）：吸籌 / 派發？
- 清算：軋空燃料還是多殺多？
- 鯨魚（Hyperliquid）：壓倒做多/做空/中性？

<b>3. 三重匯合判定</b>
- 流派 A（威科夫 / 高時框）方向：？
- 流派 B（SMC / 中時框）方向：？
- 數據面方向：？
- **三者同向 = 可做、不同向 = 觀望**

<b>4. 交易建議</b>

**若可做單**，必須給：
- **方向**：做多 / 做空
- **進場類型**：現價市價追入 / 限價分批埋伏
- **進場區間**：具體價位 — **優先用 SMC 結構**（OB 底/頂、FVG 區、Swing 點），不是憑空估
- **止損**：具體價位 — **必須基於結構**（OB 邊界、Swing 之上/下、Liquidity 區之外），不是固定 % 距離
- **止盈分批**：TP1 / TP2 / TP3 — **目標是下一個 Liquidity / OB / Swing**，不是固定 R 倍數
- **倉位計算（必算！）**：
  - 1R 價差 = |entry - stop|
  - position_notional = $100 / 1R × entry
  - margin = notional / leverage
  - **驗證 margin ≤ $500**，超出就調高槓桿（最多 15x for BTC/ETH/SOL）
- **建議槓桿**：經上述驗算後決定（5x / 10x / 15x）
- **R:R 報酬比**：TP2 至少 1.5R，TP3 至少 2R，否則 setup 不值得
- **持倉時長預估**：日內 / 1-3 日 / 1 週
- **失效條件**：什麼價位或數據變化 = 立即出場（要基於 SMC 結構）
- **TP 後保護動作**：
  - TP1 觸及 → SL 移到開倉價（保本）
  - TP2 觸及 → SL 移到 TP1 價位（鎖小利）
  - TP3 觸及 → 全平 或 trailing stop 距高/低 1×ATR
- **時段風險**：若進場時為亞洲深夜或週末（流動性薄）→ 縮小倉位 30%

**若不適合做單**，必須給：
- **在等什麼具體條件**？（如「等 4h 收回 $63,800 站穩」、「等 funding 跌破 +0.1%」）
- **預期等待時長**：幾小時 / 幾天
- **目前 setup 缺什麼**：哪些條件已滿足、哪些還沒

<b>5. 風險警示</b>
3 個最大風險，每個含「觸發條件 + 應對動作」。

# 寫作風格
- 具體數字 > 模糊形容
- 因果脈絡 > 列數據
- 可執行 > 教科書
- 長度 800-1500 字
- HTML 標籤：<b>, <i>, <code>

# 給你的數據
（之後在 user message 給你 5 時框 + 全數據）"""


def _format_data_for_prompt(state: dict, tradfi: dict | None = None,
                            watchlist=None) -> str:
    """把 state + tradfi 結構化成 LLM 易讀的格式"""
    ts = state.get("ts")
    ts_str = ts.strftime("%Y-%m-%d %H:%M UTC") if ts else "current"

    parts = [f"# 時間：{ts_str}\n"]

    # ----- 指標層 -----
    parts.append("## 加密貨幣指標層（BTC/ETH/SOL）")
    metrics = state.get("metrics", {})
    extras = state.get("extras", {})
    for sym in ("BTC", "ETH", "SOL"):
        m = metrics.get(sym, {})
        e = extras.get(sym, {})
        if m.get("error"): continue
        line = f"- {sym}: ${m.get('current_price')}"
        for n in (7, 30, 90):
            r = m.get(f"return_{n}d_pct")
            if r is not None: line += f"  {n}d {r:+.1f}%"
        line += f"  距期內高 {m.get('drawdown_from_high_pct', 0):.1f}%"
        if m.get("ma50"): line += f"  50d MA ${m.get('ma50')}"
        if e.get("funding") is not None:
            line += f"  funding {e['funding']*100:+.4f}%/8h"
        parts.append(line)
    parts.append(f"- ETH/BTC ratio: {state.get('eth_btc_ratio')}")
    parts.append(f"- 系統判定 regime: {state.get('regime')}")

    # ----- 現貨層 -----
    parts.append("\n## 現貨倉位（SUI/WLFI）")
    for sym in ("SUI", "WLFI"):
        m = metrics.get(sym, {})
        e = extras.get(sym, {})
        if m.get("error"): continue
        line = f"- {sym}: ${m.get('current_price')}  7d {m.get('return_7d_pct','—')}%  30d {m.get('return_30d_pct','—')}%"
        if e.get("funding") is not None:
            line += f"  funding {e['funding']*100:+.4f}%/8h"
        parts.append(line)

    # ----- 期現基差 -----
    parts.append("\n## 期現基差")
    for sym in ("BTC", "ETH"):
        b = state.get(f"basis_{sym.lower()}", {})
        if b.get("error"): continue
        parts.append(f"- {sym}: 基差 {b.get('basis_pct', 0):+.4f}%  ({b.get('interpretation','')})")

    # ----- ETF 流向 -----
    parts.append("\n## ETF 機構流向（7d 累計）")
    for sym, key in (("BTC", "etf_btc"), ("ETH", "etf_eth")):
        etf = state.get(key, {})
        if etf.get("error"): continue
        c7 = etf.get("cumulative_7d_flow_usd", 0)
        d24 = etf.get("latest_24h_flow_usd", 0)
        parts.append(f"- {sym} ETF: 7d ${c7/1e6:+.1f}M  24h ${d24/1e6:+.1f}M")

    # ----- Funding 極端值 -----
    fo = state.get("funding_outliers", {})
    if not fo.get("error"):
        parts.append("\n## Funding 極端值")
        hot = fo.get("hottest", [])[:5]
        cold = fo.get("coldest", [])[:5]
        if hot:
            hot_str = ", ".join(f"{h['symbol']}={h['funding_pct_8h']:+.3f}%" for h in hot)
            parts.append(f"- 過熱 Top 5: {hot_str}")
        if cold:
            cold_str = ", ".join(f"{c['symbol']}={c['funding_pct_8h']:+.3f}%" for c in cold)
            parts.append(f"- 過冷 Top 5: {cold_str}")

    # ----- 清算 + 鯨魚 -----
    liq = state.get("liq_scan", {})
    if not liq.get("error"):
        items = liq.get("items", [])[:5]
        parts.append("\n## 24h 清算 Top 5")
        for it in items:
            parts.append(f"- {it.get('symbol')}: ${it.get('total_24h')/1e6:.1f}M  imbalance {it.get('imbalance'):+.2f}")

    whales = state.get("whales", {})
    if not whales.get("error"):
        parts.append("\n## Hyperliquid 鯨魚淨倉位 Top 5")
        for w in whales.get("per_symbol_aggregate", [])[:5]:
            parts.append(f"- {w['symbol']}: 淨多 {w['net_long_pct']:+.0f}%  總倉 ${w['total_usd']/1e6:.1f}M")

    # ----- 期權 -----
    parts.append("\n## 期權市場 OI")
    for sym, key in (("BTC", "options_btc"), ("ETH", "options_eth")):
        o = state.get(key, {})
        if o.get("error"): continue
        parts.append(f"- {sym}: 總 OI ${o.get('total_oi_usd', 0)/1e9:.2f}B  24h {o.get('weighted_24h_change_pct'):+.2f}%")

    # ----- 情緒 + 週期 -----
    sent = state.get("sentiment", {})
    if not sent.get("error"):
        fg = sent.get("fear_greed_now")
        ahr = sent.get("ahr999_now")
        parts.append("\n## 情緒/估值")
        if fg is not None:
            parts.append(f"- Fear & Greed: {fg}  ({sent.get('fear_greed_label', '—')})")
        if ahr is not None:
            parts.append(f"- AHR999: {ahr}  ({sent.get('ahr999_label', '—')})")

    cycle = state.get("cycle", {})
    if not cycle.get("error"):
        parts.append("\n## BTC 週期指標")
        for key, label in [("pi_cycle", "Pi Cycle"), ("puell", "Puell"),
                           ("golden_ratio", "Golden Ratio Multiplier"),
                           ("two_year_ma", "2-Year MA Multiplier")]:
            c = cycle.get(key, {})
            if not c: continue
            if key == "pi_cycle":
                parts.append(f"- {label}: 距 350d×2 {c.get('distance_pct'):+.1f}%  signal={c.get('signal')}")
            elif key == "puell":
                parts.append(f"- {label}: {c.get('value')}  {c.get('label','')}")
            elif key == "golden_ratio":
                parts.append(f"- {label}: {c.get('multiplier')}x  {c.get('label','')}")
            elif key == "two_year_ma":
                parts.append(f"- {label}: {c.get('multiplier')}x  {c.get('label','')}")

    # ----- 傳統金融 -----
    if tradfi and not tradfi.get("error"):
        parts.append("\n## 傳統金融跨資產")
        for ticker, data in tradfi.get("items", {}).items():
            if data.get("error"): continue
            line = f"- {ticker} ({data.get('name','')}): {data.get('current')}  1d {data.get('change_1d_pct'):+.2f}%  7d {data.get('change_7d_pct'):+.2f}%  30d {data.get('change_30d_pct'):+.2f}%"
            parts.append(line)

    # ----- 多時框型態分析 (BTC/ETH/SOL) -----
    for sym, key in [("BTC", "pattern_btc"), ("ETH", "pattern_eth"), ("SOL", "pattern_sol")]:
        pat = state.get(key, {})
        if pat.get("error"): continue
        consensus = pat.get("consensus", "unknown")
        by_tf = pat.get("by_tf", {})
        if not by_tf: continue
        parts.append(f"\n## {sym} 多時框型態（共識={consensus}）")
        for tf in ["1h", "4h", "12h", "1d", "1w"]:
            if tf not in by_tf: continue
            tf_data = by_tf[tf]
            trend = tf_data.get("trend", {})
            sr = tf_data.get("sr", {})
            vp = tf_data.get("volume_price", {})
            patterns = tf_data.get("patterns", [])
            d = trend.get("direction", "?")
            chg = trend.get("change_pct", 0)
            line = f"- {tf}: trend={d} ({chg:+.2f}%)"
            if vp.get("interpretation"):
                line += f"  量價={vp['interpretation']}"
            if patterns:
                line += f"  型態:{','.join(p['pattern'] for p in patterns[:3])}"
            parts.append(line)
            # 支撐阻力
            if sr.get("supports"):
                supports_str = ", ".join(f"${s['price']}({s['distance_pct']}%)" for s in sr["supports"][:2])
                parts.append(f"   支撐: {supports_str}")
            if sr.get("resistances"):
                resist_str = ", ".join(f"${r['price']}({r['distance_pct']}%)" for r in sr["resistances"][:2])
                parts.append(f"   阻力: {resist_str}")

    # ----- OKX 公告 -----
    okx = state.get("okx_news", {})
    if not okx.get("error"):
        parts.append("\n## OKX 官方公告（近 72h）")
        rel = okx.get("watchlist_relevant", [])
        all_items = okx.get("all_recent", [])
        if rel:
            parts.append("涉及 watchlist：")
            for it in rel[:5]:
                parts.append(f"- [{it.get('matched_symbol')}] {it.get('annType')}: {it.get('title','')[:120]}")
        if all_items:
            parts.append("其他近期：")
            for it in all_items[:3]:
                if it not in rel:
                    parts.append(f"- {it.get('annType')}: {it.get('title','')[:120]}")

    return "\n".join(parts)


def _format_pulse_data(pulse_state: dict) -> str:
    """組 hourly pulse 用的 delta-focused 數據摘要"""
    ts = pulse_state.get("ts")
    ts_str = ts.strftime("%H:%M UTC") if ts else "now"
    parts = [f"# 時間：{ts_str}\n"]

    # ---- 即時價格與變動 ----
    parts.append("## 即時價格 (1h/24h/3d/1w)")
    for sym, d in pulse_state.get("price_deltas", {}).items():
        if d.get("error"): continue
        cur = d.get("current")
        c1h = d.get("change_1h_pct")
        c24h = d.get("change_24h_pct")
        c3d = d.get("change_3d_pct")
        c1w = d.get("change_1w_pct")
        hi24 = d.get("high_24h")
        lo24 = d.get("low_24h")
        parts.append(
            f"- {sym}: ${cur}  "
            f"1h={c1h:+.2f}% 24h={c24h:+.2f}% 3d={c3d:+.2f}% 1w={c1w:+.2f}%  "
            f"24h 高/低: ${hi24}/${lo24}"
        )

    # ---- 過去 24h CVD 與 taker buy/sell ----
    parts.append("\n## 24h 主動買賣力量")
    for sym, d in pulse_state.get("flow_recent", {}).items():
        if d.get("error"): continue
        slope = d.get("cvd_slope_24h", 0)
        taker_ratio = d.get("buy_sell_ratio_24h", 0)
        parts.append(f"- {sym}: CVD 24h 斜率={slope:+.3f}  taker buy/sell={taker_ratio:.2f}")

    # ---- 今日清算 ----
    liq = pulse_state.get("liq_today", {})
    if liq and not liq.get("error"):
        parts.append("\n## 今日清算（過去 24h）")
        for it in liq.get("items", [])[:5]:
            imb = it.get("imbalance", 0)
            tag = "（軋空）" if imb > 0.3 else ("（多殺多）" if imb < -0.3 else "")
            parts.append(f"- {it['symbol']}: ${it.get('total_24h', 0)/1e6:.1f}M  imb={imb:+.2f}{tag}")

    # ---- 今日 ETF ----
    parts.append("\n## ETF 即時")
    for sym in ("BTC", "ETH"):
        d = pulse_state.get(f"etf_{sym.lower()}_today", {})
        if d.get("error"): continue
        today = d.get("today_flow_usd", 0)
        last_3d = d.get("cumulative_3d_flow_usd", 0)
        parts.append(f"- {sym}: 今日 ${today/1e6:+,.1f}M  近 3d 累計 ${last_3d/1e6:+,.1f}M")

    # ---- Funding 即時變化 ----
    fund = pulse_state.get("funding_changes", {})
    if fund:
        parts.append("\n## Funding 24h 變化")
        for sym, d in fund.items():
            if d.get("error"): continue
            cur = d.get("current", 0) * 100
            chg = d.get("change_24h_pct_points", 0) * 100
            parts.append(f"- {sym}: 現 {cur:+.4f}%/8h  24h 變化 {chg:+.4f} 百分點")

    # ---- 鯨魚最新狀態 ----
    whales = pulse_state.get("whales_now", {})
    if whales and not whales.get("error"):
        parts.append("\n## Hyperliquid 鯨魚最新淨倉 Top 5")
        for w in whales.get("per_symbol_aggregate", [])[:5]:
            parts.append(f"- {w['symbol']}: 淨多 {w['net_long_pct']:+.0f}%  總倉 ${w['total_usd']/1e6:.1f}M")

    # ---- 情緒即時 ----
    sent = pulse_state.get("sentiment_now", {})
    if sent and not sent.get("error"):
        parts.append(f"\n## 情緒：F&G {sent.get('fear_greed_now')} ({sent.get('fear_greed_label','—')})")

    return "\n".join(parts)


def _format_symbol_data(symbol: str, sym_state: dict) -> str:
    """組 per-symbol deep dive 用的單一標的全資料摘要（含 SMC 量化指標 + 帳戶約束）"""
    parts = [f"# {symbol} 完整數據\n"]

    # === 使用者帳戶約束（重要：Claude 算倉位時必須遵守）===
    parts.append("## ⚠️ 帳戶約束（必須遵守）")
    parts.append("- 帳戶實際 margin: $500-800 USDT（小資金，不能超倉）")
    parts.append("- 單筆風險上限: $100 USDT (1R)")
    parts.append("- 計算公式：position_notional = $100 / |entry - SL| × entry")
    parts.append("- margin = notional / leverage，必須 ≤ $500 才安全")
    parts.append("- 若計算後 5x 槓桿超出 $500 margin → 必須建議 10-15x 才塞得進")
    parts.append("- WLFI 等低流通幣最多 5x，BTC/ETH/SOL 主流可達 15x\n")

    # 多時框型態
    pattern = sym_state.get("pattern", {})
    if pattern and not pattern.get("error"):
        parts.append(f"\n## 多時框型態（共識={pattern.get('consensus')}）")
        for tf in ["15m", "1h", "4h", "12h", "1d", "1w"]:
            by_tf = pattern.get("by_tf", {}).get(tf)
            if not by_tf: continue
            trend = by_tf.get("trend", {})
            sr = by_tf.get("sr", {})
            vp = by_tf.get("volume_price", {})
            patterns = by_tf.get("patterns", [])
            d = trend.get("direction", "?")
            chg = trend.get("change_pct", 0)
            line = f"- {tf}: 趨勢={d} ({chg:+.2f}%)"
            if vp.get("interpretation"):
                line += f"  量價={vp['interpretation']}"
            if patterns:
                line += f"  型態={','.join(p['pattern'] for p in patterns[:2])}"
            parts.append(line)
            if sr.get("supports"):
                sup_str = ", ".join(f"${s['price']}({s['distance_pct']}%)" for s in sr["supports"][:2])
                parts.append(f"   支撐: {sup_str}")
            if sr.get("resistances"):
                res_str = ", ".join(f"${r['price']}({r['distance_pct']}%)" for r in sr["resistances"][:2])
                parts.append(f"   阻力: {res_str}")

    # 即時數據
    snap = sym_state.get("snapshot", {})
    if snap and not snap.get("error"):
        parts.append(f"\n## 即時數據")
        parts.append(f"- 現價 ${snap.get('price')}")
        parts.append(f"- OI: ${snap.get('oi', 0):,.0f}  24h 變化 {snap.get('oi_delta_pct', 0):+.2f}%")
        funding = snap.get("funding", 0)
        if funding is not None:
            parts.append(f"- Funding: {funding*100:+.4f}%/8h")
        parts.append(f"- 大戶持倉比: {snap.get('top_trader_ratio')}  vs 散戶: {snap.get('ls_ratio')}")
        parts.append(f"- 24h 清算: 多 ${snap.get('liq_long', 0)/1e6:.2f}M  空 ${snap.get('liq_short', 0)/1e6:.2f}M")
        parts.append(f"- BTC 閘: {snap.get('btc_gate_open')}  regime: {snap.get('btc_regime')}")
        parts.append(f"- 強勢分數: {snap.get('strength_score')}")
        parts.append(f"- 7d 結構: ATR%={snap.get('atr_pct_7d')}, 量比={snap.get('vol_24h_vs_30d')}, "
                    f"higher_lows={snap.get('higher_lows_7d')}")

    # Hyperliquid 鯨魚（如果這個 symbol 上榜）
    whales = sym_state.get("whales", {})
    if whales and not whales.get("error"):
        sym_whale = next((w for w in whales.get("per_symbol_aggregate", [])
                         if w.get("symbol") == symbol), None)
        if sym_whale:
            parts.append(f"\n## Hyperliquid 鯨魚 ({symbol})")
            parts.append(f"- 淨多倉位百分比: {sym_whale['net_long_pct']:+.0f}%")
            parts.append(f"- 多倉 ${sym_whale['long_usd']/1e6:.1f}M  空倉 ${sym_whale['short_usd']/1e6:.1f}M")

    # ETF (僅 BTC/ETH)
    if symbol in ("BTC", "ETH"):
        etf = sym_state.get(f"etf_{symbol.lower()}", {})
        if etf and not etf.get("error"):
            parts.append(f"\n## {symbol} ETF 流向")
            parts.append(f"- 7d 累計: ${etf.get('cumulative_7d_flow_usd', 0)/1e6:+.1f}M")
            parts.append(f"- 24h: ${etf.get('latest_24h_flow_usd', 0)/1e6:+.1f}M")

    # === SMC 量化結構（joshyattridge/smartmoneyconcepts 套件）===
    smc_data = sym_state.get("smc_levels", {})
    if smc_data:
        parts.append("\n## 🔬 SMC 量化結構（4h 戰術 / 1d 戰略）")
        for tf, levels in [("4h", smc_data.get("4h", {})), ("1d", smc_data.get("1d", {}))]:
            if levels.get("error"):
                continue
            parts.append(f"\n### {tf} 時框 (現價 ${levels.get('current_price')}, {levels.get('candle_count')} 根)")

            # Swing 點
            swings = levels.get("swing_points", [])
            if swings:
                parts.append(f"**Swing 點（最近 {len(swings)} 個）**：")
                for sp in swings:
                    parts.append(f"  - {sp['type']} @ ${sp['level']} ({sp['distance_pct']:+.2f}%, {sp['ago_bars']} 根前)")

            # Order Blocks
            obs = levels.get("order_blocks", [])
            if obs:
                parts.append(f"**Order Block（最近 {len(obs)} 個）**：")
                for ob in obs:
                    status = "已 mitigated" if ob['mitigated'] else "✓ 未觸及（有效）"
                    parts.append(f"  - {ob['type']} OB: ${ob['bottom']:.2f} – ${ob['top']:.2f} "
                               f"({ob['mid_distance_pct']:+.2f}%, {ob['ago_bars']} 根前, {status})")

            # FVG
            fvgs = levels.get("fvg", [])
            if fvgs:
                parts.append(f"**FVG (Fair Value Gap)**：")
                for f in fvgs:
                    parts.append(f"  - {f['type']} FVG: ${f['bottom']:.2f} – ${f['top']:.2f} "
                               f"({f['mid_distance_pct']:+.2f}%, {f['ago_bars']} 根前)")

            # BoS / CHoCH
            bcs = levels.get("bos_choch", [])
            if bcs:
                parts.append(f"**結構變化（BoS / CHoCH）**：")
                for bc in bcs:
                    parts.append(f"  - {bc['type']} {bc['direction']} @ ${bc['level']} "
                               f"({bc['ago_bars']} 根前)")

            # Liquidity
            liqs = levels.get("liquidity", [])
            if liqs:
                parts.append(f"**流動性區域（stop hunt 目標）**：")
                for l in liqs:
                    parts.append(f"  - {l['type']} @ ${l['level']} "
                               f"({l['distance_pct']:+.2f}%, {l['ago_bars']} 根前)")

    return "\n".join(parts)


async def synthesize_hourly_pulse(pulse_state: dict, timeout_sec: int = 180) -> tuple[str | None, dict]:
    """每小時 pulse 報告"""
    return await _synthesize_with_prompt(
        system_prompt=HOURLY_PULSE_PROMPT,
        user_data=_format_pulse_data(pulse_state),
        timeout_sec=timeout_sec,
    )


async def synthesize_per_symbol(symbol: str, sym_state: dict,
                               timeout_sec: int = 180) -> tuple[str | None, dict]:
    """單一標的交易計畫"""
    return await _synthesize_with_prompt(
        system_prompt=PER_SYMBOL_DEEPDIVE_PROMPT.replace("{SYMBOL}", symbol),
        user_data=_format_symbol_data(symbol, sym_state),
        timeout_sec=timeout_sec,
    )


async def _synthesize_with_prompt(system_prompt: str, user_data: str,
                                  timeout_sec: int = 180) -> tuple[str | None, dict]:
    """共用 helper：用 Claude Code Headless 跑任何 system_prompt + user_data。"""
    import asyncio
    import shutil
    import tempfile

    claude_exe = shutil.which("claude")
    if not claude_exe:
        return None, {"error": "claude CLI not found"}

    full_prompt = f"{system_prompt}\n\n---\n{user_data}"
    neutral_cwd = tempfile.gettempdir()

    if claude_exe.endswith(".ps1") or claude_exe.endswith(".cmd"):
        proc = await asyncio.create_subprocess_exec(
            "powershell.exe", "-NoProfile",
            "-Command", "claude -p --output-format text",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, cwd=neutral_cwd,
        )
    else:
        proc = await asyncio.create_subprocess_exec(
            claude_exe, "-p", "--output-format", "text",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, cwd=neutral_cwd,
        )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(input=full_prompt.encode("utf-8")),
            timeout=timeout_sec,
        )
    except asyncio.TimeoutError:
        proc.kill()
        return None, {"error": f"claude timeout {timeout_sec}s"}

    if proc.returncode != 0:
        return None, {"error": f"claude exit={proc.returncode}: {stderr.decode('utf-8', errors='replace')[:300]}"}

    text = stdout.decode("utf-8", errors="replace").strip()
    if not text:
        return None, {"error": "empty output"}

    text = _sanitize_telegram_html(text)
    return text, {
        "input_chars": len(full_prompt),
        "output_chars": len(text),
        "estimated_cost_usd": 0.0,
    }


def _sanitize_telegram_html(text: str) -> str:
    """把 Claude 輸出清理成 Telegram 嚴格 HTML 模式接受的格式。

    Telegram 只認以下標籤（其他全部視為錯誤）：
        b, strong, i, em, u, ins, s, strike, del, code, pre, a, tg-spoiler
    策略：
    1) 把 markdown 殘留轉成 HTML
    2) 標準化白名單標籤大小寫
    3) 找出所有「<...>」位置，若不是白名單標籤 → escape 成 &lt;
    4) 確保 &/</> 在非標籤上下文中正確 escape
    """
    import re

    # v17: blockquote 是 Telegram 原生支援（含 expandable 屬性）— 分層訊息的核心
    ALLOWED = {"b", "strong", "i", "em", "u", "ins", "s", "strike", "del",
               "code", "pre", "a", "tg-spoiler", "blockquote"}

    # === Step 1: markdown → HTML ===
    text = re.sub(r"\*\*([^*]+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"(?<!\*)\*([^*\n]+?)\*(?!\*)", r"<i>\1</i>", text)
    text = re.sub(r"`([^`\n]+?)`", r"<code>\1</code>", text)

    # === Step 2: <br>/<br/> → 換行 ===
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)

    # === Step 3: 移除不支援的塊級標籤但保留內容 ===
    # v17: blockquote 從移除清單拿掉（Telegram 支援，分層訊息需要）
    UNSUPPORTED_REMOVE = ["h1", "h2", "h3", "h4", "h5", "h6", "p", "div",
                         "section", "article", "header", "footer", "nav",
                         "ul", "ol", "li", "table", "tr", "td", "th", "hr"]
    for tag in UNSUPPORTED_REMOVE:
        text = re.sub(rf"</?{tag}[^>]*>", "", text, flags=re.IGNORECASE)

    # === Step 4: 用正則找所有「< 後接東西 >」的片段，逐一判斷 ===
    def _tag_handler(m):
        full = m.group(0)
        inner = m.group(1).strip().lower()
        # 取出 tag 名（可能有 attribute、closing slash）
        tag_match = re.match(r"^/?\s*([a-z][a-z0-9\-]*)", inner)
        if not tag_match:
            return full.replace("<", "&lt;").replace(">", "&gt;")
        tag_name = tag_match.group(1)
        if tag_name in ALLOWED:
            # 標準化小寫
            return f"<{inner}>"
        # 不允許 → escape
        return full.replace("<", "&lt;").replace(">", "&gt;")

    # match <stuff> 一段（盡量短）
    text = re.sub(r"<([^<>]+?)>", _tag_handler, text)

    # === Step 5: 處理剩餘的「裸 <」（如 "< 0.5%"）→ 全部 escape ===
    # 注意：經過 Step 4 後，所有合法 <tag> 都被處理過了
    # 剩下的 < 都是不安全的，用 placeholder 換回去
    # 先把已 escape 的 &lt; 標記起來，避免重複
    text = re.sub(r"&lt;", "\x00LT\x00", text)
    text = re.sub(r"&gt;", "\x00GT\x00", text)
    text = re.sub(r"&amp;", "\x00AMP\x00", text)

    # 找剩餘的 <、>、& 都 escape
    # 但 Step 4 留下的合法標籤已經是 <tag>，要保留它們
    # 簡單做法：找「合法 <tag>」用 placeholder 替換
    legal_tags = re.findall(r"</?(?:" + "|".join(ALLOWED) + r")(?:\s[^>]*)?>", text, flags=re.IGNORECASE)
    placeholder_map = {}
    for i, t in enumerate(legal_tags):
        ph = f"\x00TAG{i}\x00"
        placeholder_map[ph] = t
        text = text.replace(t, ph, 1)

    # 此時 text 應該不再有合法標籤，剩下的 <、> 都 escape
    text = text.replace("&", "&amp;")  # 注意順序，先 & 再 < >
    text = text.replace("<", "&lt;")
    text = text.replace(">", "&gt;")

    # 還原 placeholders
    for ph, t in placeholder_map.items():
        text = text.replace(ph, t)
    text = text.replace("\x00LT\x00", "&lt;")
    text = text.replace("\x00GT\x00", "&gt;")
    text = text.replace("\x00AMP\x00", "&amp;")

    # === Step 6: 壓縮多餘空行 ===
    text = re.sub(r"\n{3,}", "\n\n", text)

    return text.strip()


async def synthesize_via_claude_code(
    state: dict,
    tradfi: dict | None = None,
    watchlist=None,
    timeout_sec: int = 240,
) -> tuple[str | None, dict]:
    """用 Claude Code Headless（CLI subprocess）合成。
    不需 ANTHROPIC_API_KEY、用使用者既有 Claude Code 訂閱（Max 推薦）。

    關鍵設計：
    - prompt 透過 stdin pipe 傳入避免 OS 參數長度限制（>30K tokens 可能撞牆）
    - cwd 設為系統 TEMP 目錄，避免 claude CLI 載入本專案 CLAUDE.md 變成助手模式
    - 強制 --output-format text 取得純文字輸出
    """
    import asyncio
    import shutil
    import tempfile

    # 找 claude 執行檔（Windows 可能是 .ps1/.cmd）
    claude_exe = shutil.which("claude")
    if not claude_exe:
        return None, {"error": "claude CLI not found in PATH"}

    data_text = _format_data_for_prompt(state, tradfi, watchlist)
    full_prompt = (
        f"{SYSTEM_PROMPT}\n\n"
        f"---\n"
        f"以下是當前所有市場數據，請依規範撰寫深度分析簡報：\n\n"
        f"{data_text}"
    )

    # 中性 cwd：避免 claude 載入本專案 CLAUDE.md 而變成「專案助手模式」
    neutral_cwd = tempfile.gettempdir()

    # Windows: claude 是 .ps1，須透過 powershell 包一層
    if claude_exe.endswith(".ps1") or claude_exe.endswith(".cmd"):
        proc = await asyncio.create_subprocess_exec(
            "powershell.exe", "-NoProfile",
            "-Command", "claude -p --output-format text",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=neutral_cwd,
        )
    else:
        proc = await asyncio.create_subprocess_exec(
            claude_exe, "-p", "--output-format", "text",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=neutral_cwd,
        )

    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(input=full_prompt.encode("utf-8")),
            timeout=timeout_sec,
        )
    except asyncio.TimeoutError:
        proc.kill()
        return None, {"error": f"claude CLI timeout ({timeout_sec}s)"}

    if proc.returncode != 0:
        err_msg = stderr.decode("utf-8", errors="replace")[:500]
        return None, {"error": f"claude CLI exit={proc.returncode}: {err_msg}"}

    text = stdout.decode("utf-8", errors="replace").strip()
    if not text:
        return None, {"error": "empty output from claude CLI"}

    # 清理 Claude 可能輸出的不被 Telegram 接受的 HTML 標籤
    text = _sanitize_telegram_html(text)

    return text, {
        "mode": "claude_code_headless",
        "input_chars": len(full_prompt),
        "output_chars": len(text),
        "estimated_cost_usd": 0.0,
        "note": "Using your Claude Code Max subscription",
    }


async def synthesize_macro(
    state: dict,
    tradfi: dict | None = None,
    watchlist=None,
    model: str = DEFAULT_MODEL,
    max_tokens: int = 3000,
) -> tuple[str | None, dict]:
    """合成宏觀敘事分析。

    Returns: (markdown_text 或 None 若失敗, metadata dict 含 usage/cost)
    """
    if not _SDK_OK:
        return None, {"error": "anthropic SDK not installed, pip install anthropic"}

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        return None, {"error": "ANTHROPIC_API_KEY not set"}

    data_text = _format_data_for_prompt(state, tradfi, watchlist)

    client = AsyncAnthropic(api_key=api_key)
    try:
        response = await client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=SYSTEM_PROMPT,
            messages=[{
                "role": "user",
                "content": f"以下是當前所有市場數據，請依規範撰寫深度分析簡報：\n\n{data_text}",
            }],
        )
    except Exception as e:
        return None, {"error": f"{type(e).__name__}: {e}"}

    text = response.content[0].text if response.content else ""

    # 成本估算
    in_tok = response.usage.input_tokens
    out_tok = response.usage.output_tokens
    cost = _estimate_cost(model, in_tok, out_tok)

    return text, {
        "model": model,
        "input_tokens": in_tok,
        "output_tokens": out_tok,
        "estimated_cost_usd": round(cost, 4),
    }


def _estimate_cost(model: str, in_tok: int, out_tok: int) -> float:
    """根據定價估算（2026/6 USD per 1M token）"""
    pricing = {
        MODEL_OPUS:   (15.0, 75.0),
        MODEL_SONNET: (3.0, 15.0),
        MODEL_HAIKU:  (1.0, 5.0),
    }
    inp, out = pricing.get(model, (3.0, 15.0))
    return (in_tok * inp + out_tok * out) / 1_000_000


def should_use_opus(state: dict) -> bool:
    """重大事件啟發式：自動升級到 Opus"""
    cycle = state.get("cycle", {})
    pi = cycle.get("pi_cycle", {})
    if pi.get("signal") == "top_warning":
        return True   # Pi Cycle 頂部訊號 → Opus

    sent = state.get("sentiment", {})
    fg = sent.get("fear_greed_now")
    if fg is not None and (fg >= 85 or fg <= 15):
        return True   # 極端情緒 → Opus

    # ETF 巨額流向
    for key in ("etf_btc", "etf_eth"):
        etf = state.get(key, {})
        c7 = etf.get("cumulative_7d_flow_usd", 0)
        if abs(c7) > 1_500_000_000:  # >$1.5B
            return True

    return False
