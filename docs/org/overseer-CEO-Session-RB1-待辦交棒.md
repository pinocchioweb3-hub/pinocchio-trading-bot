# CEO Session RB-1 待辦交棒清單（監督員 Layer 2 維護）

> 用途：把監督員（無人值守、不動 daemon）每輪反覆發現、但**必須走 RB-1 防踩踏流程**才能落地的
> daemon 常駐模組修補，集中成單一可執行清單。下一個 **CEO Session（有人值守）** 直接照此執行，
> 免得每輪 overseer 重新發現、散落在 log 行裡浪費 token。
>
> 最後更新：**2026-06-23 第67輪（now≈1782209053）**。狀態快照：daemon PID **52788** 健康
> （01:03:53 啟動、穩定逾 5h、liveness ~8min 新鮮、err.log absent、watchdog 武裝）、唯一真 blocker = 紅線①人類閘。
>
> **🔄 第67輪更新（監督員親驗活樣本，非舊快照）**：
> 1. **demo 吞吐漏斗（r64 量化，本輪覆核）**：54 嘗試 → 37 拒(68%) → 17 掛單 → 8 entry_expired 餓死(47%) → 7 成交 → 1 pending＝端到端轉換 ~13%。**但此 54 筆全為重啟前歷史**：本輪讀 demo_trades 原文，最新一筆 entry_at 距今 **~5h（即重啟當下）**，自重啟以來 **0 筆新 demo 嘗試**（daemon scanned=15 / fires=0＝無新訊號通過品質閘）。
> 2. **拒單側（含 not_on_okx）已大致治本、非當前活卡點**：本輪親讀 `demo_operator.py` 確認——not_on_okx 已有**雙層防護**：(a) intake 預過濾 `_okx_demo_universe`（v84 task#8，`:117`）只挑 OKX 模擬盤可交易幣；(b) 送單前 `fetch_okx_contract_spec` 對 BadSymbol/BadRequest 優雅攔截並標 `not_on_okx`+誠實措辭（task#73，`:243-264`）。**自重啟以來 0 新拒單**。故 r64 next_step「先修拒單68%」已**部分過時**——拒單是重啟前歷史殘量，不是活卡點。
> 3. **真·當前活卡點＝上游出單量近零（fires=0）＋掛單後餓死（task#61 Step B）**，皆 daemon 軌、須人在場走 RB-1。優先序更正：**(1) task#61 Step B 餓死治本（深回踩到期轉市價，解端到端轉換）＞ (2) ①d 樣本速度 vs 品質閘權衡＞ (3) 拒單側僅監看，勿再當主修點**。
>
> **⚠️ 本輪重大校正（監督員親驗 exit_reason 原文，非舊快照）**：先前各輪 ledger 把卡點摘成
> 「OKX 51004：下單超過可用保證金 → 請補模擬盤保證金」。**這是雙重誤判，使用者不需補保證金**：
> 1. **51004 被 `_short_reject_hint` 貼錯標**。OKX 原文是「訂單數量超過**該槓桿層級的最大持倉上限**
>    （ENA-USDT-SWAP 在當前 3× 槓桿上限 10,600 張，本單欲下 11,363 張），請降槓桿或換子帳戶」——
>    且明載「**position quantity: 0, pending orders: 0**」＝**帳戶無持倉、無保證金耗盡、無幽靈倉**。
> 2. **51004 只是 37 筆累計拒單中『最後一筆』**，且是 8.4h 前唯一一次嘗試。自 01:04 重啟以來，
>    模擬盤操盤手**總共只嘗試下單 1 次（ENA，因上述張數超限被拒），0 筆成交**。
> 真正卡點＝**demo 出單吞吐量近乎零（8.4h 僅 1 單）＋該單踩到「未尊重 OKX 單一商品最大持倉分層」
> 的下單尺寸 bug**，皆 CEO 程式碼軌（RB-1），**非使用者保證金動作**。

---

## ⛔ 唯一真 blocker（純使用者側，監督員/CEO 都不可代行）
- **紅線①**：真錢實倉每筆人手按 → 目前 **0/30**（Phase0 解鎖門檻）。
- **Phase0 三閘並滿**：真實 30 筆人手實單 + 律師確認 + 本人拍板。
- demo 拒單**不是** blocker（已校正解決，見下）。請勿再以「請改保證金模式」或「請補保證金」當 escalation。

---

## RB-1 待辦（須 CEO Session 走防踩踏流程：停 daemon→改→py_compile→測→單次乾淨重啟→驗 PID/err.log/loops/liveness→重啟 watchdog）

