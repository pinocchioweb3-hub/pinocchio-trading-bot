# 通用交易意圖（Trade-Intent）輸出 Schema 設計報告

> 對象：本訊號機器人輸出「交易指令」，由人或交易所 AI agent（OKX/Binance/Gate/BingX/Bitget）執行。
> 目標：設計一份任何交易所 AI agent 都讀得懂的「通用交易意圖」輸出格式。
> 紅線（不可違反）：本系統永不自動下實盤。Schema 只是讓訊號「可被執行」，不是「自動執行」；實盤一律人工確認或 demo 盤。

---

## 1. 結論摘要

1. **「下單參數層」的通用 schema 不可行也不該硬做，但「意圖層」的通用 schema 可行且該做。** 六家交易所的下單核心欄位概念相同（標的、買賣方向、單型、價格、數量、reduce-only），但拼法全部不同，且最基礎的「數量單位」就分裂成「張數 vs 幣本位 vs 反向合約張」三種互不相容的世界 [1][2]。在這層做通用 schema 等於重寫一個 CCXT，是 leaky abstraction 的重災區。**正確切法是輸出「意圖（intent）」——進場區間、失效價、風險百分比、R 倍數目標——把「張數/tick 進位/單向雙向/保證金模式」全部留給交易所 adapter 在執行邊界解。** 這正是 DeFi intent 標準（Anoma：宣告式而非命令式）的核心教訓 [9]。

2. **能複用就別自造：路由到交易所實際下單請用 CCXT，不要自己寫六套 REST mapping。** CCXT 的 `createOrder(symbol, type, side, amount, price, params)` 已經把現貨與簡單線性永續的常規執行標準化，並已統一 `triggerPrice/stopLossPrice/takeProfitPrice` 與 cost-based 市價買 [16][17][25]。我們要造的不是「下單抽象」，是「**意圖描述 + 給人看的決策理由**」——那是 CCXT 不做的部分。

3. **沒有交易專用的 AI-agent schema 標準；事實標準是 MCP（Model Context Protocol）。** OKX 的 Agent Trade Kit（npm: `okx-trade-mcp`）把下單包成 `swap_place_order` 等函式呼叫工具，JSON Schema 沿用 OKX REST 慣例（instId/tdMode/side/ordType/sz）[8][9]。Bitget、Tradier 也跟進 [11]。**意涵：我們的 intent schema 應該長成「MCP 工具能輕鬆消費的 JSON」，而不是發明新的線路協定。**

4. **明確分「給人看的欄位」與「給機器執行的欄位」。** 訊號機器人的價值在於決策理由（CVD 背離、OI 軌跡、失效邏輯），這對人類與 LLM-as-executor 都是上下文；執行欄位（entry/stop/tp/risk）則必須是機器可校驗的數字。兩者放同一份 JSON 但分區，是本設計的關鍵。

5. **不可能像鏈上 intent 那樣 trustless 自動結算。** DeFi intent 由結算合約原子性地驗證、違反即 revert [27]。中心化交易所沒有這種原子驗證器，所以我們必須自己補「**事前編譯器**（intent → 交易所訂單參數）+ 事後一致性檢查（成交價有沒有落在宣告的價格帶/風險%內）」才拿得回「驗證結果」這個保證 [28]。但在本系統，這個編譯器永遠停在「產出可執行參數 / demo 盤」，不接實盤自動下單。

---

## 2. 各交易所下單參數對照（共通點 vs 分歧點）

### 2.1 完全一致的點（可安全當成共同核心）

| 維度 | 共識 |
|---|---|
| 概念核心欄位 | 標的、買/賣方向、單型（limit/market + post-only 變體）、價格（限價才有）、數量、reduce-only — 六家概念一致，只是拼法不同 [1] |
| **槓桿不是下單參數** | 六家**全部**用獨立端點先設好（OKX `set-leverage`、Binance `leverage`、Bybit `set-leverage`…），下單請求裡沒有 leverage 欄位 [6] |
| limit/market | 普遍支援；post-only、IOC/FOK 也都有（只是編碼方式分兩派）[7] |

### 2.2 分歧點（這些絕對不要硬塞進通用 schema 的核心欄位）

**(a) 合約計量單位 — 移植 bug 最大來源 [2]**

