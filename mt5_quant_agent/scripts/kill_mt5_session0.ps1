# Kill terminal64.exe processes in Windows Session 0 (run via schtasks /RU SYSTEM).
Get-CimInstance Win32_Process -Filter "Name='terminal64.exe'" |
    Where-Object { $_.SessionId -eq 0 } |
    ForEach-Object {
        Write-Output "Killing PID=$($_.ProcessId) SessionId=$($_.SessionId)"
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    }

Start-Sleep -Seconds 2
Get-CimInstance Win32_Process -Filter "Name='terminal64.exe'" |
    Select-Object ProcessId, SessionId |
    ForEach-Object { Write-Output "Remaining PID=$($_.ProcessId) SessionId=$($_.SessionId)" }