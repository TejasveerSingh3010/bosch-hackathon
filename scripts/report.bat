@echo off
rem Agent 2 (Reporter).  Usage:
rem   scripts\report.bat demo              show a report for a synthetic failure (no Docker needed)
rem   scripts\report.bat demo kill         the other built-in scenario
rem   scripts\report.bat watch             watch live telemetry and report automatically on any failure
rem   scripts\report.bat latest            print the newest report
rem   scripts\report.bat <experiment-id>   (re)build the report for a finished experiment
cd /d "%~dp0.."
if not exist ".venv\Scripts\python.exe" (
  echo Run scripts\setup.bat first.
  exit /b 1
)
set "PY=.venv\Scripts\python.exe"
if "%~1"=="" (
  echo Usage: scripts\report.bat ^<demo^|watch^|latest^|experiment-id^>
  exit /b 1
)
if /i "%~1"=="demo" (
  if "%~2"=="" ("%PY%" -m reporter demo) else ("%PY%" -m reporter demo --scenario %~2)
  goto :eof
)
if /i "%~1"=="watch" ("%PY%" -m reporter watch & goto :eof)
if /i "%~1"=="latest" ("%PY%" -m reporter latest & goto :eof)
"%PY%" -m reporter report %~1