| 交易所 | `數量` 的意義 |
|---|---|
| OKX | 張數（SWAP/FUTURES），notional = sz × ctVal × markPx |
| Bybit | 線性(USDT)永續=**幣本位**；反向=**張數**（同一家兩種！）|
| Binance USDⓈ-M | 幣本位（如 BTC）|
| Gate | **有號整數**張數（正=多、負=空）|
| BingX | 依該 symbol 設定（張/幣）|
| Bitget | 幣本位 |

> 結果：`qty=1` 在 OKX/Gate 是 1 張，在 Bybit-線性/Binance/Bitget 是 1 顆 BTC，曝險天差地別 [2]。OKX 甚至同一筆單生命週期內單位會變（現貨保證金市價買=計價幣 notional，首次成交後欄位被改寫成實得基準幣）[31]。**結論：通用 schema 不放「張數」，只放風險%與名目價值，張數在 adapter 用 per-instrument ctVal/contractSize 反算。**

**(b) 單向 / 雙向（hedge）持倉 — 四種互不相容做法 [3]**

| 交易所 | 做法 |
|---|---|
| OKX | `posSide` long/short（單向省略或 net）|
| Binance / BingX | `positionSide` LONG/SHORT/BOTH |
| Bybit | **數字** `positionIdx`：0 單向 / 1 hedge-buy / 2 hedge-sell |
| Bitget | 拆成兩欄：`side`(buy/sell) + `tradeSide`(open/close)，後者僅 hedge 模式用 |
| Gate | **沒有 side 欄位**，方向藏在 size 正負號 |

> 更糟：hedge/one-way 是**帳戶層全域狀態**，不是單筆訂單欄位。Binance 改 position mode 會套用到**每個** symbol，且有未平單/倉位時直接被拒（-4067/-4068）[33]。CCXT 自己都會在 one-way 模式於 Bitget、Huobi 上 `createOrder` 出包 [34]。**結論：方向用 intent 的 `side: long|short` 表達；單向/雙向交給 adapter 讀帳戶狀態解。**

**(c) 保證金模式（cross/isolated）耦合不同 [5]**

- OKX：**強制**在下單請求裡帶 `tdMode`（cross/isolated/cash）。
- Bitget：也在每筆訂單 inline 帶 `marginMode`。
- Binance/Bybit/Gate/BingX：**不**在訂單體裡，靠獨立端點先設好（per-symbol 帳戶設定）。

> 結論：intent 可放一個**選填** `margin_mode` 提示（給人看 + adapter 參考），但不當必填核心。

**(d) 進場附帶 TP/SL — 分歧最大 [7]**

| 交易所 | 能否在進場單直接帶 TP/SL |
|---|---|
| Bybit | 可（takeProfit/stopLoss/tpslMode）|
| BingX | 可（stopLoss/takeProfit JSON）|
| Bitget | 可（presetStopSurplusPrice/presetStopLossPrice）|
| Binance USDⓈ-M | **不可**。2025 algo 遷移後，STOP_MARKET/TAKE_PROFIT_MARKET 須當**獨立**單送（Algo Order 端點），舊式 type-based TP/SL 已棄用（錯誤 -4120）[7]（medium 信心：依官方 changelog + freqtrade issue #12610）|
| OKX | 走獨立 `order-algo`（新版 `attachAlgoOrds` 可 inline，此細節 medium 信心）[7] |
| Gate | 須獨立 price-triggered 單 |

> 結論：intent 把 TP/SL 當**獨立子意圖陣列**（多段分批），讓 adapter 自行決定「inline 帶 vs 拆獨立 algo 單」。這也剛好對上本系統現有的 TP1/TP2/TP3 三段分批設計（見 §6）。

**(e) reduceOnly 型別與限制不同 [4]**

- OKX/Bybit/Binance/Gate/BingX：布林 true/false。
- Bitget：字串 `'YES'/'NO'`，且**只在單向模式**生效（hedge 模式改用 `tradeSide=close`）。
- Gate：另有 `auto_size`，且 `size=0 + reduce_only` 視為平倉。

> 結論：intent 用布林 `reduce_only`，字串/模式差異由 adapter 翻譯。

**(f) 單型編碼分兩派 [7]**

- enum 派（OKX `ordType`=limit/market/post_only/fok/ioc…；Bybit/Binance/BingX 用 orderType + 獨立 timeInForce）。
- tif 派（Gate `tif`=gtc/ioc/poc/fok，poc=post-only；Bitget `force`；Gate 市價=price '0' + tif 'ioc'，沒有 market 型別）。

