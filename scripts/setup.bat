@echo off
rem One-time setup: create the virtual environment and install everything.
cd /d "%~dp0.."
echo === Creating .venv (if missing) ===
if not exist ".venv\Scripts\python.exe" (
  py -m venv .venv || goto :fail
)
set "PY=%CD%\.venv\Scripts\python.exe"
echo === Installing requirements ===
"%PY%" -m pip install --upgrade pip
"%PY%" -m pip install -r requirements.txt || goto :fail
echo.
echo Setup complete. Next: scripts\app-up.bat, then scripts\monitor.bat
goto :eof
:fail
echo.
echo SETUP FAILED - copy the messages above.
pause
exit /b 1
