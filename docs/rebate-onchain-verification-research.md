# 返佣透明化 / 鏈上即時驗證 研究報告

> 本文件記錄「讓返佣與收益可被社群獨立驗證」這條路線的研究演進。
> 一切結論皆經多源搜尋 + 對抗式查證（adversarial verification）並標註來源與時效。
> **時效基準：2026-06。此領域變化極快，引用前請以官方最新文件為準。**

---

## 0. 背景與緣起

專案目標：把「創辦人從交易所拿到的返佣與收益」做到**社群不需要相信創辦人主動公開的帳本**，而是能自己獨立查證；若哪天查不到資料，就等於專案出事（dead man's switch / 斷線即警訊）。

此需求由 Threads 社群成員 **bett.erlife1003** 在建造日誌討論串中提出（「帳本不是主動提供，而是讓大家有公開鑰匙可以查詢」），經公開採納後納入路線圖。這正是「鏈上化即時驗證」的精神。

研究分兩輪進行：
- **第一輪**：CEX 返佣能否鏈上化去信任驗證？有哪些第三方技術可協助？
- **第二輪**：對使用者提供的兩份 Gemini（Google 網頁 AI）開發草案做事實查證 —— Hyperliquid/Aster 返佣機制、Chainlink 可行性、DEX 篩選。

---

## 1. 一句話結論（Bottom Line）

> **中心化交易所（CEX）的返佣「原生」做不到鏈上即時去信任驗證**；能對上需求的技術是 zkTLS（信任打折）。
> **去中心化交易所（DEX）也不是自動就 trustless** —— 多數「DEX」（含 Aster、ApeX）的返佣會計仍在交易所式後台、只能透過 API 取得，與 CEX 的信任缺口相同。
> **真正能讓社群逐筆鏈上驗證抽成的，是 Hyperliquid 的 builder codes 機制**（非 referral）；referral 的「未領取金額」仍只能信 Hyperliquid 自家 API。
> **GMX 的推薦碼則是天生永久寫進智能合約**，鏈上可查性最強（但量較小）。
> 因此採**雙軌透明度**：CEX/API 類走「主動公開帳本」，DEX 可鏈上查的欄位走「鏈上逐筆驗證」，並**誠實標示哪一塊是 trustless、哪一塊不是**。

---

## 2. 第一輪：CEX 返佣鏈上化現況

| 機制 | CEX 有嗎 | 證明什麼 | 與返佣的關係 |
|---|---|---|---|
| Proof of Reserves（OKX：Merkle + zk-STARK，2022-11／zk-STARK 2023-04／V2 2024-09） | ✅ | 「儲備 ≥ 用戶餘額」（償付能力） | **零關係**，PoR 文件全篇不提 affiliate/rebate |
| 返佣資料 | ❌ 無任何原生鏈上/密碼學機制 | — | 只存交易所內部 DB，私有鑑權 API 取純 JSON |

**關鍵缺口**：OKX affiliate API（`GET /api/v5/affiliate/invitee/detail`）的 HMAC 簽章**只簽「請求」、不簽「回應內容」**，所以第三方無法證明貼出的數字是 OKX 真的回的、未被竄改。

**能補這個缺口的技術（依信任假設由髒到乾淨）**：

1. **zkTLS / Web Proofs（Reclaim、Primus）** — 唯一能密碼學證明「資料真的來自 okx.com 該 API、未竄改」的原語。先例：elizaOS plugin-primus 對 Binance BTC 價格做 zkTLS 證明；ZKP2P 正式環境用 Reclaim。
   - ⚠️ 信任邊界：**需信任 attestor/proxy 不串謀**。單一 attestor ≠ 真去信任；自架 = 又變成信任專案方。去中心化 attestor 網路（Reclaim EigenLayer AVS）仍進行中。
2. **預言機（Chainlink 標準 feed / API3）+ EAS** — 只證明「誰簽的、傳輸沒被改」，**不證明來源 API 真的回了這個值** → 解半套。Chainlink DECO（zkTLS）是例外但仍 sandbox（2024-10 公開）。
3. **改用本身就在鏈上的 DEX 返佣** — 見第 3 節。

