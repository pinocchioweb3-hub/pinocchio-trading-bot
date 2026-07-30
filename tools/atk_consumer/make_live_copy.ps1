# make_live_copy.ps1 - build live copy (risk params scaled for account) + rebuild schedule
# -GenerateOnly : rebuild consume_intents_live.py ONLY (no schtasks changes, no live round).
#                 Use this to ship a template fix to the live copy without re-arming anything.
param([switch]$GenerateOnly)
$ErrorActionPreference = "Stop"
$base = "C:\Users\user\OneDrive\桌面\交易機器人\tools\atk_consumer"

# 1) create live copy (risk 20U / notional 600U / daily stop 60U / weekly 150U; separate state files)
$s = [System.IO.File]::ReadAllText((Join-Path $base "consume_intents.py"), [System.Text.Encoding]::UTF8)
$s = $s.Replace('PROFILE = "demo"', 'PROFILE = "live"')
$s = $s.Replace('RISK_USD = 100.0', 'RISK_USD = 20.0')
$s = $s.Replace('RISK_USD_CAP = 150.0', 'RISK_USD_CAP = 30.0')
$s = $s.Replace('NOTIONAL_CAP_USD = 3000.0', 'NOTIONAL_CAP_USD = 600.0')
$s = $s.Replace('DAILY_STOP_USD = 300.0', 'DAILY_STOP_USD = 60.0')
$s = $s.Replace('WEEKLY_STOP_USD = 750.0', 'WEEKLY_STOP_USD = 150.0')
$s = $s.Replace('LEVERAGE = 5', 'LEVERAGE = 20')
$s = $s.Replace('atk_consumer_state.json', 'atk_consumer_live_state.json')
$s = $s.Replace('atk_positions.json', 'atk_positions_live.json')
$s = $s.Replace('atk_consumer_health.json', 'atk_consumer_live_health.json')
$s = $s.Replace('prof.get("demo") is True', 'prof.get("demo") is False')
[System.IO.File]::WriteAllText((Join-Path $base "consume_intents_live.py"), $s, (New-Object System.Text.UTF8Encoding $true))
Write-Host "1) live copy created OK"

if ($GenerateOnly) {
  Write-Host "GenerateOnly: schedule untouched, no round executed. DONE"
  exit 0
}

# 2) rebuild schedule (path has no spaces, no inner quotes needed)
schtasks /Delete /TN "ATKLiveConsumer" /F 2>$null | Out-Null
schtasks /Create /TN "ATKLiveConsumer" /SC MINUTE /MO 1 /TR "powershell -NoProfile -ExecutionPolicy Bypass -File $base\run_live_once.ps1" /F | Out-Null
Write-Host "2) schedule ATKLiveConsumer rebuilt OK (every minute)"

# 3) run one round now to verify
& powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $base "run_live_once.ps1")
Write-Host "3) first round executed; log tail:"
Get-Content "$env:LOCALAPPDATA\TradingBot\atk_live.log" -Tail 5 -ErrorAction SilentlyContinue
Write-Host "DONE"
