@echo off
title CloudRender Python Server 8080 (admin)
rem CloudRender server - admin launcher (for Start-Process -Verb RunAs)
rem Server logs print to this window, same style as the 8081 restart script.
rem Stop old instances first (match by command line; do not kill the static
rem file server); worker exits when its parent dies as fallback
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0_kill_server.ps1"
cd /d c:\zhcf\pythonproject\cloudrender\server\python
set PYTHONIOENCODING=utf-8
c:\zhcf\pythonproject\cloudrender\.venv\Scripts\python.exe -u -X utf8 -m cloudrender.server --port 8080 --fps 60 --bitrate 10000
echo.
echo [server] exited (press any key to close this window).
pause