> 結論：intent 用 `order_type: limit|market` + `time_in_force` + `post_only` 三個正交欄位，adapter 自行折疊成各家編碼。

---

## 3. 現有標準借鏡

### 3.1 FIX 協定（NewOrderSingle, MsgType=D）[15]
用編號 tag 表達訂單：ClOrdID(11)、Symbol(55)、Side(54: 1=Buy/2=Sell)、OrdType(40: 1=Market/2=Limit/3=Stop)、OrderQty(38)、Price(44 限價必填)、StopPx(99 停損必填)。**借鏡：用「客戶端訂單 ID」做去重/對帳是老牌做法 → 我們 intent 必須有 `intent_id`（即 ClOrdID 角色）。** 但 FIX 是低延遲線路協定，欄位是扁平 tag，不適合表達「決策理由」，不直接抄。

### 3.2 CCXT 統一 API — 已解決什麼、抽象在哪漏
- **已解決（直接用就夠）[25][26]**：markets/tickers/orderbook/balances、基本 `createOrder/cancelOrder`、統一 `triggerPrice/stopLossPrice/takeProfitPrice`、cost-based 市價買（`createMarketBuyOrderRequiresPrice`/`quoteOrderQty`）。對「現貨 + 簡單線性永續執行」，再造一套平行通用 schema 基本是冗餘。
- **抽象在哪漏 [17][18][19]**：
  - 簽名 `createOrder(symbol, type, side, amount, price, params)` 只統一前五個，**其餘全靠 `params={}` 逃生艙**（reduceOnly、posSide、marginType、closePosition…由 caller 直接塞 exchange-specific 值）[17][18]。
  - 觸發單命名歷史混亂，CCXT 統一後仍有 alias 怪癖（不支援觸發單時 triggerPrice 變 stopLossPrice 的別名）且分批 rollout，behavior 不一致 [19]。
  - 能力不齊：`has[]`/`.features` per-exchange 旗標（triggerPrice/stopLossPrice/marginMode/trailing/iceberg…可 true 可 false），同一通用呼叫在不同所可能不支援或行為不同 [20]。
  - 官方自承：unified layer 刻意隱藏端點細節，這正是洩漏源頭 [21]。CCXT 明說 unified API 只是「各所共同子集 + params override」，**從沒宣稱要做到完全通用** [23]。

> **教訓：意圖層在 CCXT 之上、不要與它競爭。我們的 intent → 用 adapter 翻成 CCXT 的 `createOrder` + `params`，由 CCXT 吃掉六套 REST 的差異。**

### 3.3 ISO 20022 [22]
XML/ASN.1 訊息標準，Securities Trade(setr) 域涵蓋下單到結算（如 SubscriptionOrder setr.010）。**但它瞄準機構基金/證券 post-trade 流程，不是低延遲交易所下單（那是 FIX 的地盤）[24]。** 對加密永續可移植下單而言過重、不適用，僅作為「訊息要分業務域 + 版本化」的觀念參考。

---

## 4. 「意圖（intent）」vs「訂單（order）」分層

### 4.1 DeFi intent 標準的四個教訓

- **Anoma**：intent 是「個體期望的最終狀態」，**宣告式而非命令式**，只關心 what 不關心 how（如 [-2000 USDC, +1 ETH] 授權任何滿足此差額的未來狀態，不指定執行步驟）[9]。**→ 這正是「交易意圖（進場區/失效/風險%/R 目標）vs 具體交易所訂單」分層的完美類比。**
- **ERC-7521**：穩定且內容無關的 entry/validation 核心 + 可插拔的 Intent Standard 合約定義各 intent 型別如何被解讀；新 intent 種類不必改錢包 [23-erc]。**→ 教訓：保持穩定的信封/校驗核心，把 intent 語意版本化在模組化具名標準裡，別硬寫死。**
- **UniswapX**：簽名的鏈下訂單，欄位**純粹是結果約束**（inputs/outputs/deadline/Dutch 衰減排程/exclusiveFiller），**不指定路由或場所**，由 filler 競標決定如何執行；`ResolvedOrder` 是校驗後的可執行形態 [24-ux]。**→ 教訓：定義一個「resolve」翻譯邊界，把通用 intent 解析成場所專屬可執行參數。**
- **CoW Protocol**：使用者只簽 limit price/quantity/kind/deadline（EIP-712 等，免 gas 因為不是交易），solver 競相翻成結算交易，結算合約**強制**驗證限價/數量 [26]。**→ 教訓：intent 是簽名的約束集，是否符合在「結算時被強制驗證」，不是被假設。**

