@echo off
title MT5 Quant OS — Full Launch
cd /d "%~dp0"
echo.
echo  Launching bot + all dashboards (8080, 8081, 8083)...
echo.
call scripts\select_mt5_python.bat
if errorlevel 1 (
    pause
    exit /b 1
)
"%MT5_PYTHON_EXE%" scripts\launch_all.py %*
if errorlevel 1 (
    echo.
    echo  Launch failed.
    pause
    exit /b 1
)
echo  Running in background. Use scripts\kill_agent.bat to stop.
pause