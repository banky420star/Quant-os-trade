@echo off
title MT5 Quant OS — Full Growth
cd /d "%~dp0"
call scripts\select_mt5_python.bat
if errorlevel 1 (
    pause
    exit /b 1
)
call scripts\kill_agent.bat
set MT5_QUANT_PROFILE=growth
echo.
echo  Starting MT5 Quant OS — FULL GROWTH (14 symbols)
echo.
"%MT5_PYTHON_EXE%" start.py --profile growth %*
if errorlevel 1 pause