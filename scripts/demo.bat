@echo off
rem The day-1 demo, in order. Needs the app and monitor already running.
setlocal
cd /d "%~dp0.."
set "PY=%CD%\.venv\Scripts\python.exe"

echo ============================================================
echo  1. Monitoring status
echo ============================================================
"%PY%" -m orchestrator status || goto :fail
echo.
pause

echo ============================================================
echo  2. Dependency map - which service is most critical, and why
echo ============================================================
"%PY%" -m mapper --print
echo.
pause

echo ============================================================
echo  3. Safety layer - a plan the rules must refuse
echo ============================================================
echo Trying to break the load generator (protected infrastructure)...
"%PY%" -m orchestrator run --target loadgenerator --action kill --duration 10
echo (exit code %ERRORLEVEL% - 2 means the safety layer rejected it)
echo.
pause

echo ============================================================
echo  4. Dry run - validate the real experiment, change nothing
echo ============================================================
"%PY%" -m orchestrator run --target adservice --action kill --duration 20 --dry-run
echo.
pause

echo ============================================================
echo  5. Experiment A: crash adservice (expected: degrades gracefully)
echo ============================================================
"%PY%" -m orchestrator run --target adservice --action kill --duration 20 ^
  --id demo-a-adservice-kill --reason "Least critical service: does the page still load without ads?"
echo.
pause

echo ============================================================
echo  6. Experiment B: freeze currencyservice (expected: hangs, watchdog aborts)
echo ============================================================
"%PY%" -m orchestrator run --target currencyservice --action pause --duration 60 ^
  --id demo-b-currency-pause --reason "On the checkout path: does a hung dependency breach the SLA?"
echo.
echo ============================================================
echo  Done. Results are in results\demo-*.json
echo ============================================================
pause
goto :eof
:fail
echo.
echo The monitor is not ready. Run scripts\monitor.bat and wait for samples.
pause
exit /b 1
