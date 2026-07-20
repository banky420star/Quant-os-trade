@echo off
title MT5 Quant OS — Full Growth
cd /d "%~dp0"
call scripts\kill_agent.bat
set MT5_QUANT_PROFILE=growth
echo.
echo  Starting MT5 Quant OS — FULL GROWTH (14 symbols)
echo.
python start.py --profile growth
if errorlevel 1 pause