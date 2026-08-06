@echo off
title MT5 Quant OS -- Full Growth (demo trading)
cd /d "%~dp0"
call scripts\select_mt5_python.bat
if errorlevel 1 ( pause; exit /b 1 )
call scripts\kill_agent.bat
echo.
echo  Full Growth -- 14 symbols, demo trading, fraction-Kelly
echo  *** This profile enables DEMO order routing ***
echo.
"%MT5_PYTHON_EXE%" start.py --profile growth %*
if errorlevel 1 pause
