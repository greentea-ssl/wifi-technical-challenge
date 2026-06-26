@echo off
cd /d "%~dp0"
echo Building ESP32C5 Controller...
arduino-cli compile -b esp32:esp32:XIAO_ESP32C5 ESP32C5Controller.ino --output-dir ./
echo.
echo Build completed!
pause
