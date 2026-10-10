@echo off
rem EMERGENCY STOP: tells a running experiment to roll back now.
cd /d "%~dp0.."
".venv\Scripts\python.exe" -m orchestrator stop --reason "stopped with scripts\stop.bat"
