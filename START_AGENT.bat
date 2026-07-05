@echo off
title MT5 Quant OS
cd /d "%~dp0"
call scripts\kill_agent.bat
echo.
echo  Starting MT5 Quant OS — bot only (use LAUNCH.bat for bot + all dashboards)
echo  Full stack: LAUNCH.bat
echo  Override: python start.py --profile 30
echo.
python start.py
if errorlevel 1 (
    echo.
    echo  Agent exited with an error.
    pause
)