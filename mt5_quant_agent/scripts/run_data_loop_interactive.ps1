# Run data_loop in the interactive user session (fixes MT5 IPC on Windows Server).
$ErrorActionPreference = "Continue"

$project = "C:\Users\Administrator\Desktop\new task\mt5_quant_agent"
$bat = Join-Path $project "scripts\run_data_loop.bat"
$taskName = "MT5DataLoopRun"
$launchScript = Join-Path $project "scripts\launch_mt5_interactive.ps1"

function Get-SessionSnapshot {
    $pythonSession = $null
    try {
        Add-Type @"
using System;
using System.Runtime.InteropServices;
public static class WinSession {
    [DllImport("kernel32.dll")]
    public static extern bool ProcessIdToSessionId(uint pid, out uint sessionId);
}
"@
        $sid = [uint32]0
        [void][WinSession]::ProcessIdToSessionId([uint32]$PID, [ref]$sid)
        $pythonSession = [int]$sid
    } catch {
        $pythonSession = $null
    }

    $mt5 = Get-Process -Name "terminal64" -ErrorAction SilentlyContinue | ForEach-Object {
        [PSCustomObject]@{
            Pid        = $_.Id
            SessionId  = $_.SessionId
            Path       = $_.Path
        }
    }

    return [PSCustomObject]@{
        PythonPid       = $PID
        PythonSessionId = $pythonSession
        Mt5Processes    = @($mt5)
    }
}

Write-Host "=== MT5 Data Loop (interactive session) ==="
$snap = Get-SessionSnapshot
Write-Host "PowerShell PID=$($snap.PythonPid) SessionId=$($snap.PythonSessionId)"

if (-not $snap.Mt5Processes -or $snap.Mt5Processes.Count -eq 0) {
    Write-Host "WARNING: MT5 not running. Launching via launch_mt5_interactive.ps1 ..."
    if (Test-Path $launchScript) {
        & $launchScript
        Start-Sleep -Seconds 5
        $snap = Get-SessionSnapshot
    } else {
        Write-Host "ERROR: launch_mt5_interactive.ps1 not found at $launchScript"
    }
}

foreach ($proc in $snap.Mt5Processes) {
    Write-Host "MT5 PID=$($proc.Pid) SessionId=$($proc.SessionId) Path=$($proc.Path)"
    if ($null -ne $snap.PythonSessionId -and $proc.SessionId -ne $snap.PythonSessionId) {
        Write-Host "WARNING: Python SessionId ($($snap.PythonSessionId)) != MT5 SessionId ($($proc.SessionId))"
        Write-Host "         IPC timeout likely unless data loop runs in SessionId $($proc.SessionId)."
    }
}

schtasks /Delete /TN $taskName /F 2>$null | Out-Null
schtasks /Create /TN $taskName /TR "`"$bat`"" /SC ONCE /ST 00:00 /SD 01/01/2020 /RU $env:USERNAME /IT /F | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: Failed to create scheduled task '$taskName'. Run from an elevated or interactive session."
    exit 1
}
schtasks /Run /TN $taskName | Out-Null

Write-Host "Data loop task started in interactive session for user $env:USERNAME. Waiting 120s..."
Start-Sleep -Seconds 120

$candles = Join-Path $project "state\latest_candles.json"
$taskLog = Join-Path $project "logs\data_loop_task.log"
$dataLog = Join-Path $project "logs\data_loop.log"

if (Test-Path $candles) {
    $json = Get-Content $candles -Raw | ConvertFrom-Json
    Write-Host "OK: source=$($json.source) login=$($json.account.login) timestamp=$($json.timestamp)"
    if ($json.symbol_map) {
        $json.symbol_map | Format-List
    }
    if ($json.source -ne "mt5") {
        Write-Host "WARNING: expected source=mt5 but got $($json.source)"
    }
} else {
    Write-Host "FAILED: state\latest_candles.json not created"
}

Write-Host "--- data_loop_task.log tail ---"
if (Test-Path $taskLog) { Get-Content $taskLog -Tail 40 } else { Write-Host "(missing)" }

Write-Host "--- data_loop.log tail ---"
if (Test-Path $dataLog) { Get-Content $dataLog -Tail 40 } else { Write-Host "(missing)" }

schtasks /Delete /TN $taskName /F 2>$null | Out-Null