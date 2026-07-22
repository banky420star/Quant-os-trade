# Fix MT5 IPC on Windows Server: bootstrap portable MT5Agent, launch in Session 3, verify Python IPC.
$ErrorActionPreference = "Continue"

$project = "C:\Users\Administrator\Desktop\new task\mt5_quant_agent"
$srcTerminal = "C:\Program Files\MetaTrader 5"
$agentRoot = "C:\Users\Administrator\MT5Agent"
$agentTerminal = Join-Path $agentRoot "terminal64.exe"
$launchBat = Join-Path $agentRoot "launch.bat"
$srcCommon = "C:\Users\Administrator\AppData\Roaming\MetaQuotes\Terminal\D0E8209F77C8CF37AD8BF550E51FF075\config\common.ini"
$agentCommon = Join-Path $agentRoot "config\common.ini"
$killScript = Join-Path $project "scripts\kill_mt5_session0.ps1"
$killTask = "MT5KillSession0"
$launchTask = "MT5AgentInteractive"
$testTask = "MT5ConnectTest"
$testBat = Join-Path $project "scripts\mt5_connect_test_task.bat"
$enableApi = Join-Path $project "scripts\enable_mt5_api.ps1"

function Write-Step([string]$msg) {
    Write-Host ""
    Write-Host "=== $msg ==="
}

function Get-Mt5Snapshot {
    Get-Process -Name "terminal64" -ErrorAction SilentlyContinue | ForEach-Object {
        $owner = $null
        try {
            $own = Invoke-CimMethod -InputObject (Get-CimInstance Win32_Process -Filter "ProcessId=$($_.Id)") -MethodName GetOwner -ErrorAction SilentlyContinue
            if ($own.User) { $owner = "$($own.Domain)\$($own.User)" }
        } catch {}
        [PSCustomObject]@{
            Pid       = $_.Id
            SessionId = $_.SessionId
            Owner     = $owner
            Path      = $_.Path
        }
    }
}

Write-Step "1) Current MT5 processes"
Get-Mt5Snapshot | Format-Table -AutoSize

Write-Step "2) Attempt kill Session 0 zombie via SYSTEM task (may fail if not elevated)"
schtasks /Delete /TN $killTask /F 2>$null | Out-Null
schtasks /Create /TN $killTask /TR "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$killScript`"" /SC ONCE /ST 00:00 /SD 01/01/2020 /RU SYSTEM /F 2>$null | Out-Null
schtasks /Run /TN $killTask 2>$null | Out-Null
Start-Sleep -Seconds 5
schtasks /Delete /TN $killTask /F 2>$null | Out-Null
$s0 = @(Get-Mt5Snapshot | Where-Object { $_.SessionId -eq 0 })
if ($s0.Count -gt 0) {
    $ids = ($s0 | ForEach-Object { "PID=$($_.Pid)" }) -join ", "
    Write-Host "WARNING: Session 0 MT5 still running: $ids (ignored - using isolated MT5Agent)"
} else {
    Write-Host "OK: No Session 0 MT5 processes."
}

Write-Step "3) Bootstrap portable MT5Agent (separate data dir avoids Session 0 lock)"
if (-not (Test-Path $agentRoot)) { New-Item -ItemType Directory -Path $agentRoot -Force | Out-Null }
if (-not (Test-Path (Join-Path $agentRoot "config"))) { New-Item -ItemType Directory -Path (Join-Path $agentRoot "config") -Force | Out-Null }
robocopy $srcTerminal $agentRoot terminal64.exe /NFL /NDL /NJH /NJS /nc /ns /np | Out-Null
if (Test-Path $srcCommon) { Copy-Item $srcCommon $agentCommon -Force }
if (Test-Path $enableApi) { & $enableApi -ConfigPath $agentCommon }
Set-Content -Path $launchBat -Value "@echo off`r`nstart `"`" `"$agentTerminal`" /portable" -Encoding ASCII
Write-Host "Portable terminal: $agentTerminal"

Write-Step "4) Stop interactive MT5, launch MT5Agent in Session 3"
Get-Process -Name "terminal64" -ErrorAction SilentlyContinue | Where-Object { $_.SessionId -ne 0 } | Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 2
schtasks /Delete /TN $launchTask /F 2>$null | Out-Null
schtasks /Create /TN $launchTask /TR "`"$launchBat`"" /SC ONCE /ST 00:00 /SD 01/01/2020 /RU $env:USERNAME /IT /F | Out-Null
schtasks /Run /TN $launchTask | Out-Null
Write-Host "Waiting 120s for first-time compile + broker login..."
Start-Sleep -Seconds 120

Write-Step "5) MT5 processes after relaunch"
$mt5 = @(Get-Mt5Snapshot | Where-Object { $_.SessionId -ne 0 })
$mt5 | Format-Table -AutoSize
$agent = @($mt5 | Where-Object { $_.Path -like "*MT5Agent*" })
if ($agent.Count -ge 1) {
    Write-Host "OK: MT5Agent in Session $($agent[0].SessionId) PID=$($agent[0].Pid)"
} else {
    Write-Host "WARNING: MT5Agent not detected in interactive session"
}

Write-Step "6) Run mt5_connect_test_session.py via schtasks /IT"
if (Test-Path (Join-Path $project "logs\mt5_connect_test_result.json")) {
    Remove-Item (Join-Path $project "logs\mt5_connect_test_result.json") -Force -ErrorAction SilentlyContinue
}
schtasks /Delete /TN $testTask /F 2>$null | Out-Null
schtasks /Create /TN $testTask /TR "`"$testBat`"" /SC ONCE /ST 00:00 /SD 01/01/2020 /RU $env:USERNAME /IT /F | Out-Null
schtasks /Run /TN $testTask | Out-Null
Write-Host "Waiting 90s for connection test..."
Start-Sleep -Seconds 90
schtasks /Delete /TN $testTask /F 2>$null | Out-Null

$resultFile = Join-Path $project "logs\mt5_connect_test_result.json"
if (Test-Path $resultFile) {
    Get-Content $resultFile
} else {
    Write-Host "FAILED: $resultFile not created"
    $taskLog = Join-Path $project "logs\mt5_connect_test_task.log"
    if (Test-Path $taskLog) { Get-Content $taskLog }
}

Write-Step "Done"