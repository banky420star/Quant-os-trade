@echo off
title MT5 Quant OS — Full Launch
cd /d "%~dp0"
echo.
echo  Launching bot + all dashboards (8080, 8081, 8083)...
echo.
python scripts/launch_all.py %*
if errorlevel 1 (
    echo.
    echo  Launch failed.
    pause
    exit /b 1
)
echo  Running in background. Use scripts\kill_agent.bat to stop.
pause