@echo off
rem Stop Online Boutique and remove its containers.
cd /d "%~dp0..\app"
docker compose down