### ① 【最高價值·使用者直接所見】Layer1 ledger writer 改「近窗」計拒單，消除誤導性 escalation
- **問題**：ledger 的 `demo_rejected`/`next_step`/`demo_reject_hint` 計的是**累計**拒單（目前恆顯 36 筆 +「請改保證金模式」），與實況不符（近 ~15h 僅 1 筆、拒單早已基本解決）。使用者每次看 ledger 都被誤導成「持續卡在拒單」。
- **根因定位**：
  - `l3_dispatcher/demo_journal.py:337 count_rejected()` → `SELECT COUNT(*) FROM demo_trades WHERE status='rejected'`，**無時間窗**。
  - `l3_dispatcher/ceo_oversight.py:65-69` escalation 判定用累計 `_attempts = demo_n + demo_rejected`。
- **修法**：`count_rejected()` 加近窗過濾（如 `AND entry_at >= now-48h`，建議參數化 `window_sec`）；escalation 改用近窗拒單率而非累計。保留累計值另存欄位供透明，但 next_step 文案以近窗為準。**建議同時回傳拒因『分佈』而非只取最後一筆**——只取最後一筆會讓 ledger 被單一陳舊拒單綁架（這正是 51004 假象的來源）。
- **驗收**：修後 ledger `demo_rejected` 應降為近窗值（近 24-48h），next_step 不再出現「請改保證金模式／請補保證金」舊假說。
- **安全**：純呈現/計數邏輯，不碰訊號數學（strength.py / eval_cvd_divergence 不動）。
- **【v94 8426719 已完成一半｜監督員 r53】**`next_step()` 不再硬寫「改帳戶模式 51010」——改為據實際最常見拒因 reason-aware 給建議（51010 主導才提帳戶模式；否則明說「系統端參數、你無須調整 OKX」）；並把 `not_on_okx` 白話化。純函式＋已測（27/27 綠），已 push，待下次乾淨重啟生效。**仍待辦（①a 另一半）**：`count_rejected()` 仍是**累計無時間窗**（`demo_rejected` 恆顯 37）→ 須加 `window_sec` 近窗過濾、累計值另存欄位。escalation 判定（`_attempts = demo_n + demo_rejected`）仍用累計，須改近窗率。
- **親驗附註**：本輪（r53）讀 demo_trades 原文確認——**37 筆拒單全為重啟前歷史，最新一筆（ENA 51004）距今 ~9.8h；自 ~71min 前乾淨重啟以來零新拒單、僅 1 筆 pending（LIT）**。故近窗化後 `demo_rejected` 近值應 ≈ 0。真當前卡點＝吞吐量（①d），非拒單。

### ①b 【一行字串·高價值·治誤導】修正 `_short_reject_hint` 對 51004 的錯誤標籤
- **【狀態待核·監督員 r53】**本輪親驗 `demo_journal.py:386` 現行碼已是「OKX 51004：下單張數超過該槓桿層級最大持倉上限…（非餘額問題）」＝**此項疑似已治**（非保證金文案）。下個 CEO Session 請先 `grep -n 51004 l3_dispatcher/demo_journal.py` 確認後即可勾掉本項，勿重做。
- **問題**：`l3_dispatcher/demo_journal.py:372` 把 `"51004"` 標成「**下單超過可用保證金**」。但 OKX 51004 在本案的原文是「訂單數量超過**該槓桿層級最大持倉上限**」（與保證金無關）。這個錯標讓監督層每輪都把卡點誤導成「保證金不足→請補額度」。
- **修法**：改為如「OKX 51004：下單數量超過該槓桿層級的最大持倉上限（請降槓桿或縮小部位）」。純字串，零邏輯/零訊號變更。
- **驗收**：重啟後 ledger 不再以「可用保證金」描述 51004。

### ①c 【真·當前卡點·治本】demo 下單尺寸須尊重 OKX「單一商品最大持倉分層」上限
- **問題（自 01:04 重啟以來唯一一次嘗試就踩到）**：ENA 單欲下 11,363 張，但 ENA-USDT-SWAP 在當前槓桿上限僅 10,600 張 → 51004 直接被拒。小市值幣的 `maxMktSz`/持倉分層上限低，現行尺寸計算未夾此上限。
- **定位**：`l3_dispatcher/demo_operator.py` 下單尺寸計算（contracts 換算後、送單前）。
- **修法**：送單前查該 instrument 在當前槓桿的 max position（OKX `instruments`/`position-tiers`），把 contracts 夾到上限內（或對小市值幣自動降名目/降槓桿）。避免整單被拒。
- **安全**：模擬盤路徑（demo_guard x-simulated-trading=1），不碰真錢（紅線①）、不碰訊號數學。

