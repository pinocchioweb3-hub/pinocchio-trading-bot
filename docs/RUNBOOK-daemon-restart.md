# RUNBOOK：安全重啟 daemon（改任何被 daemon 載入的模組後）

> 目的：把已驗證的安全重啟流程固化成一張表，杜絕「舊 daemon 跑舊碼、新碼戳記基準錯位」
> 這類過渡瑕疵（見 v49 liveness 首次重啟誤觸一則斷層告警的成因）。
> 適用：任何時候改到 daemon 會 `import` 的模組（`run_bot.py`、`l3_dispatcher/*`、
> `l2_trigger/*`、`market_intel_mcp/*`、`botconfig.py`、`telegram_bot/*` …）。

## 為什麼需要這張表

- daemon 在「啟動那一刻」把所有模組 import 進記憶體。**之後改檔案不會生效，直到重啟。**
  → 改完若忘了重啟＝以為修好了其實沒生效（假完成）。
- 但「重啟」本身有踩踏風險：若新碼引入語法錯/缺 import，舊 daemon 已被殺、新 daemon 起不來
  → 變成**整段時間沒有任何掃描**（最糟的失敗模式）。
- 所以：**先在不動 daemon 的前提下證明新碼可開機，再做唯一一次切換。**

## 標準流程（每次都照走，不跳步）

1. **改完所有檔**（一次把這批要改的都改完，避免多次重啟）。

2. **靜態自檢（不碰 daemon）** — 用真解譯器逐檔編譯：
   ```powershell
   $py = "C:\Users\user\AppData\Local\Python\pythoncore-3.14-64\python.exe"
   & $py -X utf8 -m py_compile run_bot.py l3_dispatcher\<改到的檔>.py ...
   ```
   有任何 `SyntaxError` → **停**，修好再來，**不要殺 daemon**。

3. **可開機性自檢（不碰 daemon）** — 確認每個改過的模組 import 得起來：
   ```powershell
   & $py -X utf8 -c "import importlib; [importlib.import_module(m) for m in ['l3_dispatcher.ceo_session','l3_dispatcher.liveness']]"
   ```

4. **跑離線測試套件（不碰 daemon）**：
   ```powershell
   & $py -X utf8 -m pytest -q
   ```
   只接受「全綠，或僅剩既有已知紅（如 stale 環境相關）」。新出現的紅 → **停**，先修。

5. **唯一一次切換** — 用 `start_bot.ps1`（它自己會冪等地先殺舊 run_bot、再起新的）：
   ```powershell
   powershell -ExecutionPolicy Bypass -File .\start_bot.ps1
   ```
   ⚠️ `start_bot.ps1` **必須是 UTF-8 BOM 編碼**，否則 Big5/950 會解析失敗 → 整晚斷線。

6. **重啟後驗證（4 點全綠才算成功）**：
   - **單一進程**：`Get-WmiObject Win32_Process -Filter "Name='python.exe'" | ? {$_.CommandLine -match 'run_bot'}` 只回 1 筆（不是 0、不是 2+）。
   - **真解譯器**：該進程的 CommandLine 指向 `pythoncore-3.14-64\python.exe`，非 WindowsApps shim。
   - **err.log 乾淨**：`bot.err.log` 無新的 traceback / 409 Conflict（409＝有兩個 daemon 搶同一 Telegram bot）。
   - **各 loop online**：`bot.log` 出現各 worker 的 `loop online`、`backend=coinglass`、tier 幣數。

## 已知過渡瑕疵（預期、會自癒，不必驚慌）

- 若這次重啟「之前」daemon 真的斷線過（當機/休眠/手動關 > 1 小時），新 daemon 啟動時
  `liveness.check_gap()` 會推**一則**斷層告警到系統頻道——這是**正確行為**（事後通知）。
- 但若是「從沒戳記過的舊碼」第一次換到「有 liveness 的新碼」，戳記基準可能錯位、
  誤報一則。**一次性、下次重啟就靜默**（新碼每輪都會戳記）。看到一則別重查。

## 紅線提醒（重啟流程不碰這些）

- 重啟＝純本機行為，**不等於**推送公開（紅線②）、不等於下任何單（紅線①）、不碰金鑰。
- 重啟前的改動若包含對外/真錢/Phase0 相關，那些仍要走各自的人類放行閘，不因重啟而生效。

---
*由 Claude Code（CEO 角色）建立於 v50；對應 task #26。*
