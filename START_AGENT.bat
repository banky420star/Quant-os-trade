@echo off
title MT5 Quant OS
cd /d "%~dp0"
call scripts\kill_agent.bat
echo.
echo  Starting MT5 Quant OS — auto-detects profile from logged-in MT5 account
echo  Override: python start.py --profile 30
echo  Variants: start30.bat, start100.bat, start_growth.bat
echo.
python start.py
if errorlevel 1 (
    echo.
    echo  Agent exited with an error.
    pause
)