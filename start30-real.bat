@echo off
title MT5 Quant OS -- $30 Micro LIVE (EXPLICIT OPT-IN)
cd /d "%~dp0"
call scripts\select_mt5_python.bat
if errorlevel 1 ( pause; exit /b 1 )
call scripts\kill_agent.bat
echo.
echo  *** WARNING: 30-real profile enables LIVE order routing ***
echo  *** NOT PART OF PHASE 0 -- reviewed deployment only. ***
echo  Symbols: XAUUSDm, USOILm, UK100m -- Max loss: $10/trade
echo.
"%MT5_PYTHON_EXE%" scripts\launch_all.py --profile 30-real %*
if errorlevel 1 pause