### 4.2 我們應輸出 intent 還是 order？→ **輸出 intent**

理由：
1. 訊號機器人本來就在「想 intent」——ICT/SMC 方法論把失效/停損放在「論點結構上錯的地方」，把進場區/失效/停損/止盈/倉位耦合成一個邏輯單元（exchange-agnostic 心智模型），之後才編譯成 limit/stop/TP 參數 [29]。
2. 輸出 order 等於把張數/tick/單向雙向硬編進訊號，一換交易所就全錯（§2 的洩漏）。
3. 輸出 intent + 一個 adapter 邊界，才能讓「任何交易所 AI agent」自己 resolve。

**但要補鏈上 intent 免費拿到、CEX 拿不到的東西 [28]**：CEX 沒有原子結算驗證器，所以我們自帶
- **事前編譯器**：intent → 交易所訂單參數（經 CCXT/MCP）。
- **事後一致性檢查**：成交是否落在宣告的價格帶 / 風險%。
- 在本系統，編譯器產物只到「可執行參數預覽 / demo 盤」，**永不接實盤自動下單**。

---

## 5. 草案：最小可行通用 trade-intent JSON Schema

設計原則：核心只放**穩定共同核心 + 結果約束**，**不**通用化張數、tick/lot 進位、單向/雙向 [35]。這些在 adapter 邊界用 per-instrument metadata（ctVal/contractSize、precision、min-notional）與帳戶狀態解。

### 5.1 欄位定義

| 欄位 | 型別 | 必/選 | 給誰 | 說明 / 單位約定 |
|---|---|---|---|---|
| `schema` | string | 必 | 機器 | 固定 `"trade-intent"` |
| `version` | string | 必 | 機器 | semver，如 `"1.0"`（語意版本化，借 ERC-7521 [23-erc]）|
| `intent_id` | string | 必 | 機器 | 唯一 ID（FIX ClOrdID 角色 [15]），去重/對帳 |
| `created_at` | string(ISO8601) | 必 | 兩者 | UTC |
| `asset_class` | enum | 必 | 機器 | `crypto_perp` \| `crypto_spot` \| `equity_signal` |
| `symbol_canonical` | string | 必 | 兩者 | 規範形 `BASE-QUOTE`（如 `BTC-USDT`），**adapter 負責轉成各所拼法** [1] |
| `venue_hint` | string\|null | 選 | 機器 | 偏好交易所，null=任意，呼應 UniswapX「不綁場所」[24-ux] |
| `side` | enum | 必 | 兩者 | `long` \| `short`（**不用 buy/sell**，避開 Gate 號位/Bitget 雙欄分歧 [3]）|
| `order_type` | enum | 必 | 機器 | `limit` \| `market`（正交於 tif）[7] |
| `time_in_force` | enum | 選 | 機器 | `gtc`\|`ioc`\|`fok`（預設 gtc）[7] |
| `post_only` | bool | 選 | 機器 | 預設 false [7] |
| `reduce_only` | bool | 選 | 機器 | 預設 false（布林；字串/模式差異 adapter 翻 [4]）|
| `margin_mode` | enum\|null | 選 | 機器 | `cross`\|`isolated`\|null（提示，非必填 [5]）|
| `entry_zone` | {low,high,reference} | 必 | 兩者 | 報價幣計價的**進場區間**（非單一價），呼應現有「分段掛限價」[本系統] |
| `invalidation` | {price, rule} | 必 | 兩者 | 失效價 + 人話規則（如「4h 收盤跌破」）；論點結構失效 [29] |
| `risk` | {pct_of_account, suggested_leverage, max_slippage_pct} | 必 | 兩者 | **以帳戶%表風險**（不放張數！），槓桿是建議值（六家都靠獨立端點設 [6]）|
| `take_profits` | [{r_multiple, price, size_pct, action}] | 選 | 兩者 | 多段分批；adapter 決定 inline 帶或拆 algo 單 [7] |
| `acceptance` | {price_band_pct, deadline, max_slippage_pct} | 必 | 機器 | **機器可校驗的接受準則**（事後一致性檢查用）[6-cow][28] |
| `rationale` | {composite_score, strength_score, signals[], narrative} | 選 | **人** | 決策理由（CVD/OI 等），給人與 LLM 上下文，**不影響執行** |
| `execution_policy` | {mode} | 必 | 機器 | **`human_gated`** \| **`demo_only`**（紅線：**永不** `auto_live`，見 §6）|

