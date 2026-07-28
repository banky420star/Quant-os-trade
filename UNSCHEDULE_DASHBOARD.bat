@echo off
REM ============================================================================
REM  UNSCHEDULE_DASHBOARD.bat
REM  Inverse of SCHEDULE_DASHBOARD_AT_LOGON.bat. Deletes the logon task.
REM ============================================================================

setlocal
set "TASKNAME=MT5-Dashboard-Launch"

echo [-] deleting task "%TASKNAME%"...
schtasks /Delete /TN "%TASKNAME%" /F
if errorlevel 1 (
    echo [!] task did not exist or delete failed
) else (
    echo [OK] task removed; dashboard will no longer auto-start at logon
)
pause
endlocal
