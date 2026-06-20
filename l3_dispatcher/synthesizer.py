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
- 加密貨幣永續期貨交易者（本金大小依各自設定，不要假設特定金額或報酬目標）
- 追求穩健的正期望值與嚴格風控，不是賭單一暴利（不承諾任何固定報酬）
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
HOURLY_PULSE_PROMPT = """你是專業交易監視員，每小時做「差分回報」：只報告與上一次報告相比 *新發生* 的變化。

# 差分鐵律（最高優先，違反即失敗）
1. user message 末尾附有「上一次 pulse 報告全文」。凡是上次已講過、且數字無實質變化的內容，禁止再寫。
2. 「實質變化」門檻（低於門檻 = 視為沒變，不准提）：
   - 價格：1h 變動 |≥0.8%|，或突破/跌破上次報告提到的觀察價位或 24h 高低
   - Funding：較上次 |≥0.005 百分點/8h|
   - ETF：出現「新的」單日流向數字（Farside 一天只更新一次；數字沒換 = 沒新資料，禁止重講）
   - 鯨魚：淨多百分比變動 |≥5 個百分點| 或總倉變動 |≥20%|
   - Fear & Greed：變動 |≥5 點|
   - 清算：過去 1h 出現單邊 > $20M
3. 若所有項目都低於門檻 → 只輸出一行，不准多寫任何字：
   ⚪ 過去 1h 無顯著變化｜BTC $XX,XXX（1h ±X.X%）｜上次觀察價位仍有效
4. 有變化時，只列有變化的項目，每項 1-2 句：
   🔺 <項目>：<上次值> → <現值>，<一句因果含義>
5. 上次報告若提了觀察價位/事件：第一句先回答「觸發了沒」，再給新觀察點。
6. 禁止重述 regime、跨月脈絡、30/60/90 天敘事 — 那是早上 08:00 daily macro 的事。
7. 禁止固定段落模板；報告長度跟著變化量走，不准為了湊版面而寫。

# 輸出格式（HTML for Telegram）
- 有變化：≤ 250 中文字。🔺 變化條列 + 最後一行「⏰ 觀察：<價位/事件>」。
  標籤限 <b>、<i>、<code>。
- 無變化：第 3 條的單行格式。
- 若本次是今日第一則（無上次報告）：給 3-4 句當下基準描述即可，標註「（基準）」。"""


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

<b>2. 數據面確認</b>（**強制：OI／資金費率／多空比／CVD 一律以「CoinGlass 數據佐證」區塊為唯一來源、引用其具體數字**；不可引用其他來源或寫「偏多/偏空」空話。圖表與本文用同一份數據，數字必須一致）
- CVD（累積成交量差）：引用最新值與斜率 → 主動買盤吸籌 / 主動賣盤派發？與價格背離了嗎？
- OI 變化：引用 24h % → 增倉（趨勢延續）/ 減倉（獲利了結）？OI 升+價漲=健康多頭；OI 升+價跌=空頭加碼
- Funding rate：引用 %/8h 數字 → 負/中/熱？是否擁擠到反指?
- 大戶帳戶多空比：引用比值 → 誰偏多？與散裝/價格背離了嗎？
- 清算：軋空燃料還是多殺多？
- 鯨魚（Hyperliquid）：壓倒做多/做空/中性？
（若某項數據缺失=「n/a」，明講「該指標暫無數據」，不可捏造）

<b>3. 匯合判定（重要：算「獨立票數」不是「數票數」）</b>
- 流派 A（威科夫 / 量價結構 / 高時框）方向：？
- 流派 B（SMC / 訂單流 / 中時框）方向：？
- 數據面（資金費率 / OI / 多空比 / CVD）方向：？
- ⚠️ **去除假匯合（critical）**：上面三桶若其實都在反映「同一股動能」（例如 CVD 上升、OI 增、多空比偏多其實是同一件事），那只算「1 票」不是 3 票。**同一資訊桶內彼此高度相關的訊號只能算一個獨立確認**。請明確點出「真實獨立確認數 = N 桶」。
- ⚠️ **匯合多 ≠ 勝率高**：研究證實單純堆疊訊號常因相關性/過擬合/狀態錯配而無效。請結合上面的「🧭 市場狀態（regime）」判斷——**趨勢態**才適合順勢突破匯合；**盤整態**對順勢匯合要降權、改看區間反轉；**高波動**縮小倉位。
- **結論**：獨立確認 ≥2 桶且與當前 regime 相符 = 可做；否則觀望。寧缺勿濫。

