@echo off
title MT5 Quant OS
cd /d "%~dp0"
call scripts\select_mt5_python.bat
if errorlevel 1 (
    pause
    exit /b 1
)
call scripts\kill_agent.bat
echo.
echo  Starting MT5 Quant OS -- validation-only by default (no orders sent)
echo  To trade demo: START_AGENT.bat --profile growth
echo  To trade live: START_AGENT.bat --profile 30-real
echo.
"%MT5_PYTHON_EXE%" start.py --profile validation %*
if errorlevel 1 (
    echo.
    echo  Agent exited with an error.
    pause
)
