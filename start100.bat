@echo off
title MT5 Quant OS - $100 Profile
cd /d "%~dp0"
call scripts\select_mt5_python.bat
if errorlevel 1 (
    pause
    exit /b 1
)
call scripts\kill_agent.bat
echo.
echo  Starting MT5 Quant OS - $100 SMALL profile (REAL account)
echo  Profile: 100  ^|  Symbols: XAUUSDm, USOILm, UK100m, US30m
echo  Max loss: $20/trade  ^|  Lot: up to 0.02  ^|  No pyramiding
echo  Dashboard: http://127.0.0.1:8081  (open manually in browser)
echo.
"%MT5_PYTHON_EXE%" scripts\launch_all.py --profile 100 %*
if errorlevel 1 pause