> 單位鐵則：**整份 schema 不出現「張數/contracts」**。曝險只用 `risk.pct_of_account` + `entry_zone`/`invalidation` 表達；張數在 adapter 用 per-instrument ctVal/contractSize 反算 [2][30]。價格一律報價幣（quote）計價。

### 5.2 範例 A：BTC 永續做多（可由本系統 decision_dict 直接編譯）

```json
{
  "schema": "trade-intent",
  "version": "1.0",
  "intent_id": "ti_2026-06-16T08:32Z_BTC_intraday_a1b2",
  "created_at": "2026-06-16T08:32:00Z",
  "asset_class": "crypto_perp",
  "symbol_canonical": "BTC-USDT",
  "venue_hint": null,
  "side": "long",
  "order_type": "limit",
  "time_in_force": "gtc",
  "post_only": false,
  "reduce_only": false,
  "margin_mode": "isolated",
  "entry_zone": { "low": 64500.0, "high": 64900.0, "reference": 64700.0 },
  "invalidation": { "price": 62100.0, "rule": "4h 收盤跌破 62100（1R 止損）" },
  "risk": {
    "pct_of_account": 1.0,
    "suggested_leverage": 10,
    "max_slippage_pct": 0.15
  },
  "take_profits": [
    { "r_multiple": 1.0, "price": 67300.0, "size_pct": 40, "action": "平40% + 止損移到開倉價" },
    { "r_multiple": 1.5, "price": 68600.0, "size_pct": 30, "action": "平30%" },
    { "r_multiple": 2.0, "price": 69900.0, "size_pct": 30, "action": "平剩餘或移動止損" }
  ],
  "acceptance": {
    "price_band_pct": 0.5,
    "deadline": "2026-06-16T12:32:00Z",
    "max_slippage_pct": 0.15
  },
  "rationale": {
    "composite_score": 2.31,
    "strength_score": 78,
    "signals": [
      { "name": "cvd_divergence", "state": "bull", "note": "看漲背離，斜率 +0.142" },
      { "name": "oi_trajectory", "state": "bull", "note": "OI 穩定上升" },
      { "name": "funding", "state": "neutral", "note": "+0.0042%/8h 中性" }
    ],
    "narrative": "BTC 4h 站上 200MA，閘開啟；日內爆發 setup"
  },
  "execution_policy": { "mode": "human_gated" }
}
```

> 對應現有程式：`symbol_canonical/side` ← `snap["symbol"]/direction`；`entry_zone` ← `entry_low/entry_high/entry`；`invalidation.price` ← `stop`；`risk.pct_of_account` ← `CONFIG.risk_per_trade_pct`；`suggested_leverage` ← `choose_leverage()`；`take_profits` ← `compute_tp_prices()` + `CONFIG.tp_size_split`；`rationale` ← `composite_score/strength_score/confirmed[]`（見 `telegram_bot/message_format.py:render_fire_message`）。

### 5.3 範例 B：美股訊號（純訊號，無自動執行路徑）

