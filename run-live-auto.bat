@echo off
title MT5 Quant OS — LIVE console (auto profile)
cd /d "%~dp0"
call scripts\select_mt5_python.bat
if errorlevel 1 (
    pause
    exit /b 1
)
call scripts\kill_agent.bat
echo.
echo  LIVE mode — logs in this window, profile auto-detected from MT5 login
echo  Ctrl+C to stop
echo.
"%MT5_PYTHON_EXE%" start.py %*
pause