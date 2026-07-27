# Launch MT5 in the active RDP/interactive session (fixes IPC timeout on Windows Server).
# Uses isolated portable install so Session 0 zombie cannot block IPC.
$ErrorActionPreference = "Continue"

$terminal = "C:\Users\Administrator\MT5Agent\terminal64.exe"
$launchBat = "C:\Users\Administrator\MT5Agent\launch.bat"
$enableApi = Join-Path $PSScriptRoot "enable_mt5_api.ps1"
$taskName = "MT5AgentInteractive"
$commonIni = "C:\Users\Administrator\MT5Agent\config\common.ini"

if (-not (Test-Path $terminal)) {
    Write-Host "ERROR: Portable terminal not found at $terminal"
    Write-Host "Run scripts/fix_mt5_session.ps1 once to bootstrap MT5Agent."
    exit 1
}

if (Test-Path $enableApi) {
    & $enableApi -ConfigPath $commonIni
}

# Stop interactive-session MT5 only (leave Session 0 zombie alone — it is not killable).
Get-Process -Name "terminal64" -ErrorAction SilentlyContinue |
    Where-Object { $_.SessionId -ne 0 } |
    Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 2

schtasks /Delete /TN $taskName /F 2>$null | Out-Null
schtasks /Create /TN $taskName /TR "`"$launchBat`"" /SC ONCE /ST 00:00 /SD 01/01/2020 /RU $env:USERNAME /IT /F | Out-Null
schtasks /Run /TN $taskName | Out-Null

Write-Host "Launched MT5 portable in interactive session for user $env:USERNAME"
Write-Host "Waiting 90s for terminal login + IPC dispatcher..."
Start-Sleep -Seconds 90

$proc = Get-Process -Name "terminal64" -ErrorAction SilentlyContinue |
    Where-Object { $_.SessionId -ne 0 }
if ($proc) {
    foreach ($p in $proc) {
        Write-Host "MT5 running PID=$($p.Id) SessionId=$($p.SessionId) Path=$($p.Path)"
    }
} else {
    Write-Host "WARNING: MT5 process not detected in interactive session"
}