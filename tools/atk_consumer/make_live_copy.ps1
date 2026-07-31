# make_live_copy.ps1 - build live copy (risk params scaled for account) + rebuild schedule
# -GenerateOnly : rebuild consume_intents_live.py ONLY (no schtasks changes, no live round).
#                 Use this to ship a template fix to the live copy without re-arming anything.
#
# v156【靜默 no-op 治本】.Replace() 找不到字串時不報錯也不回傳筆數：模板那幾行只要
# 改個空格或改個預設值，對應代換就會**靜默沒發生**，這支腳本照樣印 OK，而真錢副本
# 會沿用 demo 級參數（風險 100U＝5 倍、名義值 3000U＝5 倍、日熔斷 300U＝5 倍）。
# 改法：代換表變成單一真相 $subs；每個錨點必須在模板中剛好命中 1 次，否則直接中止、
# 一個字都不寫出去（fail-closed）；代換完再複驗一次舊值全消失、新值全在。
# tests/test_live_copy_parity.py 會直接解析下面的 $subs 表，模板改壞錨點當場紅。
param([switch]$GenerateOnly)
$ErrorActionPreference = "Stop"
$base = "C:\Users\user\OneDrive\桌面\交易機器人\tools\atk_consumer"

# ⛔ 單一真相表：模板 → 真錢副本的每一處代換。格式固定為 @('<from>', '<to>', '<label>')，
#    每行一筆（tests/test_live_copy_parity.py 逐行解析，不要改成別的寫法）。
#    ⛔ intent_outbox 是刻意共用的（訊號來源同一份），故不在表中。
$subs = @(
  @('PROFILE = "demo"',            'PROFILE = "live"',              'profile'),
  @('RISK_USD = 100.0',            'RISK_USD = 20.0',               'risk 1R 20U'),
  @('RISK_USD_CAP = 150.0',        'RISK_USD_CAP = 30.0',           'risk cap 30U'),
  @('NOTIONAL_CAP_USD = 3000.0',   'NOTIONAL_CAP_USD = 600.0',      'notional cap 600U'),
  @('DAILY_STOP_USD = 300.0',      'DAILY_STOP_USD = 60.0',         'daily stop 60U'),
  @('WEEKLY_STOP_USD = 750.0',     'WEEKLY_STOP_USD = 150.0',       'weekly stop 150U'),
  @('LEVERAGE = 5',                'LEVERAGE = 20',                 'leverage cap 20x'),
  @('atk_consumer_state.json',     'atk_consumer_live_state.json',  'state file'),
  @('atk_positions.json',          'atk_positions_live.json',       'positions file'),
  @('atk_consumer_health.json',    'atk_consumer_live_health.json', 'health file'),
  @('prof.get("demo") is True',    'prof.get("demo") is False',     'demo guard inverted')
)

# 1) create live copy (risk 20U / notional 600U / daily stop 60U / weekly 150U; separate state files)
$s = [System.IO.File]::ReadAllText((Join-Path $base "consume_intents.py"), [System.Text.Encoding]::UTF8)
foreach ($sub in $subs) {
  $from = $sub[0]; $to = $sub[1]; $label = $sub[2]
  # 邊界規則：錨點後面若接數字／小數點就不算命中。純子字串比對會讓 `LEVERAGE = 5`
  # 命中改成 `LEVERAGE = 50` 的模板，代換後變成 `LEVERAGE = 200`＝200 倍槓桿上真錢。
  $pat = [regex]::Escape($from) + '(?![\d.])'
  $hits = ([regex]::Matches($s, $pat)).Count
  if ($hits -ne 1) {
    throw ("代換錨點『$label』在模板中命中 $hits 次（應為 1 次）——模板被改過而本表沒跟上。" +
           "若放行，真錢副本會靜默沿用 demo 級風險參數。已中止，未寫出任何檔案；" +
           "請同步修 `$subs 表後重跑（tests/test_live_copy_parity.py 可先驗）。")
  }
  $s = [regex]::Replace($s, $pat, $to.Replace('$', '$$'))
}
# 複驗：舊值必須全數消失、新值必須全數在場（防「代換到一半」）
foreach ($sub in $subs) {
  if ($s.Contains($sub[0]))       { throw ("代換後模板舊值仍在：『" + $sub[2] + "』——已中止，未寫檔") }
  if (-not $s.Contains($sub[1]))  { throw ("代換後真錢值不在場：『" + $sub[2] + "』——已中止，未寫檔") }
}
[System.IO.File]::WriteAllText((Join-Path $base "consume_intents_live.py"), $s, (New-Object System.Text.UTF8Encoding $true))
Write-Host ("1) live copy created OK（" + $subs.Count + " 處代換全部命中並複驗通過）")

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
