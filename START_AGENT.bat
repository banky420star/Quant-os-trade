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
echo  Starting MT5 Quant OS — bot only (use LAUNCH.bat for bot + all dashboards)
echo  Full stack: LAUNCH.bat
echo  Override: START_AGENT.bat --profile 30
echo.
"%MT5_PYTHON_EXE%" start.py %*
if errorlevel 1 (
    echo.
    echo  Agent exited with an error.
    pause
)