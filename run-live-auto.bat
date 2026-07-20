@echo off
title MT5 Quant OS — LIVE console (auto profile)
cd /d "%~dp0"
call scripts\kill_agent.bat
echo.
echo  LIVE mode — logs in this window, profile auto-detected from MT5 login
echo  Ctrl+C to stop
echo.
python start.py
pause