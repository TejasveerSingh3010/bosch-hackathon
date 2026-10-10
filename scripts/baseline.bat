@echo off
rem Capture the steady-state baseline from the monitor's history.
cd /d "%~dp0.."
set "MINUTES=%~1"
if "%MINUTES%"=="" set "MINUTES=10"
".venv\Scripts\python.exe" monitor\baseline.py --minutes %MINUTES%
echo.
pause
