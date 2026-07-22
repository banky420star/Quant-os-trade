@echo off
title MT5 Quant OS — $30 Profile C3 (Session Edge)
cd /d "%~dp0"
call scripts\kill_agent.bat
set MT5_QUANT_PROFILE=30-c3
echo.
echo  Starting MT5 Quant OS — $30 C3 Session Edge Specialist
echo  Symbols: XAUUSDm, USOILm, UK100m
echo  Strict session x setup edges with culturing evolution
echo.
python start.py --profile 30-c3
if errorlevel 1 pause