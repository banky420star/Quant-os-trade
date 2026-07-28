@echo off
REM ============================================================================
REM  SCHEDULE_DASHBOARD_AT_LOGON.bat
REM  Register LAUNCH_DASHBOARD.bat with Windows Task Scheduler so the
REM  dashboard auto-starts on every user logon, fully detached.
REM
REM  Run once. From then on, every time you log in, port 8088 is up by the
REM  time your desktop settles. UNSCHEDULE_DASHBOARD.bat reverts it.
REM ============================================================================

setlocal
set "ROOT=%~dp0"
set "ROOT=%ROOT:~0,-1%"
set "TASKNAME=MT5-Dashboard-Launch"
set "LAUNCHER=%ROOT%\LAUNCH_DASHBOARD.bat"

if not exist "%LAUNCHER%" (
    echo [!] LAUNCH_DASHBOARD.bat not found at %LAUNCHER%
    pause
    exit /b 1
)

echo [+] registering task "%TASKNAME%" with Task Scheduler...
schtasks /Create ^
    /TN "%TASKNAME%" ^
    /TR "\"%LAUNCHER%\"" ^
    /SC ONLOGON ^
    /RL LIMITED ^
    /F
if errorlevel 1 (
    echo [!] schtasks /Create failed; see error above
    pause
    exit /b 1
)

echo.
echo [OK] Scheduled. To verify:
echo     schtasks /Query /TN "%TASKNAME%" /V /FO LIST
echo.
echo To disable, run UNSCHEDULE_DASHBOARD.bat
pause
endlocal
