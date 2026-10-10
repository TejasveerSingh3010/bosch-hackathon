@echo off
rem Build the service dependency map (results\graph.json + docs\dependency_graph.png).
cd /d "%~dp0.."
".venv\Scripts\python.exe" -m mapper
echo.
pause
