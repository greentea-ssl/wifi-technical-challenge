@echo off
cd /d "%~dp0"
echo Building Wio Display...
docker compose build
docker compose run build
echo.
echo Build completed!
pause
