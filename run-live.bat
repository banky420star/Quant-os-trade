@echo off
title MT5 Quant OS — LIVE console (30-real)
cd /d "%~dp0"
call scripts\kill_agent.bat
echo.
echo  LIVE mode — all logs print in THIS window
echo  Profile: 30-real (real micro) or auto if you omit --profile
echo  Dashboard: http://127.0.0.1:8081  (run nojs in another window if needed)
echo  Ctrl+C to stop
echo.
python start.py --profile 30-real
pause