```json
{
  "schema": "trade-intent",
  "version": "1.0",
  "intent_id": "ti_2026-06-16T13:45Z_NVDA_swing_c3d4",
  "created_at": "2026-06-16T13:45:00Z",
  "asset_class": "equity_signal",
  "symbol_canonical": "NVDA",
  "venue_hint": null,
  "side": "long",
  "order_type": "limit",
  "time_in_force": "gtc",
  "post_only": false,
  "reduce_only": false,
  "margin_mode": null,
  "entry_zone": { "low": 168.0, "high": 171.0, "reference": 169.5 },
  "invalidation": { "price": 159.0, "rule": "日線收盤跌破 159（結構轉弱）" },
  "risk": {
    "pct_of_account": 1.0,
    "suggested_leverage": 1,
    "max_slippage_pct": 0.3
  },
  "take_profits": [
    { "r_multiple": 2.0, "price": 190.5, "size_pct": 50, "action": "平半" },
    { "r_multiple": 3.0, "price": 201.0, "size_pct": 50, "action": "平剩餘" }
  ],
  "acceptance": {
    "price_band_pct": 1.0,
    "deadline": "2026-06-20T20:00:00Z",
    "max_slippage_pct": 0.3
  },
  "rationale": {
    "composite_score": 1.8,
    "strength_score": 71,
    "signals": [
      { "name": "trend_4h", "state": "bull", "note": "盤外永續實測有真實波動" },
      { "name": "higher_lows", "state": "bull", "note": "高低點抬升" }
    ],
    "narrative": "美股訊號（週末/盤外掃描）；現貨/CFD 標的，無加密永續槓桿語意"
  },
  "execution_policy": { "mode": "human_gated" }
}
```

> 美股無「永續合約張/ctVal」概念，`suggested_leverage:1`、`margin_mode:null`，凸顯 schema 跨資產類仍成立（`asset_class` 切換語意）。

### 5.4 「給人看 vs 給機器」一覽
- **給人看（rationale 區 + 每個欄位的人話 `rule`/`action`/`note`）**：composite_score、strength_score、signals、narrative、invalidation.rule、take_profits.action。
- **給機器執行（其餘扁平數字欄位）**：side、order_type、time_in_force、entry_zone、invalidation.price、risk、acceptance、execution_policy。
- 兩者同一份 JSON、分區存放，LLM-executor 可同時拿到「做什麼」與「為什麼」。

---

## 6. 落地步驟

1. **新增 `intent_format.py`（與現有 message_format 並存，不取代）**：寫一個 `to_trade_intent(decision_dict) -> dict`，輸入就是現有 `render_fire_message` 吃的同一個 `decision_dict`，輸出上面的 JSON。現有 Telegram HTML 渲染**完全不動**——intent 是「機器版」，HTML 是「人看版」，兩者同源同一個 decision。
2. **附在 FIRE 訊息旁**：Telegram 卡片底下加一顆按鈕「📋 複製可執行 JSON」/或 `/intent <symbol>` 指令吐出 intent JSON，讓人或外部 AI agent 取用。不改現有按鈕流程（✅已下單/⏭略過）。
3. **schema 校驗**：用 JSON Schema（draft 2020-12）定義並在 CI 驗證範例；version 欄位做版本化（借 ERC-7521 模組化版本思路 [23-erc]）。
4. **adapter 邊界（可選、後置）**：若要真的 resolve 成訂單參數，寫 `intent_to_ccxt(intent, exchange) -> (args, params)`，由 **CCXT** 吃掉六套 REST 差異 [16][17]；張數用該所 market metadata（ctVal/contractSize/precision）反算 [2][30]。**此 adapter 的輸出只到「參數預覽」或「demo 盤下單」。**
5. **demo / 紅線（不可違反）**：
   - `execution_policy.mode` 只允許 `human_gated` 或 `demo_only`，**程式層硬拒 `auto_live`**。
   - 已知環境事實：連線的 `okx-trade-mcp` 是**實盤**（`system_get_capabilities` 回報 `demo:false`、可下真單）[10][okx-realmoney-catch 記憶]，因此本系統**絕不**把 intent 自動餵進該 MCP 下單；自動路徑一律走 demo guard 正向證明的模擬盤。
   - 事後一致性檢查（成交價是否落在 `acceptance.price_band_pct`/風險%）只在 demo 或人工回報後執行，用來「驗證結果」，不是觸發自動加倉。

---

## 7. ⚠️ 誠實批判（紅線③：過度設計 / leaky abstraction / 維護負擔）

**哪些是過度設計、不該硬做**
- **不要在通用 schema 放張數/contracts**：這是六家最大的洩漏（OKX 張、Bybit 線性幣本位/反向張、Gate 有號張…）[2]，且 OKX 同一單生命週期單位還會變 [31]。硬通用化等於替每個 instrument 維護 ctVal 對照表，這正是 CCXT 用 market metadata 處理、刻意不放進核心欄位的東西 [35]。
- **不要在 intent 通用化單向/雙向 position mode**：那是帳戶全域狀態、不是訂單欄位，Binance 改它會套全 symbol 且有倉就被拒 [33]，CCXT 自己都在 one-way 模式出包 [34]。塞進 schema 只會製造假通用。
- **不要重寫六套交易所 REST mapping**：對現貨 + 簡單線性永續，CCXT 已是現成共同接口（markets/tickers/balances/createOrder + 統一 triggerPrice/stopLossPrice）[25]。自造 = 繼承「N 交易所 × 頻繁 API 變動」的維護成本，且沒有上游社群分攤 [27-maint]。

