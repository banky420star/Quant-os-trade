@echo off
title MT5 Quant OS — $30 Micro LIVE
cd /d "%~dp0"
call scripts\kill_agent.bat
echo.
echo  Starting MT5 Quant OS — REAL micro profile (30-real)
echo  Symbols: XAUUSDm, USOILm, UK100m
echo  Max loss: $10/trade  |  Lot: 0.01  |  LIVE MONEY
echo.
python scripts/launch_all.py --profile 30-real
if errorlevel 1 pause