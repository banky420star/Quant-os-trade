@echo off
title MT5 Quant OS — Full Launch
cd /d "%~dp0"
echo.
echo  Launching bot + all dashboards (8080, 8081, 8083)...
echo  Profile: growth (live MT5 demo, 14 symbols, data-collecting)
echo.
call scripts\select_mt5_python.bat
if errorlevel 1 (
    pause
    exit /b 1
)
REM 2026-08-04: force --profile growth so the desktop shortcut always starts
REM live MT5 demo trading + data collection. Without this, launch_all.py
REM auto-detects from state/active_profile.json, which a dashboard profile
REM click can leave on data-lab (paper mode -> no live trades, no data). The
REM trailing %* still lets you override, e.g. LAUNCH.bat --profile 30
REM (argparse takes the LAST --profile, so the override wins).
"%MT5_PYTHON_EXE%" scripts\launch_all.py --profile growth %*
if errorlevel 1 (
    echo.
    echo  Launch failed.
    pause
    exit /b 1
)
echo  Running in background. Use scripts\kill_agent.bat to stop.
pause