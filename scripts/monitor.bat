@echo off
rem Start the headless monitor (writes telemetry) and the dashboard (shows it).
rem Re-running this closes the windows from a previous run first.
cd /d "%~dp0.."
if not exist ".venv\Scripts\python.exe" (
  echo Run scripts\setup.bat first.
  pause
  exit /b 1
)
set "PY=%CD%\.venv\Scripts\python.exe"
taskkill /FI "WINDOWTITLE eq Boutique*" /T /F >nul 2>&1

set "BOUTIQUE_URL=http://localhost:8080"
start "Boutique monitor" cmd /k ""%PY%" monitor\headless_monitor.py"

set "BOUTIQUE_DASHBOARD_WRITES=0"
start "Boutique dashboard" cmd /k ""%PY%" -m streamlit run monitor\dashboard.py"

echo.
echo Monitor and dashboard starting in their own windows.
echo Dashboard: http://localhost:8501
echo Leave both running, then: scripts\baseline.bat (after ~5 minutes)
