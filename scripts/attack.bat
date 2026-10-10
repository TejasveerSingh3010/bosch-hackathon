@echo off
rem Run chaos attacks by serial number (the # column from `planner rank`).
rem   scripts\attack.bat             list every attack, then type a serial number to run it
rem   scripts\attack.bat 7           run the attack at rank 7 directly, no prompt
rem   scripts\attack.bat 7 --dry-run validate and explain rank 7, change nothing
rem The safety layer still pre-checks the plan and the watchdog can still abort it.
setlocal
cd /d "%~dp0.."
set "PY=%CD%\.venv\Scripts\python.exe"
if not exist "%PY%" (
  echo Run scripts\setup.bat first.
  exit /b 1
)
if "%~1"=="" (
  "%PY%" -m planner menu
) else (
  "%PY%" -m planner approve %* --yes
)
