@echo off
rem Run one experiment.  Usage:
rem   scripts\experiment.bat redis-cart kill 30
rem   scripts\experiment.bat adservice pause 20 --dry-run
rem   scripts\experiment.bat frontend spike 30 --rps 20 --users 5
rem   scripts\experiment.bat currencyservice throttle 30 --cpus 0.05
cd /d "%~dp0.."
if "%~1"=="" (
  echo Usage: scripts\experiment.bat ^<service^> ^<kill^|stop^|pause^|spike^|throttle^> [seconds] [extra flags]
  echo   spike example: scripts\experiment.bat frontend spike 30 --rps 20 --users 5
  echo.
  ".venv\Scripts\python.exe" -m orchestrator targets
  exit /b 1
)
set "TARGET=%~1"
set "ACTION=%~2"
set "SECONDS=%~3"
if "%SECONDS%"=="" set "SECONDS=30"
shift
shift
shift
".venv\Scripts\python.exe" -m orchestrator run --target %TARGET% --action %ACTION% --duration %SECONDS% %1 %2 %3 %4 %5 %6 %7 %8 %9
echo.
pause
