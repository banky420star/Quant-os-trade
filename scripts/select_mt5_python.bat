@echo off
set "MT5_PYTHON_EXE="

rem Prefer an explicit override, then the known MT5-capable install, then Python launcher.
if defined MT5_PYTHON_OVERRIDE set "MT5_PYTHON_EXE=%MT5_PYTHON_OVERRIDE%"
if defined MT5_PYTHON_EXE "%MT5_PYTHON_EXE%" -c "import MetaTrader5" >nul 2>&1
if defined MT5_PYTHON_OVERRIDE if errorlevel 1 echo [WARN] MT5_PYTHON_OVERRIDE failed MetaTrader5 import; falling back.
if defined MT5_PYTHON_EXE if not errorlevel 1 goto :selected

set "MT5_PYTHON_EXE="
if exist "C:\Python314\python.exe" set "MT5_PYTHON_EXE=C:\Python314\python.exe"
if defined MT5_PYTHON_EXE "%MT5_PYTHON_EXE%" -c "import MetaTrader5" >nul 2>&1
if defined MT5_PYTHON_EXE if not errorlevel 1 goto :selected

set "MT5_PYTHON_EXE="
set "PY314_EXE="
for /f "delims=" %%P in ('py -3.14 -c "import sys; sys.stdout.write(sys.executable)" 2^>nul') do set "PY314_EXE=%%P"
if defined PY314_EXE set "MT5_PYTHON_EXE=%PY314_EXE%"
if defined MT5_PYTHON_EXE "%MT5_PYTHON_EXE%" -c "import MetaTrader5" >nul 2>&1
if defined MT5_PYTHON_EXE if not errorlevel 1 goto :selected

set "MT5_PYTHON_EXE=python"
"%MT5_PYTHON_EXE%" -c "import MetaTrader5" >nul 2>&1
if not errorlevel 1 goto :selected

set "MT5_PYTHON_EXE="
echo [ERROR] No usable Python interpreter with MetaTrader5 was found.
echo [ERROR] Install dependencies with the selected interpreter:
echo         C:\Python314\python.exe -m pip install -r requirements.txt
exit /b 1

:selected
echo Using MT5 Python: %MT5_PYTHON_EXE%
exit /b 0
