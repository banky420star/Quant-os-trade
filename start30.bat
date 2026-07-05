@echo off
title MT5 Quant OS — $30 Profile
cd /d "%~dp0"
call scripts\kill_agent.bat
set MT5_QUANT_PROFILE=30
echo.
echo  Starting MT5 Quant OS — $30 MICRO profile (Aggressive Edge Sessions)
echo  Symbols: XAUUSDm, USOILm, UK100m
echo  Max loss: $10/trade  |  Lot: 0.01  |  Aggressive mode + positive-evolution
echo.
python start.py --profile 30
if errorlevel 1 pause