@echo off
rem Run the test suite (no Docker needed - it uses a fake).
cd /d "%~dp0.."
".venv\Scripts\python.exe" -m pytest
echo.
pause