<b>4. 交易建議</b>

**若可做單**，必須給：
- **方向**：做多 / 做空
- **進場類型**：現價市價追入 / 限價分批埋伏
- **進場區間**：具體價位 — **優先用 SMC 結構**（OB 底/頂、FVG 區、Swing 點），不是憑空估
- **止損**：具體價位 — **必須基於結構**（OB 邊界、Swing 之上/下、Liquidity 區之外），不是固定 % 距離
- **止盈分批**：TP1 / TP2 / TP3 — **目標是下一個 Liquidity / OB / Swing**，不是固定 R 倍數
- **倉位計算（必算！）**：
  - 1R 價差 = |entry - stop|（R 就是這個風險單位，不是固定金額）
  - **依資料中「## ⚠️ 帳戶約束」區塊給的 1R(USD) 與使用者自選槓桿計算 —— 不要自行假設金額、也不要替使用者改槓桿**
  - position_notional = 1R(USD) / 1R價差(%) × entry；margin = notional / leverage
  - 用使用者設定的槓桿算 margin；**若 margin > 帳戶約束的安全上限，只「提示保證金偏重、注意爆倉」，不主動建議調低槓桿**
- **槓桿**：使用使用者自己設定的值（1–50x 由他決定），**不做「你應該用幾倍」這類個人化建議**；只在風險偏高時誠實提示
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

# 🔧 機器可讀計畫（務必遵守）
在文章的「最後一行之後」，附上一段機器可讀的計畫，用下列標記嚴格包住（標記獨立成行、JSON 用半形雙引號、無多餘文字）：
===PLAN_JSON===
{"actionable": true 或 false, "direction": "bull" 或 "bear" 或 null, "entry_type": "market" 或 "limit", "entry": 數字, "entry_lo": 數字或null, "entry_hi": 數字或null, "stop": 數字, "tp1": 數字, "tp2": 數字, "tp3": 數字}
===END_PLAN===
規則：可做單才填 actionable=true 且 direction/stop/tp1-3 必為數字；限價分批進場用 entry_type="limit" 並給 entry_lo/entry_hi 區間；市價追入用 entry_type="market"、entry 給現價附近。觀望則 actionable=false、其餘可為 null。數字不帶千分位逗號與貨幣符號。

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


def _account_constraints_block() -> list[str]:
    """依 botconfig 動態產生「帳戶約束」段（v42 動態化；v44 改 R 制＋尊重使用者自選槓桿）。

    這段是給 LLM 算倉位的護欄；實際風控仍由 leverage/risk_manager 把關。
    v44：R 就是 R（金額由使用者自設，不綁死固定 U 數）；槓桿尊重使用者設定（不主動建議調高或調低），
    只在 margin 超過安全上限時「提示風險」而非替他決定（紅線②：不做個人化投資建議）。"""
    from botconfig import CONFIG
    bal = CONFIG.account_balance_usd
    risk_1r = CONFIG.risk_per_trade_usd
    lev = CONFIG.default_leverage
    # 單筆 margin 上限 = 帳戶 10%（避免單筆壓太重）；現行 $5000 → $500，與升級前一致。
    margin_cap = round(bal * 0.10)
    return [
        "## ⚠️ 帳戶約束（護欄，非投資建議）",
        f"- 本金：約 ${bal:,.0f} USDT（{CONFIG.tier.label}級保守護欄，不可超倉）",
        f"- 1R＝「一個風險單位」＝|entry − stop| 的價差；此帳戶單筆 1R 設為 ${risk_1r:,.0f} USDT"
        "（使用者自設值，可在 /settings 改；R 不是固定金額）",
        "- 倉位公式：position_notional = 1R(USD) / 1R價差(%) × entry；margin = notional / leverage",
        f"- 使用者自選槓桿：{lev}x（1–50x 由使用者自己定；**尊重此設定，不要主動建議調高或調低**）",
        f"- 唯一護欄：單筆 margin 宜 ≤ ${margin_cap:,.0f}（帳戶 10%）。若按使用者槓桿算出的 margin 超過此線，"
        "**只提示「保證金偏重、注意爆倉風險」，不替他改槓桿**",
        "- 誠實提醒：槓桿越高、離爆倉越近；但用多少是交易員自己的決定（紅線②：不做個人化投資建議）\n",
    ]