**什麼情況直接用 CCXT 就夠、不必自造**
- 只要是「把一筆已決定好的訂單送到某一家所」——常規現貨、簡單線性 USDT 永續、市價/限價/帶觸發單——**直接 `ccxt.createOrder(...)` + 必要的 `params`** 即可 [16][17][18]。再造一層平行通用下單 schema 是冗餘。

**leaky abstraction 必然殘留處（要老實標示）**
- TP/SL inline vs 拆獨立 algo 單（Binance -4120 遷移）[7]、reduceOnly 在 Bitget 是字串且只單向生效 [4]、Gate 市價=price'0'+ioc [7]——這些 adapter 一定要 per-exchange 分支，**通用 intent 只能表達「我要 1R 止損 / 三段止盈」的意圖，不能假裝抹平執行差異**。CCXT 官方也明說 unified API 只是共同子集 + params 逃生艙 [23]。

**維護負擔（結構性，非一次性）**
- 任何自造通用層都繼承「per-exchange 支援度不齊 + 交易所持續 breaking change」的維護流 [27-maint]。**因此本設計刻意把自造範圍縮到最小：只造「intent 描述 + 人話理由」（CCXT 不做的部分），執行差異全外包給 CCXT/MCP。** intent 層穩定、變動慢；adapter 層才是隨交易所變動的薄殼。

**信心標示**
- Binance TP/SL algo 遷移與 -4120：**medium**（官方 changelog + freqtrade #12610）[7]。
- OKX `attachAlgoOrds` inline 細節：**medium** [7]。
- 「執行型 MCP 在 2026 底成默認」：**low**（廠商部落格預測，視為市場情緒非事實）[14]。
- 「MCP 是交易 de-facto 標準」：MCP 開放標準/2024-11 起源 high，但「交易專用 de-facto」是綜合推論 medium [11]。
- BingX 幣/張 per-instrument：**medium**（依各 instrument 設定）[2]。

---

## 參考來源

