@echo off
rem CloudRender Python server (frozen build v0.1.0)
rem Admin is required for lock-screen capture and input into elevated
rem windows; without it the server still works but degrades.
fltmc >nul 2>&1
if errorlevel 1 (
    echo Requesting administrator privileges...
    powershell -NoProfile -Command "Start-Process -Verb RunAs -FilePath '%~f0'"
    exit /b
)
title CloudRender Desktop Python Server 8080 (admin)
cd /d "%~dp0"
echo Starting CloudRender Python server on port 8080 (fps=60 bitrate=10000)...
echo Open http://localhost:8080/ in your browser.
echo.
"%~dp0cloudrender-desktop-server-python.exe" --port 8080 --fps 60 --bitrate 10000
echo.
echo [server] exited (press any key to close this window).
pause