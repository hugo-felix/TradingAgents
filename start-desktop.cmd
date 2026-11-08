@echo off
cd /d "%~dp0"
set PYTHONUTF8=1
"%~dp0.venv\Scripts\python.exe" -m tradingagents.desktop --data-dir "%~dp0data\desktop" --env-file "%~dp0.env" %*
if errorlevel 1 pause
