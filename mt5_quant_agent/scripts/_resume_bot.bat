@echo off
REM USER-AUTHORIZED 2026-06-30: resume conservative trading on real account after
REM crash fixes (news-blackout gate + 12% daily-loss pause de-blinded) + rebaseline
REM to $14.32. Detached launcher; redirect stdout/stderr so the /B process has
REM valid handles and survives this shell session. The bot also writes its own
REM per-loop log files under logs/.
cd /d "C:\Users\Administrator\Desktop\new task\mt5_quant_agent"
start "" /B "C:\Python314\python.exe" -X utf8 start.py > "logs\start_resume_20260630.log" 2>&1