def _format_symbol_data(symbol: str, sym_state: dict) -> str:
    """組 per-symbol deep dive 用的單一標的全資料摘要（含 SMC 量化指標 + 帳戶約束）"""
    parts = [f"# {symbol} 完整數據\n"]

    # v33：使用者選的訊號模式 → 調整「可做單」嚴格度（穩健少而精／積極多而廣）
    try:
        from botconfig import get_str
        _mode = get_str("SIGNAL_MODE", "balanced")
    except Exception:
        _mode = "balanced"
    _mode_rule = {
        "steady": "🛡️ 穩健模式：寧缺勿濫。**只有在 regime 相符 + 真實獨立確認 ≥2 桶 + RR≥1.5 時才 actionable=true**，否則一律觀望。",
        "balanced": "⚖️ 平衡模式：獨立確認 ≥2 桶且與 regime 相符可做單；邊緣情況偏保守。",
        "aggressive": "🔥 積極模式：可放寬到獨立確認 ≥1 桶、含逆勢/異常機會，但**必須在文中明確標註風險較高、勝率較低**。",
    }.get(_mode, "⚖️ 平衡模式")
    parts.append(f"## 🎚️ 訊號模式：{_mode_rule}\n")

    # === 使用者帳戶約束（v42：動態依 botconfig，不再寫死）===
    parts.extend(_account_constraints_block())

    # v33：市場狀態（regime）— 策略-狀態適配的前提
    rg = sym_state.get("regime") or {}
    if rg.get("label") and rg["label"] != "資料不足":
        parts.append(f"## 🧭 市場狀態（4h regime）：{rg['label']}")
        parts.append("（趨勢態：順勢突破/續勢較順風（教科書傾向，非勝率保證）；盤整態：區間高賣低買、對順勢追單降權；"
                     "高波動：縮小倉位。請依此狀態調整策略選擇與信心，不要不分狀態硬套同一招。）\n")

    # v33：Wyckoff 階段（heuristic，定宏觀方向偏置 + 關鍵事件）
    wy = sym_state.get("wyckoff") or {}
    if wy.get("phase") and wy.get("narrative"):
        parts.append(f"## 🔷 Wyckoff 階段：{wy['narrative']}")
        evs = wy.get("events") or []
        if evs:
            parts.append("- 近期事件：" + "、".join(
                f"{e['type']}({e['ago_bars']}根前)" for e in evs[-4:]))
        if wy.get("box_lo") and wy.get("box_hi"):
            parts.append(f"- 交易區間(TR)：{wy['box_lo']:,.6g} ~ {wy['box_hi']:,.6g}"
                         f"（{wy.get('caveat','')}）")
        parts.append("（Wyckoff 定方向偏置與『該不該等』；Spring/UTAD 是經典反轉前置（勝算傾向較佳、非保證），"
                     "但須 CVD/OI 同向驗證避免假突破。）\n")

    # M2：多時框對齊驗證（HTF 1d → LTF 4h）— 高層偏置閘，deepdive 須據此調整信心
    ha = sym_state.get("htf_alignment") or {}
    if ha.get("verdict") and ha["verdict"] != "unknown":
        parts.append(f"## 🎯 多時框對齊（HTF 1d → LTF 4h）：{ha['note']}")
        seg = []
        if ha.get("ltf_signal"):
            seg.append(f"4h 最新結構={ha['ltf_signal']}")
        if ha.get("htf_trend"):
            seg.append(f"1d 趨勢={ha['htf_trend']}")
        if ha.get("price_1d_zone"):
            seg.append(f"現價在 1d {ha['price_1d_zone']} 區")
        if seg:
            parts.append("- " + "／".join(seg))
        parts.append("（規則：順 1d 趨勢、且在 1d 折價區做多／溢價區做空＝較順風（條件佔優、非勝率保證）；"
                     "逆勢或追高殺低＝接刀。此為輔助偏置非硬性否決——若逆 HTF 仍要做，"
                     "必須有強力獨立數據確認並在文中標註勝算較差、縮小倉位。）\n")

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

    # 即時數據（v33：OI/資金費率/多空比 一律以下方「CoinGlass 數據佐證」為唯一來源，
    #          這裡不再印，避免與圖表/佐證區塊數字打架）
    snap = sym_state.get("snapshot", {})
    _has_cg = bool(sym_state.get("coinglass"))
    if snap and not snap.get("error"):
        parts.append(f"\n## 即時數據")
        parts.append(f"- 現價 ${snap.get('price')}")
        if not _has_cg:   # 無 CoinGlass 時才用快照的 OI/funding/多空比（fallback）
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

    # === v32: CoinGlass 佐證序列（CVD / OI / 資金費率 / 多空比）===
    cg = sym_state.get("coinglass", {})
    if cg and any(cg.get(k) is not None for k in ("cvd", "oi", "funding", "ls_ratio")):
        parts.append("\n## 📊 CoinGlass 數據佐證（4h，必須在分析中引用具體數字）")
        cvd = cg.get("cvd") or []
        if cvd:
            slope = cg.get("cvd_slope")
            trend = ("上升=買方主動吸籌" if (slope or 0) > 0
                     else "下降=賣方主動派發" if (slope or 0) < 0 else "走平")
            parts.append(f"- CVD（累積成交量差）：最新 {cvd[-1]:,.0f}，"
                         f"近 24h 斜率 {slope if slope is not None else 'n/a'}（{trend}）")
        oi = cg.get("oi") or []
        if oi:
            d24 = cg.get("oi_delta_24h")
            oi_trend = ("增倉" if (d24 or 0) > 0 else "減倉" if (d24 or 0) < 0 else "持平")
            parts.append(f"- OI（未平倉合約）：最新 ${oi[-1]:,.0f}，"
                         f"24h {d24:+.2f}%（{oi_trend}）" if d24 is not None
                         else f"- OI：最新 ${oi[-1]:,.0f}")
        if cg.get("funding") is not None:
            f = cg["funding"]
            ftone = ("過熱偏多（軋空風險）" if f > 0.0005 else
                     "偏空（空頭擁擠）" if f < -0.0005 else "中性")
            parts.append(f"- 資金費率：{f*100:+.4f}%/8h（{ftone}）")
        if cg.get("funding_oi_weighted") is not None:   # M5
            fw = cg["funding_oi_weighted"]
            wtone = ("（OI 加權偏高＝大倉位方向擁擠，反指/軋空風險↑）" if fw > 0.0005 else
                     "（OI 加權偏低＝空方擁擠，反彈風險↑）" if fw < -0.0005 else "")
            parts.append(f"- 資金費率(OI 加權)：{fw*100:+.4f}%{wtone}")
        if cg.get("ls_ratio") is not None:
            ls = cg["ls_ratio"]
            ltone = "大戶偏多" if ls > 1.05 else "大戶偏空" if ls < 0.95 else "多空均衡"
            parts.append(f"- 大戶帳戶多空比：{ls:.2f}（{ltone}）")
        # v33 新增佐證：清算 / 期現基差 / 結構評分 / 情緒
        liq = cg.get("liq_24h") or {}
        if liq:
            lo, sh = liq.get("long", 0) / 1e6, liq.get("short", 0) / 1e6
            fuel = ("空頭被清算較多→軋空燃料" if sh > lo * 1.3 else
                    "多頭被清算較多→下殺燃料" if lo > sh * 1.3 else "多空清算均衡")
            parts.append(f"- 近24h 清算：多 {lo:.2f}M／空 {sh:.2f}M USD（{fuel}）")
        clusters = cg.get("liq_clusters") or []   # M1
        if clusters:
            parts.append("- 清算密集價帶（估計分佈，非真實掛單／非熱力圖，流動性磁吸參考）：")
            for cl in clusters:
                dom = {"long": "多單清算為主→下方磁吸/支撐曾被洗",
                       "short": "空單清算為主→上方磁吸/軋空帶",
                       "balanced": "多空均衡"}.get(cl["dominant"], "")
                parts.append(f"  · ${cl['low']:.4g}–${cl['high']:.4g}："
                             f"${cl['total']/1e6:.1f}M（{dom}）")
        basis = cg.get("basis") or {}
        if basis.get("pct") is not None:
            parts.append(f"- 期現基差：{basis['pct']:+.3f}%"
                         + (f"（{basis['interp']}）" if basis.get("interp") else ""))
        st = cg.get("structure") or {}
        if st:
            seg = []
            if st.get("cvd_slope_7d") is not None:
                seg.append(f"CVD斜率7d {st['cvd_slope_7d']:+.2f}")
            if st.get("oi_delta_7d_pct") is not None:
                seg.append(f"OI 7d {st['oi_delta_7d_pct']:+.1f}%")
            if st.get("higher_lows_7d") is not None:
                seg.append("墊高低點✓" if st["higher_lows_7d"] else "未墊高低點")
            if st.get("above_4h_200ma") is not None:
                seg.append("站上4h_200MA✓" if st["above_4h_200ma"] else "在4h_200MA下")
            if seg:
                parts.append("- 結構評分（7d）：" + "／".join(seg))
        senti = cg.get("sentiment") or {}
        if senti.get("fg") is not None:
            parts.append(f"- 市場情緒：恐懼貪婪 {senti['fg']}"
                         + (f"（{senti['fg_label']}）" if senti.get("fg_label") else ""))

    # v33：Binance 第二來源交叉驗證（兩所分歧＝資訊，須在分析點出）
    xc = sym_state.get("binance_xcheck") or {}
    bn = xc.get("binance") or {}
    if bn:
        line = "\n## 🔀 跨所交叉驗證（Binance 第二來源）"
        parts.append(line)
        if bn.get("ls_ratio") is not None:
            parts.append(f"- Binance 大戶多空比：{bn['ls_ratio']:.2f}")
        if bn.get("funding") is not None:
            parts.append(f"- Binance 資金費率：{bn['funding']*100:+.4f}%/8h")
        if xc.get("flags"):
            for fl in xc["flags"]:
                parts.append(f"- ⚠️ {fl}（兩所分歧，留意）")
        else:
            parts.append("- ✅ 與主源(OKX/CoinGlass)大致一致，訊號可信度較高")

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

            # Order Blocks（H3：只取未緩解、依強度排序，與圖一致；L_b：附強度）
            obs_all = levels.get("order_blocks", [])
            obs = sorted([o for o in obs_all if not o.get("mitigated")],
                         key=lambda o: o.get("strength", 0), reverse=True)
            if obs:
                _drop = len(obs_all) - len(obs)
                parts.append(f"**Order Block（未緩解 {len(obs)} 個，與圖一致"
                             + (f"；另 {_drop} 個已 mitigated 略過" if _drop else "") + "）**：")
                for ob in obs:
                    parts.append(f"  - {ob['type']} OB: ${ob['bottom']:.2f} – ${ob['top']:.2f} "
                               f"({ob['mid_distance_pct']:+.2f}%, {ob['ago_bars']} 根前, "
                               f"強度 {ob.get('strength', 0):.0f}/100)")

            # FVG（H3：只取位移達標的，與圖表 0.45×ATR 過濾一致）
            fvgs_all = levels.get("fvg", [])
            fvgs = [f for f in fvgs_all if f.get("significant", True)]
            if fvgs:
                _drop = len(fvgs_all) - len(fvgs)
                parts.append("**FVG (Fair Value Gap，已過位移過濾"
                             + (f"；另 {_drop} 個位移不足略過" if _drop else "") + ")**：")
                for f in fvgs:
                    parts.append(f"  - {f['type']} FVG: ${f['bottom']:.2f} – ${f['top']:.2f} "
                               f"({f['mid_distance_pct']:+.2f}%, {f['ago_bars']} 根前)")

            # BoS / CHoCH（M4：附 OI 確認真偽）
            bcs = levels.get("bos_choch", [])
            if bcs:
                parts.append(f"**結構變化（BoS / CHoCH）**：")
                for bc in bcs:
                    line = (f"  - {bc['type']} {bc['direction']} @ ${bc['level']} "
                            f"({bc['ago_bars']} 根前)")
                    if bc.get("oi_confirm"):
                        line += f"｜OI：{bc['oi_confirm']}"
                    parts.append(line)

            # H4：Premium / Discount / Equilibrium + OTE 進場區
            pd = levels.get("premium_discount") or {}
            if pd.get("zone"):
                zmap = {"premium": "溢價區（偏找空、不追多）",
                        "discount": "折價區（偏找多、不追空）",
                        "equilibrium": "均衡區（中性）"}
                parts.append(f"**溢價/折價（{tf}）**：現價位於 {zmap.get(pd['zone'], pd['zone'])}"
                             f"，區間 ${pd['swing_low']:.4g}–${pd['swing_high']:.4g}"
                             f"，均衡線 ${pd['equilibrium']:.4g}，位置 {pd['price_position']:.0%}")
                ote = levels.get("ote") or {}
                lo, sh = ote.get("long") or {}, ote.get("short") or {}
                if lo and sh:
                    parts.append(
                        f"  - OTE 多方甜蜜帶 ${lo['low']:.4g}–${lo['high']:.4g}"
                        f"{'（現價在此✓）' if lo.get('in_zone') else ''}；"
                        f"OTE 空方甜蜜帶 ${sh['low']:.4g}–${sh['high']:.4g}"
                        f"{'（現價在此✓）' if sh.get('in_zone') else ''}")

            # H2：流動性掃單（Spring/UTAD，最有 alpha 的反轉前置）+ M4 OI 確認
            sweeps = levels.get("liquidity_sweeps") or []
            if sweeps:
                parts.append(f"**流動性掃單（Spring/UTAD，{tf}）**：")
                for sw in sweeps:
                    tag = ("▲ 下方掃單(Spring，偏多反轉)" if sw["dir"] == "up"
                           else "▼ 上方掃單(UTAD，偏空反轉)")
                    line = f"  - {tag} @ ${sw['level']:.4g}（{sw['ago_bars']} 根前）"
                    if sw.get("oi_confirm"):
                        line += f"｜OI：{sw['oi_confirm']}"
                    parts.append(line)

            # Liquidity
            liqs = levels.get("liquidity", [])
            if liqs:
                parts.append(f"**流動性區域（stop hunt 目標）**：")
                for l in liqs:
                    parts.append(f"  - {l['type']} @ ${l['level']} "
                               f"({l['distance_pct']:+.2f}%, {l['ago_bars']} 根前)")

    return "\n".join(parts)


