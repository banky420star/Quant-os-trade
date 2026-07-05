@echo off
title MT5 Quant OS — $30 Profile C2 (Weight Evolution)
cd /d "%~dp0"
call scripts\kill_agent.bat
set MT5_QUANT_PROFILE=30-c2
echo.
echo  Starting MT5 Quant OS — $30 C2 Adaptive Weight Evolution
echo  Symbols: XAUUSDm, USOILm, UK100m
echo  Replay-validated per-symbol weight evolution
echo.
python start.py --profile 30-c2
if errorlevel 1 pause