@echo off
title MT5 Quant OS -- $100 Profile (EXPLICIT OPT-IN)
cd /d "%~dp0"
call scripts\select_mt5_python.bat
if errorlevel 1 ( pause; exit /b 1 )
call scripts\kill_agent.bat
echo.
echo  *** WARNING: $100 profile enables LIVE order routing ***
echo  *** NOT PART OF PHASE 0 -- reviewed deployment only. ***
echo  Symbols: XAU+FX -- Max loss: $20/trade
echo.
"%MT5_PYTHON_EXE%" scripts\launch_all.py --profile 100 %*
if errorlevel 1 pause
