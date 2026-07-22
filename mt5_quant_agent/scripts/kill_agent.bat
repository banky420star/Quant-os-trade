@echo off
title Kill MT5 Quant Agent
cd /d "%~dp0\.."
echo Stopping MT5 Quant OS (bot + dashboards)...
for /f "tokens=2" %%p in ('wmic process where "name='python.exe' and CommandLine like '%%start.py%%'" get ProcessId 2^>nul ^| findstr /r "[0-9]"') do (
  taskkill /F /PID %%p >nul 2>&1
)
for /f "tokens=2" %%p in ('wmic process where "name='python.exe' and CommandLine like '%%nojs_dashboard.py%%'" get ProcessId 2^>nul ^| findstr /r "[0-9]"') do (
  taskkill /F /PID %%p >nul 2>&1
)
for /f "tokens=2" %%p in ('wmic process where "name='python.exe' and CommandLine like '%%terminal_view_server.py%%'" get ProcessId 2^>nul ^| findstr /r "[0-9]"') do (
  taskkill /F /PID %%p >nul 2>&1
)
for /f "tokens=2" %%p in ('wmic process where "name='python.exe' and CommandLine like '%%mt5_quant_agent%%'" get ProcessId 2^>nul ^| findstr /r "[0-9]"') do (
  taskkill /F /PID %%p >nul 2>&1
)
timeout /t 2 /nobreak >nul
echo Done.