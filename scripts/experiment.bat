@echo off
rem Run one experiment.  Usage:
rem   scripts\experiment.bat redis-cart kill 30
rem   scripts\experiment.bat adservice pause 20 --dry-run
cd /d "%~dp0.."
if "%~1"=="" (
  echo Usage: scripts\experiment.bat ^<service^> ^<kill^|stop^|pause^> [seconds] [--dry-run]
  echo.
  ".venv\Scripts\python.exe" -m orchestrator targets
  exit /b 1
)
set "SECONDS=%~3"
if "%SECONDS%"=="" set "SECONDS=30"
".venv\Scripts\python.exe" -m orchestrator run --target %1 --action %2 --duration %SECONDS% %4 %5
echo.
pause
