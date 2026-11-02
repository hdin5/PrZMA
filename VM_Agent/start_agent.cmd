@echo off
setlocal
set "PRZMA_AGENT_ROOT=C:\Users\VM Agent\VM_Agent"
cd /d "%PRZMA_AGENT_ROOT%"
if not exist "logs" mkdir "logs"
"%PRZMA_AGENT_ROOT%\.venv\Scripts\python.exe" -u "%PRZMA_AGENT_ROOT%\agent_main.py" >> "%PRZMA_AGENT_ROOT%\logs\agent_main_startup.log" 2>&1
exit /b %ERRORLEVEL%
