@echo off
title MT5 Quant OS
cd /d "%~dp0"
call scripts\kill_agent.bat
echo.
echo  Starting MT5 Quant OS (default $30 Culturing Evolution profile)...
echo  Use start30.bat, start100.bat, start_growth.bat, or start30-c2.bat for variants.
echo.
set MT5_QUANT_PROFILE=30
python start.py --profile 30
if errorlevel 1 (
    echo.
    echo  Agent exited with an error.
    pause
)