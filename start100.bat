@echo off
title MT5 Quant OS — $100 Profile
cd /d "%~dp0"
call scripts\kill_agent.bat
set MT5_QUANT_PROFILE=100
echo.
echo  Starting MT5 Quant OS — $100 SMALL profile
echo  Symbols: XAUUSDm, USOILm, UK100m, US30m
echo  Max loss: $20/trade  |  Lot: up to 0.02  |  No pyramiding
echo.
python start.py --profile 100
if errorlevel 1 pause