# memguard 常態清掃為何還沒觸發 + 一個會把「有」量成「沒有」的 UTF-8 量測陷阱（r80）

日期：2026-08-01 06:0x（監督員 Layer 2，第 80 輪）
狀態：全程唯讀量測，未改任何一行程式、未動 daemon、未重啟。

---

## 一、r79 留下的問題：常態清掃入版控了，但線上沒看到它跑過

r79 把並行 session 寫的 `memory_guard()` 兩段式改寫（新增①常態清掃）補進版控（87171e6），
但當時 log 裡 grep 不到「常態清掃」字樣，所以 r79 明確寫下「⛔ 不得宣稱『效能日衰已修』」。

本輪把「為什麼沒跑」量出來了。**結論：條件確實沒滿足，不是碼沒載到。**

### 1-1 碼是活的（先排除「改了沒生效」這個假設）

`PinocchioWatchdog` 是**排程任務、每 3 分鐘起一個新行程**（`pinocchio_watchdog_launch.ps1` → `python -u watchdog.py` 跑完就結束），
不是常駐行程。所以只要檔案存檔，下一次觸發就是新碼。

- `watchdog.py` 最後修改：2026-08-01 03:24（自此凍住不動）
- 排程 Last Run：06:04:01，Last Result = **0**，Next Run 06:07
- ⇒ 新碼自 03:24 起已連續在線上執行約 **2.7 小時**，跑過約 **54 次**，一次都沒觸發常態清掃。

### 1-2 觸發條件當下的實測值

常態清掃的兩個門檻（`watchdog.py`）：commit charge **< 88%**（未達緊急線才走這段）、
且**符合指紋的殭屍 runner ≥ 4 個、且每一個都老於 90 分鐘**。

本輪實測（用 watchdog.py 自己的函式量，不是另寫一套）：

| 量測項 | 實測值 | 門檻 | 是否滿足 |
|---|---|---|---|
| commit charge | **53.1%** | < 88% 才進常態清掃 | ✅ 進得來 |
| 符合指紋的 runner 總數 | 4 個（年齡 169.2 / 122.3 / 62.3 / 2.3 分） | — | — |
| 其中 **≥ 90 分鐘**者 | **2 個** | 需 ≥ 4 個 | ❌ **差 2 個** |
| 距上次 memguard 動作 | 181 分鐘 | 冷卻 600 秒 | ✅ 早就過了 |

**所以是「年齡夠老的只有 2 個、門檻要 4 個」把它擋下來的**——邏輯正確、不是壞掉。
記憶體目前也很健康（53.1%，離 88% 緊急線很遠；相較之下 03:04 那次是 95%）。

### 1-3 因此仍然不能說「效能日衰已修」

- 常態清掃**至今零次線上觸發**（`grep 常態清掃 watchdog.log` = 0），⛔ 沒有任何線上實證。
- 只證明了「它在線上、條件式地待命」，沒證明「它真的救得回使用者說的越來越卡」。
- 下輪的查點不變：`grep "常態清掃" C:\Users\user\AppData\Local\TradingBot\watchdog.log`，
  找到第一行才算線上實證；找到後要順便看那一輪 commit% 與被清的 PID 數。

---

## 二、⚠️ 本輪自踩並抓回的量測陷阱：加 `-X utf8` 會把 4 個殭屍量成 0 個

這條要單獨記，因為**監督員的標準操作指示本身就要求加 `-X utf8`**，
下一輪若照做去量 memguard，會得到一個乾淨漂亮的**錯誤**答案。

### 發生了什麼

`_stale_claude_runners()` 內部呼叫 PowerShell 拿 `claude.exe` 清單，用的是
`subprocess.run(..., capture_output=True, text=True)`——**沒有指定 encoding**，
所以解碼用的是行程當下的 locale 編碼。

- 我第一次量：`python -X utf8 -c ...` → locale 被強制成 UTF-8 → PowerShell 吐的是 **cp950 位元組**
  → reader thread 丟 `UnicodeDecodeError: 'utf-8' codec can't decode byte 0xae`
  → 被函式尾巴的 `except Exception: return []` **吞掉**
  → 回傳空表，印出 **`runners_total 0`**。
- 我第二次量（拿掉 `-X utf8`，跟排程實際跑的方式一致）：`locale.getpreferredencoding()` = **cp950**
  → 正常解碼 → 回傳 **4 個 runner**，年齡 169.2 / 122.3 / 62.3 / 2.3 分。
- 交叉驗證（完全不經 Python，直接 `Get-CimInstance Win32_Process`）：確實 **4 個**符合指紋的 runner。

### 判定

