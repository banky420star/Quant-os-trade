@echo off
REM Run Agent 1 data loop in the current Windows session (use run_data_loop_interactive.ps1 on Server).
setlocal
cd /d "C:\Users\Administrator\Desktop\new task\mt5_quant_agent"
if not exist logs mkdir logs

echo [%date% %time%] data_loop.bat start >> logs\data_loop_task.log
python -u -c "from core.mt5_client import log_session_alignment; from core.utils import setup_logger; log_session_alignment(setup_logger('data_loop_task','data_loop_task.log'))" >> logs\data_loop_task.log 2>&1

python -u loops\data_loop.py >> logs\data_loop_task.log 2>&1
set EXITCODE=%ERRORLEVEL%
echo [%date% %time%] data_loop.bat exit code=%EXITCODE% >> logs\data_loop_task.log
exit /b %EXITCODE%