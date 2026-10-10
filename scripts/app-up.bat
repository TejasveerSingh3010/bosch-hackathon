@echo off
rem Start Online Boutique (12 containers) and wait for the frontend to answer.
cd /d "%~dp0..\app"
echo === Starting Online Boutique ===
docker compose up -d || goto :fail
echo.
echo === Waiting for http://localhost:8080 ===
set /a tries=0
:wait
set /a tries+=1
curl -s -o nul -w "%%{http_code}" http://localhost:8080/ > "%TEMP%\bh_code.txt" 2>nul
set /p CODE=<"%TEMP%\bh_code.txt"
if "%CODE%"=="200" goto :ready
if %tries% GEQ 60 goto :timeout
timeout /t 2 /nobreak >nul
goto :wait
:ready
echo Frontend is up. Next: scripts\monitor.bat
docker compose ps
goto :eof
:timeout
echo.
echo The frontend did not answer within ~2 minutes. Check `docker compose logs frontend`.
exit /b 1
:fail
echo Could not start the stack. Is Docker Desktop running?
pause
exit /b 1