async def synthesize_hourly_pulse(pulse_state: dict, timeout_sec: int = 180,
                                  last_pulse_text: str | None = None,
                                  last_pulse_ts: str | None = None
                                  ) -> tuple[str | None, dict]:
    """每小時 pulse 報告（v23-2 差分式：附上次報告全文當基準）"""
    user_data = _format_pulse_data(pulse_state)
    if last_pulse_text:
        user_data += (f"\n\n## 上一次 pulse 報告全文（{last_pulse_ts or '?'}）"
                      f"— 差分基準，已講過且沒變的禁止再講\n"
                      f"{last_pulse_text[:1500]}")
    else:
        user_data += "\n\n## 上一次報告：無（今日第一則，輸出基準描述）"
    return await _synthesize_with_prompt(
        system_prompt=HOURLY_PULSE_PROMPT,
        user_data=user_data,
        timeout_sec=timeout_sec,
    )


def _extract_plan_block(text: str) -> tuple[str, dict | None]:
    """v33：從 deepdive 文末抽機器可讀 PLAN_JSON 區塊；回 (去掉區塊的文章, plan dict 或 None)。"""
    import json
    import re
    m = re.search(r"===PLAN_JSON===\s*(\{.*?\})\s*===END_PLAN===", text, re.DOTALL)
    if not m:
        return text, None
    clean = (text[:m.start()] + text[m.end():]).strip()
    try:
        plan = json.loads(m.group(1))
    except Exception:
        return clean, None
    # 正規化數字欄位
    def _num(v):
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None
    for k in ("entry", "entry_lo", "entry_hi", "stop", "tp1", "tp2", "tp3"):
        plan[k] = _num(plan.get(k))
    plan["actionable"] = bool(plan.get("actionable"))
    if plan.get("direction") not in ("bull", "bear"):
        plan["direction"] = None
    return clean, plan


async def synthesize_per_symbol(symbol: str, sym_state: dict,
                               timeout_sec: int = 180) -> tuple[str | None, dict]:
    """單一標的交易計畫（v33：附帶機器可讀 plan，存進 meta['plan']）。"""
    text, meta = await _synthesize_with_prompt(
        system_prompt=PER_SYMBOL_DEEPDIVE_PROMPT.replace("{SYMBOL}", symbol),
        user_data=_format_symbol_data(symbol, sym_state),
        timeout_sec=timeout_sec,
    )
    if text:
        text, plan = _extract_plan_block(text)
        meta["plan"] = plan
    return text, meta


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
