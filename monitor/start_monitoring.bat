@echo off
setlocal
rem Starts the Boutique monitor + dashboard. Double-click this file in File Explorer.
rem Re-running it first closes the windows from a previous run.
cd /d "%~dp0.."

taskkill /FI "WINDOWTITLE eq Boutique*" /T /F >nul 2>&1

if not exist ".venv\Scripts\python.exe" (
  echo Creating virtual environment...
  py -m venv .venv || goto :fail
)
set "PY=%CD%\.venv\Scripts\python.exe"

echo Installing monitor requirements...
"%PY%" -m pip install -r monitor\requirements.txt || goto :fail

set "BOUTIQUE_URL=http://localhost:8080"
start "Boutique headless monitor" cmd /k ""%PY%" monitor\headless_monitor.py"

set "BOUTIQUE_DASHBOARD_WRITES=0"
start "Boutique dashboard" cmd /k ""%PY%" -m streamlit run monitor\dashboard.py"
exit /b 0

:fail
echo.
echo Setup failed - copy the messages above and send them to Claude.
pause
exit /b 1
