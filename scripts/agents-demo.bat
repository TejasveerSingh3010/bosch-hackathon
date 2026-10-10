@echo off
rem The three-agent demo, in order. Needs the app and monitor already running
rem (scripts\app-up.bat, scripts\monitor.bat) and a baseline (scripts\baseline.bat).
rem Phase 1: no LLM key required - every agent runs deterministically offline.
setlocal
cd /d "%~dp0.."
set "PY=%CD%\.venv\Scripts\python.exe"

echo ============================================================
echo  0. Dependency map (Agent 1 ranks by criticality from this)
echo ============================================================
"%PY%" -m mapper --no-png || goto :fail
echo.
pause

echo ============================================================
echo  1. Agent 1: rank every attack, most to least harmful
echo ============================================================
"%PY%" -m planner rank || goto :fail
echo.
echo The table is sorted by predicted harm. A human approves ONE entry below.
pause

echo ============================================================
echo  2. Agent 1: approve the rank-1 attack (requires confirmation)
echo ============================================================
echo This prints the plan and asks you to type the experiment id to confirm.
"%PY%" -m planner approve 1
echo (exit code %ERRORLEVEL%: 0 ran clean, 3 was aborted/stopped by the safety layer)
echo.
echo Agent 2 (report) and Agent 3 (recommendations) ran automatically after it.
pause

echo ============================================================
echo  3. Agent 2: the failure report for that experiment
echo ============================================================
echo See reports\ for the per-experiment report and reports\campaign.md.
dir /b reports\*.md
echo.
pause

echo ============================================================
echo  4. Agent 3: the ranked, deduplicated fix recommendations
echo ============================================================
echo Recommendations only - nothing was applied.
type reports\recommendations.md
echo.
echo ============================================================
echo  Done. Re-run `python -m planner rank` to see the ranking change:
echo  the attack you just ran will have moved based on what happened.
echo ============================================================
pause
goto :eof
:fail
echo.
echo Something is not ready. Make sure the app and monitor are running
echo (scripts\app-up.bat, scripts\monitor.bat) and a baseline exists.
pause
exit /b 1
