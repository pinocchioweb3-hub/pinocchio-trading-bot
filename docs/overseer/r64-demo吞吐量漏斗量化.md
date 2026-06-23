# 監督員第 64 輪——demo 吞吐量漏斗量化（throughput funnel quantification）

**產出時間**：2026-06-23（now≈1782203700, UTC）
**監督員狀態判定**：`ADVANCING(code burst 今晨,當前安靜 default;PID52788 穩定~3.1h)` / `BLOCKED_ON_USER(唯一真 blocker=人工紅線①)`
**本輪定位**：等待期並行的安全唯讀工作。前 10 輪（r54–r63）皆心跳並反覆指認「吞吐量近零才是真卡點」，但**從未實際量化**。本輪補上這筆數字。

---

## 一、活樣本親驗（trade_journal.db，mtime 1 分鐘）

| 層 | 數量 | 近 24h | 近 7d | 結論 |
|---|---|---|---|---|
| 紙上訊號 paper_trades | 155（148 closed / 7 open） | 15 | 116 | **訊號產生健康**（~16/日）。paper_n 已破 100 下限，非瓶頸 |
| demo 進場嘗試 demo_trades.entry_at | 54 | 3 | 54 | 嘗試持續，但成交轉換極低 |
| demo 實際成交 filled_at | 7 | 0 | 7 | **最近一筆成交在 25 小時前**——成交端停滯 |

## 二、demo 漏斗拆解（54 筆嘗試的去向）

```
54 進場嘗試
 ├─ 37 被 OKX 拒單 (68%)         ← 卡點①：拒單率
 │     主因 not_on_okx 17/37（標的不在 OKX 永續清單＝系統端篩選）
 └─ 17 被 OKX 接受為掛單
       ├─ 8 entry_expired 未成交 (47%)  ← 卡點②：限價單餓死（入場區太深,價未回踩）
       │     （8 筆 exit_reason 全=entry_expired, pnl=0, R=0；filled_at 正確為 NULL）
       ├─ 7 成交並走完 (timeout/stop/tp)
       └─ 2 pending
```

**端到端成交轉換率 = 7/54 ≈ 13%。**

## 三、關鍵釐清（避免誤判）

1. **`demo_n=7` 是誠實正確的**，非低估。8 筆「closed 但無 filled_at」全屬 `entry_expired`（掛單從未成交→無成交時戳天經地義），不應計為成交。**本輪親讀原始列確認＝非 telemetry bug**（恪守「下結論前讀原文」教訓）。
2. **兩個吞吐殺手皆已被既有任務追蹤，皆屬 daemon 常駐碼，皆正確延後給「人在場」RB-1 防踩踏 session**：
   - 卡點①拒單率 68%（not_on_okx 符號篩選）→ RB-1 ①a / ①c
   - 卡點②餓死率 47%（entry_expired 限價單未成交）→ task#61 Step B（resolve_entry_policy 消費 + D 到期轉市價）。對照 memory `entry-depth-ab-verdict`：Step A 學習半已 LIVE，Step B 執行半待建。
3. **此漏斗全屬模擬盤（proxy），與 Phase 0 解鎖無關**。Phase 0 真錢解鎖閘 = live_n=0/30 真人手單 + 律師 + 本人拍板（紅線①），即使 demo 吞吐修好也只加速開發側樣本累積，**不解鎖 Phase 0**。

## 四、給下一輪／CEO Session 的下一步

- **使用者側（唯一真 blocker·紅線①）**：真錢實單 0/30 + 律師確認 + 本人拍板——AI 不代行。
- **CEO Session（人在場走 RB-1 防踩踏）**：本量化把優先序講清了——**先修卡點①（拒單 68%，影響面最大），再修卡點②（餓死 47%，task#61 Step B）**。兩者修好後 demo 端到端轉換可望從 13% 顯著上升，加速開發側驗證迴圈。
- **監督員側**：維持輕量心跳；見 demo 異常碼／daemon 失活／liveness>30min／人在線推 RB-1 才升級。

---
*本報告純唯讀親驗、未碰 daemon、未跨任何紅線。*
