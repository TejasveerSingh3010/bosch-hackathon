@echo off
rem Is the monitor healthy and is a baseline present?
cd /d "%~dp0.."
".venv\Scripts\python.exe" -m orchestrator status
echo.
pause
