@echo off
title MT5 Quant OS - Full Launch (claude + fixes)
cd /d "%~dp0"

echo.
echo  ===============================================================
echo   MT5 QUANT OS - FULL STACK LAUNCHER
echo   claude branch + recent fixes
echo   profile default: growth (14 symbols)
echo  ===============================================================
echo.
echo   This will start:
echo     - Bot (start.py --profile growth) on :8080
echo     - Dashboard mirror (static) on :8088 (Freebuff Preview tab)
echo     - Health monitor on :8090
echo     - TUI status board in a separate window
echo.
echo   Use:
echo     --profile 30      to run the $30 demo profile
echo     --profile 100     to run the $100 real profile
echo     --profile 30-real to run $30 real account
echo     --no-tui          to skip the TUI window
echo     --browser         to open browser to the mirror dashboard
echo     --wait            to keep this window alive tailing status
echo     --status          just check current state, no spawn
echo.
echo   To stop: scripts\kill_agent.bat   (or Ctrl+C in this window if --wait)
echo.

REM Ensure Windows console renders UTF-8 properly for the banner + Python output
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1

python scripts\launch_all.py %*

if errorlevel 1 (
    echo.
    echo  Launch failed - check logs\bot.log, logs\dashmirror.log, logs\health.log
    pause
    exit /b 1
)

REM If --wait was NOT passed, the launcher exits after spawning.
echo.
echo  Launch complete. Services running in background.
echo  Use scripts\kill_agent.bat to stop, or python scripts\launch_all.py --status to recheck.
echo.
