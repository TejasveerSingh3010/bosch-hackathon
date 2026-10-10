@echo off
rem Computes results\baseline.json from the last 10 minutes of monitor history.
cd /d "%~dp0.."
".venv\Scripts\python.exe" monitor\baseline.py --minutes 10
echo.
pause
