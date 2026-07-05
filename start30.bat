@echo off
title MT5 Quant OS — $30 Profile
cd /d "%~dp0"
call scripts\kill_agent.bat
echo.
echo  Starting MT5 Quant OS — force $30 MICRO profile (Aggressive Edge Sessions)
echo  Symbols: XAUUSDm, USOILm, UK100m
echo  Max loss: $10/trade  |  Lot: 0.01  |  Aggressive mode + positive-evolution
echo  Tip: run START_AGENT.bat to auto-match the logged-in MT5 account instead.
echo.
python start.py --profile 30
if errorlevel 1 pause