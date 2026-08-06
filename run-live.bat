@echo off
title MT5 Quant OS -- LIVE console (EXPLICIT OPT-IN)
cd /d "%~dp0"
call scripts\select_mt5_python.bat
if errorlevel 1 (
    pause
    exit /b 1
)
call scripts\kill_agent.bat
echo.
echo  *** WARNING: --profile 30-real enables LIVE order routing ***
echo  Dashboard: http://127.0.0.1:8081
echo  Ctrl+C to stop
echo.
"%MT5_PYTHON_EXE%" start.py --profile 30-real %*
pause
