# TradingBot daemon 啟動腳本（v14）
# - 冪等：先殺掉既有的 run_bot 程序再啟動，不會出現重複 daemon
# - 給 Windows Task Scheduler 開機自啟用，也可手動跑
$cwd = "C:\Users\user\OneDrive\桌面\交易機器人"

# 殺既有 daemon
Get-WmiObject Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -match 'run_bot' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Seconds 2

# v126: 重啟會覆寫 bot.err.log → 舊行程的崩潰痕跡（驗屍證據）被清掉（2026-07-09
# daemon 無聲消失即因此無從診斷）。啟動前先把非空 err.log 附時間戳歸檔，檔案封頂 ~200KB。
$errLog = "$cwd\bot.err.log"
$archive = "$cwd\bot.err.archive.log"
if ((Test-Path $errLog) -and ((Get-Item $errLog).Length -gt 0)) {
    Add-Content -Path $archive -Value "`n===== archived $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') =====" -Encoding UTF8
    Get-Content $errLog -Encoding UTF8 | Add-Content -Path $archive -Encoding UTF8
    if ((Get-Item $archive -ErrorAction SilentlyContinue).Length -gt 200KB) {
        $tail = Get-Content $archive -Tail 1000 -Encoding UTF8
        Set-Content -Path $archive -Value $tail -Encoding UTF8
    }
}

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
