# 監督員診斷 · 第21輪 · plan_snapshot 缺鍵普查（最高影響缺鍵定序）

- 日期：2026-06-22（台北 ~16:15）
- 觸發：CEO 自 v90/v91（14:52 / 15:09）後 commit_age ~2.4h 無新進展；第20輪已預設「下次喚醒 commit_age > 數小時且無新 commit → 改判 STALLED 推單一安全工作」。本輪據此推進 **CEO 自己定的下一步**：「量 plan_snapshot_health 其餘缺鍵挑最高影響」（承 v91 方法論）。
- 性質：**純唯讀**取證（`trade_journal.db?mode=ro` 普查 51 筆活線 deepdive 進場快照），零下單數學、未碰 daemon 常駐模組、未跨任何紅線。本報告只「定位＋分類＋定序」，**不**落地修復——任何觸 daemon 常駐訊號模組的治本須由 CEO Session 走 RB-1 逐項人工監督。

---

## 一、普查結果（活線 deepdive n=51，parse_err=0，近48h=30）

### context_at_entry 各鍵 None 率
| 鍵 | None 率 | 分類 |
|---|---|---|
| **whale_net** | **92.2%** (47/51) | HL 覆蓋限制（見§二）|
| macro_confluence_score | 25.5% (13/51) | 宏觀層讀末行，部分窗無值 |
| wyckoff_phase | 25.5% (13/51) | 4h 階段未明（盤整/資料不足）|
| htf_aligned | 23.5% (12/51) | 高時框對齊 |
| **oi_delta_pct** | 19.6% (10/51) | snapshot-cluster（共因，見§三）|
| **cvd_slope** | 21.6% (11/51) | snapshot-cluster |
| **top_trader_ratio** | 21.6% (11/51) | snapshot-cluster |
| **btc_above_200ma_4h** | 21.6% (11/51) | snapshot-cluster |
| avg_funding | 7.8% (4/51) | 低 |
| breadth_up_pct | 7.8% (4/51) | 低 |

### regime_at_entry 各鍵 None 率
| 鍵 | None 率 | 分類 |
|---|---|---|
| oi_price_quadrant | 47.1% (24/51) | **已 v91 治本**（盤整死區 honest None，紅線③）|
| funding_state | 21.6% (11/51) | snapshot-cluster |
| cvd_state | 21.6% (11/51) | snapshot-cluster |
| vol_trend | 0.0% (0/51) | 健康（每路徑都帶 ATR 分桶）|

---

## 二、whale_net 92%：是覆蓋限制，不是 bug（不列為下一治本）

接線本身正確（v56-2 已治「資料被丟棄」的 bug，`macro.py:_deepdive_extra_context`）。逐標的查證：

- **有值**只出現在最高流動性標的：BTC / ETH / SOL / WLD（各 1）。
- **缺值**集中在長尾 alt（IP/HYPE/JTO/XPL/NEAR/EIGEN/SEI/TIA/ASTER/PEPE… 共 47 列、36 個標的）。

根因＝資料源 Hyperliquid 鯨魚淨多倉只覆蓋頭部流動性標的，長尾 alt 本就無此資料 → **誠實 None（紅線③）**，與 `data_quality_low`「可接受降級」同哲學。**結構性無法補**（HL 對長尾 alt 沒有鯨魚資料），故：
- ❌ 不要為它接第三方源（ROI 低、長尾 alt 鯨魚資料普遍稀缺）。
- ✅ 建議優化器對 whale_net 採「在場才計分」（macro_confluence 的 `score_whales` 本就缺料→None，不灌水），維持現狀即可。
- 92% 這個高數字**會誤導**為「最大缺口」，但它不可治也不該治——真正可治的是下節。

---

## 三、最高槓桿可治本：snapshot-cluster「整包同生共死」（CEO 下一治本目標）

把 4 個 snapshot 衍生鍵（oi_delta_pct / cvd_slope / top_trader_ratio / btc_above_200ma_4h）逐列交叉：

```
每列在 4 鍵中 None 的個數分佈：{0: 39, 1: 1, 2: 1, 4: 10}
→ 4 鍵全在場 39 列 | 4 鍵全 None 10 列 | 僅 2 列部分缺
```

**雙峰、幾乎沒有部分缺** ⇒ 這 4 鍵（外加 funding_state / cvd_state，同源）是**整包 per-symbol snapshot 同生共死**：約 **10/51（≈20%）的 deepdive 進場，整份 `sym_state.snapshot` 不在場**（`mi_get_snapshot` 缺料或 `.error`），於是 `_record_deepdive_plan` 的 `_snap_for_rv` 為空 → 一次連帶遺失約 6–7 個 context 鍵 ＋ 該列的 oi_price_quadrant。

**為何這是最高槓桿**：修這一個共因，等於同時為那 ~10 列補回 6–7 個鍵，遠勝逐鍵修（whale_net 不可治、oi_price_quadrant 已治）。

### 給 CEO Session 的下一步（走 RB-1，逐項人工監督）
1. **取證那 ~10 列**：撈出 `sym_state.snapshot` 缺席的 deepdive 進場，看共同特徵——
   - 是否撞 **task#64 宇宙截斷**（冷快取 burst 429 截斷 30–92%）：當掃描宇宙被 burst 截斷、某標的 snapshot 未進快取，deepdive 對它建單 → 整包 None。**這是最可能的同一根因**（待 CEO 驗證，非斷言）。
   - 或 deepdive 對「掃描器未覆蓋之標的」建單的時序競態。
2. 若確為 task#64 同根 → **task#68 free_universe gated 半上線**（改免費 OKX/Binance 大宗源解 burst 截斷）會**順帶**把這 ~20% snapshot 缺口一起補上，無須為 snapshot 另開治本任務。建議併入 task#68 gated 半驗收指標：上線後重跑本普查，snapshot-cluster None 率應自 ~20% 降。
3. 純唯讀普查腳本可重跑（見附錄），作為 task#68 前後對照的回歸量尺。

---

## 四、結論與交棒

- **健康**：daemon 單一 PID 33484（v91 15:09 RB-1 乾淨重啟後穩定）、liveness 新鮮、無 err.log、watchdog 啟用、git 同步 0/0。CEO 非停滯，僅 v90/v91 後正常工作間隔。
- **唯一硬 blocker 仍純使用者側**：真錢實倉 0/30（紅線①人手實單）＋OKX demo 保證金模式（解 demo 成交 1/30）＋Phase0 三閘（真30筆／律師／拍板）。should_nudge=false，不硬推被卡決策。
- **本輪交付**：把「whale_net 92%」這個會誤導的表面最大缺口**降級為不可治**，並定位真正可治的最高槓桿＝snapshot-cluster ~20% 整包缺（疑與 task#64 / task#68 同根）。CEO 下一治本目標明確、且很可能**不必新開任務**（併入 task#68 gated 半即可順帶解決）。

### 附錄：可重跑唯讀普查（回歸量尺）
讀 `paper_trades WHERE regime='deepdive' AND plan_snapshot IS NOT NULL`，對每列 `json.loads(plan_snapshot)` 後統計 `context_at_entry` / `regime_at_entry` 各鍵 None 率，並對 snapshot-cluster 4 鍵做逐列 None-個數分佈（驗雙峰＝整包同生共死）。`mode=ro` 連線，零寫入。
