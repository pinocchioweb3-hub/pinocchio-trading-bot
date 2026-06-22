# CEO Session RB-1 待辦交棒清單（監督員 Layer 2 維護）

> 用途：把監督員（無人值守、不動 daemon）每輪反覆發現、但**必須走 RB-1 防踩踏流程**才能落地的
> daemon 常駐模組修補，集中成單一可執行清單。下一個 **CEO Session（有人值守）** 直接照此執行，
> 免得每輪 overseer 重新發現、散落在 log 行裡浪費 token。
>
> 最後更新：2026-06-22 第33輪（now≈1782142449）。狀態快照：daemon PID 33484 健康、
> demo 拒單已基本解決（見 r29 定案 0f36407）、唯一真 blocker = 紅線①人類閘。

---

## ⛔ 唯一真 blocker（純使用者側，監督員/CEO 都不可代行）
- **紅線①**：真錢實倉每筆人手按 → 目前 **0/30**（Phase0 解鎖門檻）。
- **Phase0 三閘並滿**：真實 30 筆人手實單 + 律師確認 + 本人拍板。
- demo 拒單**不是** blocker（已校正解決，見下）。請勿再以「請改保證金模式」當 escalation。

---

## RB-1 待辦（須 CEO Session 走防踩踏流程：停 daemon→改→py_compile→測→單次乾淨重啟→驗 PID/err.log/loops/liveness→重啟 watchdog）

### ① 【最高價值·使用者直接所見】Layer1 ledger writer 改「近窗」計拒單，消除誤導性 escalation
- **問題**：ledger 的 `demo_rejected`/`next_step`/`demo_reject_hint` 計的是**累計**拒單（目前恆顯 36 筆 +「請改保證金模式」），與實況不符（近 ~15h 僅 1 筆、拒單早已基本解決）。使用者每次看 ledger 都被誤導成「持續卡在拒單」。
- **根因定位**：
  - `l3_dispatcher/demo_journal.py:337 count_rejected()` → `SELECT COUNT(*) FROM demo_trades WHERE status='rejected'`，**無時間窗**。
  - `l3_dispatcher/ceo_oversight.py:65-69` escalation 判定用累計 `_attempts = demo_n + demo_rejected`。
- **修法**：`count_rejected()` 加近窗過濾（如 `AND entry_at >= now-48h`，建議參數化 `window_sec`）；escalation 改用近窗拒單率而非累計。保留累計值另存欄位供透明，但 next_step 文案以近窗為準。
- **驗收**：修後 ledger `demo_rejected` 應降為近窗值（近 24-48h），next_step 不再出現「請改保證金模式」舊假說。
- **安全**：純呈現/計數邏輯，不碰訊號數學（strength.py / eval_cvd_divergence 不動）。

### ② 【低優先】task#54 宇宙洩漏濾除（not_on_okx / 51155 / BadSymbol）
- **問題**：美股代號曾洩漏進 OKX 永續路由（17 筆 BadSymbol，24h+ 前已停，但根因濾除未治本）。
- **定位**：`l3_dispatcher/demo_operator.py`（下單前符號白名單/路由閘）。
- **修法**：下單前對 OKX 永續 universe 做存在性濾除，非 OKX 上市符號直接跳過不送單。

### ③ 【低優先·罕見邊界】51048 PEPE TP 價格序檢查
- 最近 ~15h 唯一一筆拒單（51048，TP 價序）。`demo_operator.py` TP 價格相對進場/方向的序檢查邊界。非系統性，可併入 ② 一起做。
- **勿重修 51121**（張數規格已由 bec9056 / 06-21 治好，45h+ 零復發）。

---

## 非 RB-1 backlog（程式碼軌，CEO 可一般推進）
- **task#61 Step B**：`resolve_entry_policy` 消費 + D（深回踩）到期轉市價（學習半已 LIVE，執行半待建，走 RUNBOOK#26）。
- **task#68 gated 半**：free universe 改源的 gated 半（shadow 半已 LIVE，flip 須過 EV 回測閘，topN_agreement 僅 0.2 須注意改源會實質改 chosen）。

---

## 監督員紀律備忘
- demo 拒單真因已**兩度校正定案**（r27→r29）：51121/51010 早已治、近窗近乎零。**勿再重啟舊假說 escalation**。
- 上述 ①②③ 全觸 daemon 常駐模組 → 監督員無人值守**不動 daemon**，僅維持輕量心跳直到狀態改變或 CEO 上線。
- 三紅線不可跨：①真錢只人手、②對外只逐次人工、③不捏造績效。