來源：[OKX Affiliate FAQ](https://www.okx.com/en-us/help/affiliate-faq)、[OKX PoR repo](https://github.com/okx/proof-of-reserves)、[Reclaim attestor-core](https://github.com/reclaimprotocol/attestor-core)、[Reclaim security FAQ](https://blog.reclaimprotocol.org/posts/security-faq)、[Primus docs](https://docs.primuslabs.xyz/data-verification/tech-intro/)、[CoinDesk 批 Chainlink PoR](https://www.coindesk.com/tech/2023/07/05/chainlink-proof-of-reserve-proves-little-beyond-data-going-in-coming-out)

---

## 3. 第二輪：DEX 路線深入查證（Gemini 草案勘誤）

### 3.1 Gemini 草案「數字算術」—— 全對 ✅

Hyperliquid 官方機制可精確重現草案數字：
- 被推薦人前 **$25M** 量享 **4%** 手續費折扣；推薦人賺被推薦人手續費的 **10%**（扣折扣後）；上限為每位被推薦人各自前 **$1B** 量；需先累積 **$10,000** 量才能生成推薦碼。
- 滿額理論上限：100% Taker → **$44,955**、100% Maker → **$14,985**、50/50 → **$29,970**。
- 那 ~0.1% 折讓（$45,000→$44,955）機制正確：4% 折扣只套在第一個 $25M 段（25M/1B×4%=0.1%）。

來源：[Hyperliquid Referrals](https://hyperliquid.gitbook.io/hyperliquid-docs/referrals)、[Fees](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/fees)

### 3.2 Gemini 草案「鏈上去信任架構」—— 兩處硬傷 🚨

#### ❌ 硬傷一（載重）：Hyperliquid referral 返佣金額 **不是**原生鏈上可獨立查的

- referral 狀態（`referrerState`、`cumVlm`、`unclaimedRewards`）只記在 **HyperCore L1**，唯一查詢管道是 Hyperliquid **自營中心化 HTTP API**（`POST api.hyperliquid.xyz/info, type=referral`）。
- **連自架開源節點都還不支援**這個查詢（Chainstack 明載 "open-source node implementation does not support it yet"）。
- HyperEVM 的 13 個讀取 precompile（0x800–0x80C）**無任何 referral 欄位**，CoreWriter 也無 referral 寫入。
- **結論**：就「返佣金額」而言，去信任缺口**與 CEX 私有 API 等價**。要鏈上可驗證仍需 zkTLS/oracle。

> ✅ 但有一個例外救星 → **builder codes**（見 3.3）。撮合本身（訂單/成交/清算）是共識層可驗證的。

#### ❌ 硬傷二（載重／時效）：Chainlink Functions 不支援 HyperEVM 且**即將停運**

- **Chainlink Functions** 不在 HyperEVM 支援清單，且**整個產品已宣布停運**（測試網 2026-06-02、主網 2026-09-01），官方要求遷移到 **CRE**。→ Gemini「方案一」是建在即將下線的產品上。
- **Chainlink CCIP** ✅ 已於 2025-06-27 在 HyperEVM 主網上線可用 —— 但官方只定位為「**代幣橋接**」，**是否支援任意資料/可程式化訊息未經官方證實**，不可假設。Gemini「方案二」傳輸層可行，但跨鏈帶「貢獻資料」那段需先向 Chainlink 確認。
- 即使用 CRE/oracle 去查 HL API，**本質仍是 oracle 信任模型**（多數節點看到 API 這樣回 ≠ 對鏈上狀態的密碼學證明）。

來源：[Chainlink Functions 支援網路+停運](https://docs.chain.link/chainlink-functions/supported-networks)、[CCIP on HyperEVM 目錄](https://docs.chain.link/ccip/directory/mainnet/chain/hyperliquid-mainnet)、[CCIP HyperEVM 整合指南](https://docs.chain.link/ccip/tools-resources/network-specific/hyperliquid-integration-guide)、[CRE 上線](https://blog.chain.link/chainlink-runtime-environment-now-live/)、[Chainstack referral 端點](https://docs.chainstack.com/reference/hyperliquid-info-referral)、[HyperEVM precompile 清單](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/hyperevm/interacting-with-hypercore)

### 3.3 正解：builder codes 才是機器人專案的核心變現＋可驗證軌

| | builder codes | referral |
|---|---|---|
| 費率 | 開發者自訂（每筆訂單帶 `{b,f}`，**f 單位＝十分之一基點**，f=10=1bp=0.01%，上限 perps 0.1%/spot 1%） | 官方固定，推薦人不可改 |
| 適合角色 | **程式化代下單者（＝我們的機器人）** | 口碑獲客 |
| 對使用者 | **加在標準手續費「之上」的服務費（機器人賺）→ 是成本，不是折扣** | 給使用者 4% **折扣** |
| 授權 | 使用者**主錢包**簽 `ApproveBuilderFee`（非 API/agent 錢包），可撤銷，最多 10 個 | 被推薦人手動輸入碼 |
| 鏈上可驗證性 | **強** —— 每筆 fee 由 HyperCore validators 出塊時鏈上執行，可逐筆 attribution，`maxBuilderFee` 端點查即時上限 | 弱 —— 對外只能透過中心化 info API 取聚合值 |
| 結算 | 累積後需主動 claim（>$1）入 HyperCore L1 spot 餘額（**非 HyperEVM ERC-20/event log**） | 同上 |

> **前置條件**：builder 帳戶需 ≥100 USDC 永續價值、用 standard 帳戶抽象模式，否則 builder 訂單被拒。

### 3.4 草案其他勘誤（摘要）

- **maker 返佣未定**：官方 Fees 頁寫「10% of **taker** fees」，referrals 頁卻寫廣義「fees」。若實際僅返 taker，則 100% Maker → **$0**、50/50 → 約 **$22,477**，**不要把 maker 返佣當定論**，須以實際 dashboard 對帳。
- **單位陷阱**：builder fee 的 f 是「十分之一基點」，填錯會少收 10 倍或誤觸上限。
- **主體錯置**：$1B 上限綁「每位被推薦人各自的量」非「邀請人錢包的量」；$10k 門檻官方未限定「主錢包總量」。
- **費率會變**：勿寫死，引用標「以官方最新費率表為準」。

---

## 4. DEX 篩選評估（取代 Gemini 的 CMC 截圖排序）

> ⚠️ 那張 CMC 表是依「**未平倉量 OI**」排序，不是 24h 量。用「24h 量 ÷ OI」周轉率可揪出刷量：

| DEX | 24h 量 | OI | 周轉率 | 解讀 |
|---|---|---|---|---|
| Hyperliquid | $193B | $137B | **1.4x** | ✅ 真實流動性 |
| Aster | $29.6B | $28.1B | 1.05x | 帳面健康，但量能另有刷量爭議（見下） |
| Dmex | $23.3B | $8.8B | 2.6x | ⚠️ 偏高 |
| Pacifica | $19.4B | $2.27B | 8.5x | 🚩 可疑 |
| **ApeX Omni** | **$53.7B** | **$3.45B** | **15.6x** | 🚩🚩 **極可疑（疑似積分農場刷量）** |

**串接優先序建議（依「真實量 × 鏈上可驗證性 × 整合難度」）**：

1. **🥇 Hyperliquid** — 真實量最大（永續 DEX 市佔約 44%、唯一市佔上升者）、builder codes 成熟且鏈上逐筆可驗。**首選核心串接**。
2. **🥈 GMX** — 推薦碼**第一次下單即永久寫入智能合約、返佣由合約累積**，**鏈上可查性最強**、最易程式化驗證（Arbitrum/Avalanche）。量較小但 trustless 程度最高。**最適合當「鏈上驗證示範」的標竿**。
3. **🥉 dYdX** — 鏈上即時撥付、聯盟佣金最高 50%，但 Cosmos appchain 需另一套整合棧。
4. **觀察級：Aster** — 見 4.1，**不宜列首選**。
5. **排除：ApeX Omni** — 15.6x 周轉率刷量嫌疑、API 式後台、鏈上可查性未證實。

### 4.1 Aster vs Astar 釐清 + 保留

- 使用者說的「ASTR」幾乎可確定指 **Aster（代幣 ASTER，CZ/YZi Labs 支持的永續 DEX）**，**不是 Astar Network（ASTR，Polkadot 公鏈，不是 DEX）**。草案若出現 ASTR ticker/Astar 合約即為硬錯誤。
- 對 Aster 三個保留：
  1. 已於 **2026-03-17 上線自有 L1「Aster Chain」**並逐步從 BNB Chain 遷移，目標鏈須以當下實際部署確認。
  2. 帳面量（曾達日約 $1000 億）**被 DefiLlama 於 2025-10 以疑似刷量下架**（與 Binance 永續量幾近完美相關、不提供掛單/吃單底層數據）。
  3. 返佣「鏈上可查」**未經證實**（官方文件只說獎勵入「Aster 帳戶」、每日結算，偏交易所式後台帳本）。

來源：[GMX Referrals](https://docs.gmx.io/docs/referrals)、[dYdX Affiliate](https://www.dydx.foundation/blog/dydx-affiliate-program)、[Hyperliquid 市佔](https://yellow.com/news/hyperliquid-perpetual-dex-volume-share)、[DefiLlama 下架 Aster](https://cointelegraph.com/news/defillama-delist-aster-perp-data-integrity)、[Aster referral 文件](https://docs.asterdex.com/program-and-rewards/referral-program)

---

## 5. 務實建議路徑（三階段）

**【現在可做 0–1 月】**
1. 核心變現定為 **Hyperliquid builder codes**：每筆成交帶 `{b,f}`（f 用十分之一基點、保守值、perps≤0.1%）。
2. 接入流程由**使用者主錢包**簽 `ApproveBuilderFee`，UI 誠實標明「這是加在標準手續費之上的機器人服務費，非折扣，可隨時撤銷」。
3. referral 只當行銷附加層（使用者輸入碼拿 4% 折扣）。
4. 透明度先走「**主動公開帳本**」：後端定期把每筆 builder fee/對應成交/已領未領整理成公開儀表板/CSV，標明資料來源。

**【中期 1–4 月】**
5. 加「**DEX 鏈上可查欄位**」雙軌：對 builder fee 提供可重現查核腳本（任何人用 `maxBuilderFee` 端點 + HyperCore 成交/`RewardsClaim` ledger 事件交叉重算）。**誠實區分**：builder fee 逐筆可鏈上驗；referral 未領取累積仍只能信 HL API。
6. 若要跨鏈分發/公示，用 **CCIP**（已上線）而非 Functions；部署前必做：CCIP directory 逐條確認 USDC 受支援 + lane 方向開通、向 Chainlink 確認是否支援任意訊息、把「RPC 離線→訊息卡住」風險寫進設計。
7. 評估把 **GMX 推薦**當「鏈上驗證示範標竿」（合約原生可查，最好講故事）。

**【終局 4 月+，視需求】**
8. 若要返佣金額真正 trust-minimized：加 **zkTLS（TLSNotary/Reclaim 類）或 oracle** 把 HL info API 回應證成鏈上事實（重工程，且仍要信 API 當下回真）。
9. 設計成「**可降級信任假設**」：HyperEVM precompile/開源節點能力仍在快速演進，未來若官方新增 referral precompile 即可移除對中心化 API 的相依。

---

## 6. 雙軌透明度策略（使用者拍板方向）

> CEX 礙於技術無法全民即時鏈上驗證 → 採「**主動公開帳本**」；同時提供 **DEX 管道**讓大家主動鏈上驗證。比例由用戶選擇。

| 軌道 | 對象 | 透明度方法 | 是否 trustless |
|---|---|---|---|
| A. 主動公開帳本 | OKX/Binance/Gate/Bitget/BingX + Aster/ApeX | API 抓取 → 公開儀表板/快照 + 斷線即警訊 | ❌ 信任最小化，非零信任（須誠實標示） |
| B. 鏈上逐筆驗證 | Hyperliquid builder fee、GMX 推薦合約 | 鏈上事件/合約狀態，第三方可獨立重算 | ✅ （builder/GMX 合約部分） |

---

## 7. 合規紅線（不可忽略）

對最終用戶**公開推廣 DEX/CEX 返佣連結**可能觸及各地對加密衍生品**招攬/返佣**的監管（呼應既有認知：台灣返佣、OKX 招攬立法收緊）。所有對外推廣前須：加 KYC/地區限制與法律免責、必要時取得法律意見。**勿假設可無限制公開招攬。**

---

## 8. 待實測 / Open Questions

1. HL referral 10% 是只計 taker 還是含 maker？官方措辭不一致 → 須以實際 dashboard 對帳。
2. CCIP 在 HyperEVM 是否支援任意資料/可程式化訊息？官方僅證實代幣橋接 → 須向 Chainlink 確認。
3. 要分發的 USDC 是否為 HyperEVM CCIP 受支援代幣、lane 方向是否開通（目前僅約 8 個支援代幣）。
4. CRE（Functions 後繼）是否正式涵蓋 HyperEVM（官方尚未明確，unverified）。
5. Aster 遷 Aster Chain 後返佣帳本/合約所在鏈、是否有鏈上可查欄位 → 串接前用獨立鏈上數據實測。
6. Gemini 第二份草案的 Hyperliquid「質押階梯差額 Staking Tier Diff」、ApeX Omni 返佣細節 → 未深度查證，列為次要待驗。
7. 各地（尤其台灣）加密衍生品返佣/招攬合規邊界 → 需法律意見。

---

## 9-bis. 第三輪查證：Aster 定案 + Hyperliquid 文案誠實校正（2026-06-14）

針對「近期公開要大家綁 Hyperliquid + Aster 邀請碼」做上線前查證，結論明確：

### ❌ Aster 無法做到與 Hyperliquid 同等的第三方獨立鏈上驗證（三層全過不了）
1. **返佣本身＝中心化後台帳本**：官方文件明載「每日 00:00 UTC 計算、09:00 UTC 更新、隔日轉入你的 Aster 帳戶」，入帳到內部帳戶餘額。全套官方文件/智能合約清單/公開 API 目錄**找不到任何返佣合約地址、事件或查詢端點**。返佣只能登入 Aster 後台 UI 看，第三方無帳戶看不到。（對比：GMX 有公開 `ReferralStorage` 合約可在 Arbiscan 直接讀。）
2. **底層成交也驗不了**：Aster Pro 鏈下撮合 CLOB、鏈上只結算；新 Aster Chain L1 **預設開啟 Account Privacy**——訂單 ZK 加密、一次性隱身地址、「訂單資料從不以明文上鏈」、暗池隱藏單。第三方要讀某用戶逐筆成交，**必須由用戶主動提供 viewer pass 解密**——定義上就不是「獨立」驗證。
3. **量能誠信存疑**：DefiLlama 2025-10-05 因 Aster 量與 Binance 永續近 1:1 鏡像、「無法提供足夠細節供獨立驗證」而下架；兩週後應 Aster 要求復列，但 0xngmi 仍稱「依然是黑盒」。Aster **不提供節點軟體**，無人能自跑節點稽核（這是它與 Hyperliquid/GMX 的根本差異）。

→ **可誠實說「帳面量居前段」，但絕不能對 Aster 掛「鏈上可驗證」標籤。** Aster 改列「高流動性選項（後台返佣，不可鏈上驗）」獨立分類。
來源：[Aster referral 文件](https://docs.asterdex.com/program-and-rewards/referral-program)、[Aster 智能合約清單](https://docs.asterdex.com/overview/smart-contracts)、[AsterScan /verify](https://aster-scan.com/verify)、[DefiLlama 復列但驗證仍有缺口](https://coincentral.com/defillama-relists-aster-perpetual-data-despite-verification-gaps/)

### ⚠️ 誠實校正：連 Hyperliquid 也不是「純鏈上一鍵可查」
前面把 Hyperliquid builder fee 講成「純鏈上、人人可逐筆讀」**是我過度宣稱**。實情：realized 逐筆 builder fee 是撮合引擎產物，**HyperCore 沒有像以太坊那樣的公開區塊瀏覽器/開放 RPC 能讀出逐筆 builder fee**；要拿到「builder 地址＋金額」必須**自跑 Hyperliquid（非開源）節點 `--write-fills`、或下載官方 S3 桶**再 join `replica_cmds` 重放。

→ 這比「信任單一 REST API」可審計性高很多，但仍依賴 Hyperliquid 自家軟體/基礎設施。**對外文案的精確措辭**：
- ✅ 可寫：「Hyperliquid 返佣/builder fee **可由任何人自架節點獨立重算對賬，不需信任單一 API**」
- ❌ 不可寫：「純鏈上、區塊瀏覽器一鍵可查」

### 🥇 真正「一鍵公開合約可讀」的只有 GMX
GMX 的 `ReferralStorage` 是公開合約，任何人在 Arbiscan 直接讀，**連節點都不用自架**。可驗證性是三者最強（代價：量小）。

### referral vs builder 的關鍵分叉（影響「綁邀請碼」這個動作）
| 路徑 | 收益來源 | 可驗證性 | 需要什麼 |
|---|---|---|---|
| **GMX 綁推薦碼** | referral | ✅ 公開合約一鍵可讀（最乾淨） | 用戶手動交易即可 |
| **Hyperliquid builder codes** | builder fee | 🟡 自架節點可重算（強，非一鍵） | **機器人需替用戶路由下單**（深整合） |
| **Hyperliquid 綁推薦碼** | referral | 🔴 僅 HL 中心化 API（開源節點不支援該端點）≈ 跟 CEX 一樣 | 用戶手動交易 |
| **Aster 綁邀請碼** | referral | 🔴 後台帳本，不可第三方驗 | — |

> ⚠️ 重要：本專案目前是**訊號機器人（用戶手動下單）**。若近期只做「綁邀請碼」漏斗 = 走 **referral**。而 **Hyperliquid 的 referral 反而不可鏈上驗（只能 HL API）**；真正可驗證的 Hyperliquid builder fee **需要機器人實際替用戶路由下單**（更深整合，且觸及代下單議題）。因此若要「綁碼即可鏈上驗證」的乾淨故事，**現階段最契合的其實是 GMX**。

### 上線前 do-now（校正後）
1. 對外「可驗證 DEX」軌**只放 Hyperliquid + GMX**，移除 Aster。
2. Aster 改列「高流動性選項（後台返佣，不可鏈上驗）」，邀請碼可放但文案明說「以 Aster 官方後台為準、無法第三方鏈上重算」，並一句帶過 DefiLlama 下架事實。
3. Hyperliquid 文案改用「自架節點獨立重算、不需信任單一 API」，不要寫「一鍵鏈上查」。
4. 釐清要推 referral（綁碼即可、GMX 可驗 / HL 不可驗）還是 builder codes（HL 可驗但需路由下單）——這是產品深度的抉擇。
5. Aster 設「升軌條件」（開放節點軟體／返佣上鏈合約／DefiLlama 解除疑慮），達標前維持觀察。

---

## 9. 變更紀錄（Evolution Log）

| 日期 | 事件 |
|---|---|
| 2026-06-14 | 社群 bett.erlife1003 提出「被動驗證 / 公開鑰匙查詢 / 斷線即警訊」，公開採納入路線圖 |
| 2026-06-14 | 第一輪研究：CEX 返佣鏈上化現況 + zkTLS/oracle/EAS 比較（22 源、25 主張對抗查證、0 推翻） |
| 2026-06-14 | 第二輪查證：Gemini 兩份 DEX 草案事實核對（Hyperliquid/Aster/Chainlink）；推翻「DEX 返佣＝原生鏈上可查」假設，確立 builder codes 為核心軌 |
| 2026-06-14 | 第三輪查證（上線前）：**Aster 定案不可鏈上驗證**（後台帳本＋預設隱私＋刷量爭議）；**誠實校正 Hyperliquid**（自架節點重算，非一鍵鏈上查）；確認 **GMX 是唯一公開合約一鍵可讀**；釐清 referral vs builder 分叉 |

---

*本報告由自動化研究工作流（多 agent 平行搜尋 + 三票對抗式查證 + 綜合）產出，所有負面/載重結論皆附主要來源。引用前請複查官方最新文件。*
