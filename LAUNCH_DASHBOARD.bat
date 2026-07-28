@echo off
REM ============================================================================
REM  LAUNCH_DASHBOARD.bat
REM  One-click launcher for the MT5 Quant OS dashboard (port 8088).
REM
REM  - If a previous run wrote a PID to .freebuff\dashboard.pid, kill it first.
REM  - Start the dashboard detached (survives this console closing).
REM  - Block until /api/health responds, then print URL+PID and open the
REM    dashboard in the default browser.
REM
REM  Doubly useful: the Freebuff thread can now point preview_snapshot at the
REM  printed URL without manual `python start_server.py ...` setup each time.
REM ============================================================================

setlocal ENABLEDELAYEDEXPANSION
set "ROOT=%~dp0"
set "ROOT=%ROOT:~0,-1%"
set "PORT=8088"
set "LOG=%ROOT%\.freebuff\preview-%PORT%.log"
set "PIDFILE=%ROOT%\.freebuff\dashboard.pid"

echo.
echo ============================================================================
echo  MT5 Quant OS - Dashboard launcher
echo  ROOT   : %ROOT%
echo  PORT   : %PORT%
echo  LOG    : %LOG%
echo ============================================================================
echo.

REM --- 1. Kill stale dashboard ----------------------------------------------------
if exist "%PIDFILE%" (
    for /f "usebackq delims=" %%P in ("%PIDFILE%") do (
        echo [-] killing stale dashboard PID %%P
        taskkill /F /PID %%P >nul 2>&1
    )
    del /Q "%PIDFILE%" >nul 2>&1
) else (
    echo [=] no prior PID file; clean start
)

REM --- 2. Start dashboard detached ----------------------------------------------
echo [+] starting dashboard detached on port %PORT%...

REM Truncate the previous log so this run's output starts cleanly.
echo. > "%LOG%" 2>nul

python "%ROOT%\dashboard\start_server.py" ^
    --port %PORT% ^
    --host 127.0.0.1 ^
    --log "%LOG%" ^
    --timeout 20
if errorlevel 1 (
    echo [!] start_server.py exited with errorlevel %errorlevel%
    echo     see %LOG%
    REM Drop the PID file so the next run does not taskkill a stale pid.
    if exist "%PIDFILE%" del /Q "%PIDFILE%" >nul 2>&1
    pause
    exit /b 1
)

REM --- 3. Show new PID + URL ----------------------------------------------------
if not exist "%PIDFILE%" (
    echo [!] no PID file written - dashboard likely failed
    pause
    exit /b 1
)
for /f "usebackq delims=" %%P in ("%PIDFILE%") do set "NEWPID=%%P"

echo.
echo ============================================================================
echo   Dashboard is up.
echo   URL : http://127.0.0.1:%PORT%/
echo   PID : %NEWPID%
echo   LOG : %LOG%
echo.
echo   Next step (Freebuff side):
echo     register_preview url=http://127.0.0.1:%PORT%/ pid=%NEWPID%
echo ============================================================================
echo.

REM --- 4. Open in default browser -----------------------------------------------
start "" "http://127.0.0.1:%PORT%/"

pause
endlocal
