@echo off
title MT5 Quant OS -- $30 C3 (paper mode)
cd /d "%~dp0"
call scripts\select_mt5_python.bat
if errorlevel 1 ( pause; exit /b 1 )
call scripts\kill_agent.bat
echo.
echo  $30 C3 Session Edge Specialist -- paper-mode research
echo.
"%MT5_PYTHON_EXE%" start.py --profile 30-c3 %*
if errorlevel 1 pause
