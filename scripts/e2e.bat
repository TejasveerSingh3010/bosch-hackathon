@echo off
rem End-to-end live test: LangGraph pipeline (attacker -> analyst -> remediation -> escalate), traced in Langfuse.
rem Needs: app-up.bat and monitor.bat running, a baseline, LLM_API_KEY and LANGFUSE_* keys in .env.
cd /d "%~dp0.."
set "PY=%CD%\.venv\Scripts\python.exe"
if not exist "%PY%" ( echo Run scripts\setup.bat first. & exit /b 1 )
echo === Pre-flight: telemetry fresh and baseline present? ===
"%PY%" -m orchestrator targets
call scripts\status.bat
echo.
echo === Dry run (validates the plan, injects nothing) ===
"%PY%" -m agents run docs\e2e-plan.json --dry-run --session e2e-dry || exit /b 1
echo.
echo Now run the LIVE attack. Watch http://localhost:8080 and http://localhost:8501 while it runs.
pause
"%PY%" -m agents run docs\e2e-plan.json --session e2e-live
echo.
echo Reports: reports\latest.md   Remediation: python -m remediation list   Traces: your Langfuse project
