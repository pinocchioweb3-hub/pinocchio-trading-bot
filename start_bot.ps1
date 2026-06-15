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
# 必須用「真」pythoncore 解譯器，不能用 WindowsApps 的 python shim
# （shim 缺套件、甚至只會跳轉 Microsoft Store，會讓 daemon 啟動失敗）。
$py = "C:\Users\user\AppData\Local\Python\pythoncore-3.14-64\python.exe"
if (-not (Test-Path $py)) {
    Write-Error "找不到真 Python 解譯器: $py"
    exit 1
}
$spArgs = @{
    FilePath               = $py
    ArgumentList           = @("-u", "run_bot.py")
    WorkingDirectory       = $cwd
    WindowStyle            = "Hidden"
    RedirectStandardOutput = "$cwd\bot.log"
    RedirectStandardError  = "$cwd\bot.err.log"
}
Start-Process @spArgs
