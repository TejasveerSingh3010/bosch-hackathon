@echo off
rem Pre-pull the remediation build-check images and create the package-cache volumes,
rem so the first code recommendation does not wait on an image pull or dependency download.
cd /d "%~dp0.."
if not exist ".venv\Scripts\python.exe" (
  echo Run scripts\setup.bat first.
  exit /b 1
)
".venv\Scripts\python.exe" scripts\prewarm.py %*
echo.
pause
