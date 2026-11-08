@echo off
cd /d "%~dp0"
set PYTHONUTF8=1
"%~dp0.venv\Scripts\tradingagents.exe" %*
if errorlevel 1 pause