### ①d 【吞吐量觀察·非 RB-1 但需 CEO 判斷】demo 出單量近乎零
- 自 01:04 重啟 8.4h，demo 操盤手**僅嘗試 1 單**。即使拒單全治好，以此速率也難在合理時間湊滿 demo 樣本。
- 可能成因＝`is_quality_signal`（R:R≥1.5）品質閘 × 近期低波動少訊號。**非 bug**（保守是 by-design），但 CEO 應有意識權衡「樣本速度 vs 品質門檻」，必要時於模擬盤放寬（紙上仍記全部）。

### ② 【✅ 大致已治·監督員 r67 親驗，僅監看】task#54 宇宙洩漏濾除（not_on_okx / 51155 / BadSymbol）
- **問題（歷史）**：美股代號曾洩漏進 OKX 永續路由（17 筆 BadSymbol）。
- **r67 親驗現況**：`demo_operator.py` 已有**雙層防護**——(a) intake 預過濾 `_okx_demo_universe`（v84 task#8，`:117`）只放行 OKX 模擬盤可交易幣；(b) 送單前 `fetch_okx_contract_spec` 對 BadSymbol/BadRequest 優雅攔截、標 `not_on_okx` + task#73 誠實措辭（`:243-264`，非把有效訊號永久誤標）。**自 01:03 重啟以來 0 新 not_on_okx 拒單**。→ **此項視為大致已治，下個 CEO Session 僅需監看，勿重做**；若仍想加強，可考慮把 `_okx_demo_universe` 快取 TTL 與失敗降級行為覆核一次（純強化、非 blocker）。

### ③ 【低優先·罕見邊界】51048 PEPE TP 價格序檢查
- 歷史一筆拒單（51048，TP 價序，PEPE）。`demo_operator.py` TP 價格相對進場/方向的序檢查邊界。非系統性，可併入 ② 一起做。
- **勿重修 51121**（張數規格已由 bec9056 / 06-21 治好，**本輪親驗：自 01:04 重啟以來 51121 復發數＝0**，治本確認有效）。

### ④ 【housekeeping·非 blocker】清 3 筆陳舊 `closing:` 殘留標記
- `demo_operator_state` 殘留 3 筆 `closing:` 標記未清：`INJ-bull`(8.7h)、`OP-bull`(8.7h)、**`LAB-bull`(47.8h)**。
- **非保證金元兇**（OKX 親回 position quantity=0，帳戶無持倉），純 KV 清理漏洞——close 流程啟動後標記未被移除。疑為 v92 同步治本的殘留邊界。
- **修法**：close 確認完成（或 OKX 回報該倉已不存在）後刪除對應 `closing:` 標記；啟動時對 >N 小時的 stale closing 標記做一次性清掃。

---

## 非 RB-1 backlog（程式碼軌，CEO 可一般推進）
- **task#61 Step B**：`resolve_entry_policy` 消費 + D（深回踩）到期轉市價（學習半已 LIVE，執行半待建，走 RUNBOOK#26）。
- **task#68 gated 半**：free universe 改源的 gated 半（shadow 半已 LIVE，flip 須過 EV 回測閘，topN_agreement 僅 0.2 須注意改源會實質改 chosen）。

---

## 監督員紀律備忘
- demo 拒單真因已**三度校正定案**（r27→r29→**第52輪**）：51121/51010 早已治、自重啟以來近窗近乎零。**勿再重啟舊假說 escalation**（含「請補保證金」這個 51004 衍生的新假象）。
- **第52輪定案（讀 exit_reason 原文，非 ledger 摘要）**：51004 不是保證金問題＝是「張數超該槓桿層級持倉上限」；OKX 親回帳戶 0 持倉；自 01:04 重啟僅 1 次嘗試（ENA，被上述尺寸 bug 拒）。真卡點＝吞吐量＋尺寸夾上限，非保證金、非帳戶模式。
- 上述 ①②③ 全觸 daemon 常駐模組 → 監督員無人值守**不動 daemon**，僅維持輕量心跳直到狀態改變或 CEO 上線。
- 三紅線不可跨：①真錢只人手、②對外只逐次人工、③不捏造績效。
