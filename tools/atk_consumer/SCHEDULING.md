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

## ⚠️ 過渡期注意

demo 金鑰時期**不要**排程本腳本——demo 帳戶由 daemon 的鏡像操盤手管理，
兩者同帳戶會重複開倉。排程只在「你換上實盤金鑰的副本」後啟用。