- **這不是生產環境的 bug。** 排程走 `pinocchio_watchdog_launch.ps1`，沒有 UTF-8 模式，locale 是 cp950，實際跑起來是對的。
- **這是量測方法的陷阱。** 「0 個殭屍可清」跟「函式解碼失敗回空表」在輸出上長得一模一樣，
  而且錯的那次連 exit code 都是 0、印出來的數字也很正常。這是本專案第 N 次遇到的同族問題：
  **一個代理值（空表）同時代表「真的沒有」與「我沒看到」。**
- 順帶一提，03:04 那行 log「commit 95% 超緊急線但無符合指紋的殭屍可清」是**排程行程**寫的（cp950 正常），
  所以那句是真的沒得清，不是被這個陷阱騙的。

### 鐵則（下輪照做）

1. **量 watchdog / memguard 相關函式時，不要加 `-X utf8`、不要設 `PYTHONIOENCODING=utf-8`**——
   要跟排程實際跑的環境一致，否則得到的是假的。
2. 任何回傳「空表 / 空字典 / None」的偵測函式，量完要**用第二種完全獨立的方法交叉驗證**
   （這次是直接 `Get-CimInstance`）。空值永遠要問一次：是「真的沒有」還是「我沒看到」。

（若日後要治本，方向是在該 `subprocess.run` 補 `errors="replace"`，讓解碼失敗不可能變成空表。
本輪**刻意不改**：生產路徑目前是對的，且該檔仍屬並行 session 的在製品，沿用 r79「只提交不修改」的處置。）

---

## 三、順手結案：v181–v187 七版的唯讀複驗（r79 的 (b) 項）

並行 session 在 04:16–04:53 之間落地 7 個版本，監督員未複驗過。本輪唯讀複驗：

- **py_compile**：8 個變動檔全過
  （`l3_dispatcher/alt20_watch.py`、`ceo_session.py`、`chart_render.py`、`synthesizer.py`、`wlfi_watch.py`、
  `market_intel_mcp/sources/binance_perp.py`、`market_intel_mcp/wyckoff.py`、`watchdog.py`）
- **測試**：相關 7 支測試檔 **44 項全綠**（0.99 秒）
- **紅線面掃描**：
  - 訊號數學核心 `strength.py` / `eval_cvd_divergence` — **本區間零改動** ✅
  - 新增行內出現 `place_order` / `create_order` / `close_position` / `set_leverage` / `transfer` — **零筆** ✅
  - `wlfi_watch.py`、`alt20_watch.py` 各含 2 處 `display_only` 標記，與 commit 訊息宣稱一致 ✅

⛔ 這是**單元測試層級**的複驗，不是真錢實證，也不是輸出正確性的驗證（圖面/日報內容沒人看過）。

---

## 四、本輪其他盤點值（供下輪差分）

- `class_counts.auth_ip_whitelist` = **1551**（r79 也是 1551）⇒ **凍住未再增長，401 仍是解除狀態**。
  ⛔ 判認證只能用這個差分，不可用 `last_ok_ts`（它仍停在假恢復那一刻）、不可用「沒有故障訊息」。
- `class_counts.orphan_position` = **128**（r79 = 98，**+30**）⇒ 孤兒部位仍是現行唯一真錢擋點，會持續累加。
- `last_fail_class` = `orphan_position`（未換類別）⇒ 依 r79 決策樹＝原狀，走第二項。
- demo 側：`halt` 鍵 **為空**（無殘閂）、`last_cycle_ts` 新鮮（99 秒）、
  `last_cycle_outcome` = `skipped:demo_guard:...` ⇒ 停擺原因未變，仍是 ①B 使用者決策。
  ⚠️ 附帶事實：`demo_operator_state` 現有 **9 筆 `closing:` 殘留標記**（先前文件記 3 筆）——
  這是鏡像停擺的必然副作用（沒有輪次去把它們收掉），不是新 bug，但數量會持續長。
- 組織產出：`org_digest` 兩席（PM 26 天、CoinGlass 25 天）皆 `covered_by_backfill=true`，本輪**無新代補產義務**。

---

## 五、下一輪具體查點

1. `grep "常態清掃" watchdog.log` — 找第一次線上觸發（找到才可談「效能日衰」有無改善）。**量時不要加 `-X utf8`。**
2. `class_counts` 差分：`auth_ip_whitelist` 是否仍凍在 1551；`orphan_position` 增幅。
3. 孤兒部位 WLFI 是否消失（只有使用者能處理，不會自解）。
4. 若進 STALLED 且無更高價值工作：`demo_operator_state` 的 `closing:` 殘留清理**不要自己做**——
   它會動到 demo 路徑，且鏡像本來就停著，清了也只是讓帳面好看。列為觀察即可。
