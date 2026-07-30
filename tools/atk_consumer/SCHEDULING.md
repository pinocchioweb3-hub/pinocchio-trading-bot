# ATK 消費腳本排程指南（一頁版）

> 這支腳本是「你的執行器」：讀 intent 訊號檔 → 換算張數 → 呼叫 okx CLI 下單 →
> 每輪管理在場倉位（對帳／24h 逾時平倉／日虧 300U 熔斷）。
> 原檔硬寫死 demo；真盤＝你自己複製一份改 PROFILE，金鑰只存 ~/.okx/config.toml。

## 先手動確認一次（換完金鑰後）

```bash
python "C:\Users\user\OneDrive\桌面\交易機器人\tools\atk_consumer\consume_intents.py" --dry-run
```

看到指令構造正確、無報錯後再排程。

## 排程（Windows 工作排程器，每分鐘檢查一次）

系統管理員 PowerShell 執行一次：

```bash
schtasks /Create /TN "ATKConsumer" /SC MINUTE /MO 1 /TR "\"C:\Users\user\AppData\Local\Python\pythoncore-3.14-64\python.exe\" \"C:\Users\user\OneDrive\桌面\交易機器人\tools\atk_consumer\consume_intents.py\" --once" /F
```

（`--once`＝跑一輪就退出，由排程器驅動節奏——避免常駐進程洩漏。）

## 停止（kill switch）

```bash
schtasks /Delete /TN "ATKConsumer" /F
```

再到 OKX 後台撤銷 API key ＝ 完全斷電。

## 狀態檔（都在 %LOCALAPPDATA%\TradingBot\）

- `atk_consumer_state.json`：已處理的 intent（冪等去重）
- `atk_positions.json`：在場倉位＋每日已實現損益（熔斷口徑）
- `atk_consumer_health.json`：連續 fail-closed 帳＋告警狀態（v143，見下節）

## 連續 fail-closed 告警（v143）

**為什麼有這層**：2026-07-30 那晚 OKX 因浮動 IP 換掉回 401，整盤 121 次全擋、零成交，
fail-closed 完全正確（一張都沒下、沒有半殘倉、零損失），但**沒有任何告警**——
只寫進 log，而 log 沒有讀者，靠肉眼撞見才發現。
失敗有出口，才算有風控；只寫 log 不算。

- 連續 **3 輪**（每輪 1 分鐘）有故障→發 Telegram；同類故障每 **60 分鐘**再提醒一次；
  **換了故障類別立刻再報**；故障消失→發「✅ 已恢復」並重置冷卻。
- 失敗分流：`auth_ip_whitelist`（要人去後台補白名單，浮動 IP 會復發）／`auth`／
  `cli_missing`／`rate_limit`／`timeout`／`query_fail`／`other`。
  **「查無此單」(51603) 判為良性**——那是冪等查詢的正常答案，誤鳴的告警等於沒有告警。
- 憑證先讀環境變數，再讀專案 `.env`（**排程器環境沒有那兩個變數**，實測確認）。永不列印憑證值。
- 告警文字保留 IP（你補白名單要用），遮蔽 API key 識別碼。
- 告警管道自己失敗也會留痕（`last_alert_channel` / `last_alert_error`）。
- 告警層永不拋例外進交易路徑——監控壞掉可接受，監控把執行器弄掛不可接受。

驗收（零網路、零下單，隨時可重跑）：

```bash
python "C:\Users\user\OneDrive\桌面\交易機器人\tools\atk_consumer\consume_intents.py" --selftest-fail 4
```

## 改了範本要同步到實盤副本

範本改完**不會**自動生效到 `consume_intents_live.py`。只重產副本、不動排程、不跑任何一輪：

```bash
powershell -NoProfile -ExecutionPolicy Bypass -File "C:\Users\user\OneDrive\桌面\交易機器人\tools\atk_consumer\make_live_copy.ps1" -GenerateOnly
```

⛔ 不帶 `-GenerateOnly` 會**重建排程並立刻跑一輪真錢路徑**——那是要人有意識地做的事。

## ⚠️ 過渡期注意

demo 金鑰時期**不要**排程本腳本——demo 帳戶由 daemon 的鏡像操盤手管理，
兩者同帳戶會重複開倉。排程只在「你換上實盤金鑰的副本」後啟用。
