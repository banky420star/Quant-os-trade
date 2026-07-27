@echo off
setlocal
cd /d "%~dp0\.."
if not exist logs mkdir logs
set LOG=logs\learning_loop_scheduled.log
echo [%DATE% %TIME%] Starting learning review loop >> "%LOG%"
python -c "from loops.learning_review_loop import run; result = run(); print(result)" >> "%LOG%" 2>&1
set _EL=%errorlevel%
echo [%DATE% %TIME%] Completed (exit=%_EL%) >> "%LOG%"
exit /b %_EL%
