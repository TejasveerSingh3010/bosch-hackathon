@echo off
rem Undo anything a crashed experiment left behind.
cd /d "%~dp0.."
".venv\Scripts\python.exe" -m orchestrator recover
echo.
pause