[1] OKX API docs-v5（六家下單端點/標的欄位交叉比對）— https://www.okx.com/docs-v5/en/
[2] Bybit v5 create-order（張數 vs 幣本位 vs 反向；OKX/Gate 張數）— https://bybit-exchange.github.io/docs/v5/order/create-order
[3] Bitget Place-Order（side+tradeSide；Bybit positionIdx；OKX posSide；Gate 號位）— https://www.bitget.com/api-doc/contract/trade/Place-Order
[4] Bitget Place-Order（reduceOnly YES/NO 只單向；Gate auto_size）— https://www.bitget.com/api-doc/contract/trade/Place-Order
[5] OKX docs-v5（tdMode 強制 inline；Bitget marginMode inline）— https://www.okx.com/docs-v5/en/
[6] Binance USDⓈ-M REST（leverage 為獨立端點，六家共識）— https://developers.binance.com/docs/derivatives/usds-margined-futures/trade/rest-api
[7] Binance New-Algo-Order（TP/SL 遷移/-4120；Bybit/Bitget/BingX inline；OKX attachAlgoOrds）— https://developers.binance.com/docs/derivatives/usds-margined-futures/trade/rest-api/New-Algo-Order
[8] OKX Agent Trade Kit（okx-trade-mcp 工具化下單）— https://www.okx.com/en-us/learn/agent-trade-kit
[9] Anoma intents（宣告式 vs 命令式）— https://anoma.net/blog/an-introduction-to-intents-and-intent-centric-architectures
[10] OKX agent-trade-kit（system_get_capabilities demo:false 實盤）— https://github.com/okx/agent-trade-kit
[11] Anthropic MCP（開放標準/事實標準）— https://www.anthropic.com/news/model-context-protocol
[12] Bitget MCP / Tradier MCP（多所跟進）— https://www.bitget.com/asia/amp/academy/best-official-crypto-exchange-mcp-servers-ai-agents-2026-introduction-to-bitget-mcp
[13] Agentic payment 協定（x402/AP2，鄰接非同類）— https://www.crossmint.com/learn/agentic-payments-protocols-compared
[14] Cryptohopper 2026 預測（low 信心）— https://www.cryptohopper.com/blog/the-2026-guide-to-crypto-mcp-servers-13080
[15] FIX 4.2 NewOrderSingle（ClOrdID/Side/OrdType/Price/StopPx）— https://www.b2bits.com/fixopaedia/fixdic42/message_New_Order_Single_D.html
[16] CCXT Manual（createOrder 簽名）— https://github.com/ccxt/ccxt/wiki/Manual
[17] CCXT issue #19086（params 逃生艙）— https://github.com/ccxt/ccxt/issues/19086
[18] CCXT Manual（subset + params override）— https://github.com/ccxt/ccxt/wiki/Manual
[19] CCXT issue #13822（觸發單 alias 與分批 rollout）— https://github.com/ccxt/ccxt/issues/13822
[20] CCXT docs（has[]/.features 能力旗標）— https://docs.ccxt.com/#/README?id=placing-orders
[21] CCXT FAQ（unified 刻意隱藏端點）— https://github.com/ccxt/ccxt/wiki/FAQ
[22] ISO 20022 setr.010 Subscription Order — https://www.iotafinance.com/en/SWIFT-ISO20022-Message-setr-010-001-Subscription-Order.html
[23] CCXT Manual（unified=共同子集，非全通用）— https://github.com/ccxt/ccxt/wiki/Manual
[23-erc] ERC-7521 generalized intents（穩定核心 + 模組化版本）— https://blog.essential.builders/introducing-erc-7521-generalized-intents/
[24-ux] UniswapX DutchOrder（純結果約束，不綁場所）— https://github.com/Uniswap/uniswapx-sdk/blob/main/src/order/DutchOrder.ts
[24] UniswapX architecture（reactor/filler/ResolvedOrder）— https://developers.uniswap.org/contracts/uniswapx/architecture
[25] CCXT docs（已支援統一方法清單）— https://docs.ccxt.com/
[26] CoW Protocol solvers（簽名約束 + 結算強制驗證；批次拍賣）— https://docs.cow.fi/cow-protocol/concepts/introduction/solvers
[27] EIP-7521（鏈上原子驗證對照 CEX 缺口）— https://eips.ethereum.org/EIPS/eip-7521
[27-maint] CCXT issues（per-method 支援不齊 + 持續變動維護流）— https://github.com/ccxt/ccxt/issues
[28] EIP-7521 + CoW/UniswapX（CEX 須自補事前編譯 + 事後驗證，medium）— https://eips.ethereum.org/EIPS/eip-7521
[29] ICT/SMC 進場區方法論（intent vs order 心智模型，medium）— https://www.bhterminal.com/en/insights/how-to-find-entry-zones-in-crypto-trading
[30] OKX docs-v5（sz/ctVal/ctMult，單一 base-amount 欄位不足）— https://www.okx.com/docs-v5/en/
[31] NautilusTrader OKX 整合（amount 單位生命週期內變動）— https://nautilustrader.io/docs/latest/integrations/okx/
[33] Binance Change-Position-Mode（hedge/one-way 帳戶全域狀態，-4067/-4068）— https://developers.binance.com/docs/derivatives/usds-margined-futures/trade/rest-api/Change-Position-Mode
[34] CCXT issue #17817（one-way 模式 createOrder 出包）— https://github.com/ccxt/ccxt/issues/17817
[35] CCXT Manual（最小核心 + 把單位/精度/模式推到 metadata/params）— https://github.com/ccxt/ccxt/wiki/Manual

> 本機程式對照：`telegram_bot/message_format.py`（`render_fire_message`，decision_dict 來源）、`l2_trigger/leverage.py`（`choose_leverage`/`compute_position`/`compute_tp_prices`，intent 數字欄位來源）、`botconfig.py`（`CONFIG.risk_per_trade_pct`/`tp_r`/`tp_size_split`/`sl_pct`）。


---
*本報告由背景研究 Session（多 agent 對抗式查證，40 條發現）自動產出，供過目；尚未落地任何交易邏輯。由 Claude Code 自行撰寫。*
