@echo off
rem CloudRender C++ server (frozen build v0.1.0)
rem Admin is required for the lock-screen worker: spawning the SYSTEM worker
rem via the winlogon token needs SeDebugPrivilege.
fltmc >nul 2>&1
if errorlevel 1 (
    echo Requesting administrator privileges...
    powershell -NoProfile -Command "Start-Process -Verb RunAs -FilePath '%~f0'"
    exit /b
)
title CloudRender Desktop C++ Server 8081 (admin)
cd /d "%~dp0"
echo Starting CloudRender C++ server on port 8081 (fps=60 bitrate=10000)...
echo Open http://localhost:8081/ in your browser.
echo.
"%~dp0cloudrender-desktop-server-cpp.exe" --port 8081 --fps 60 --bitrate 10000
echo.
echo [server] exited (press any key to close this window).
pause