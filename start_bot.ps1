# TradingBot daemon 啟動腳本（v14）
# - 冪等：先殺掉既有的 run_bot 程序再啟動，不會出現重複 daemon
# - 給 Windows Task Scheduler 開機自啟用，也可手動跑
$cwd = "C:\Users\user\OneDrive\桌面\交易機器人"

# 殺既有 daemon
Get-WmiObject Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -match 'run_bot' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Seconds 2

# 啟動新 daemon（detached + log 重導）
$py = (Get-Command python).Source
Start-Process -FilePath $py -ArgumentList @("-u", "run_bot.py") `
    -WorkingDirectory $cwd -WindowStyle Hidden `
    -RedirectStandardOutput "$cwd\bot.log" `
    -RedirectStandardError "$cwd\bot.err.log"
