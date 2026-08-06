@echo off
title MT5 Quant OS -- $30 Profile (paper mode)
cd /d "%~dp0"
call scripts\select_mt5_python.bat
if errorlevel 1 ( pause; exit /b 1 )
call scripts\kill_agent.bat
echo.
echo  $30 Micro -- paper-style gates, 0.01 lot, XAU+Oil+UK100
echo  *** This profile does NOT send orders without explicit opt-in ***
echo.
"%MT5_PYTHON_EXE%" start.py --profile 30 %*
if errorlevel 1 pause
