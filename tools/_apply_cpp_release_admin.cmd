@echo off
rem CloudRender C++ release switch - admin (self-elevating).
rem Copies the freshly built package (dist\_build\cpp) over the deployed release
rem folder and restarts the server on 8081.
rem Admin is required: the deployed server is elevated (the lock-screen worker
rem spawns via the winlogon token, see winlogon.py), so it can only be stopped
rem from an elevated shell.
rem Build first: powershell -File tools\release\build_release.ps1
setlocal
fltmc >nul 2>&1
if errorlevel 1 (
    echo Requesting administrator privileges...
    powershell -NoProfile -Command "Start-Process -Verb RunAs -FilePath '%~f0'"
    exit /b
)
title CloudRender C++ Server 8081 (admin)
set "ROOT=%~dp0.."
set "SRC=%ROOT%\dist\_build\cpp\cloudrender-desktop-server-cpp"
set "DST="
for /d %%d in ("%ROOT%\dist\cloudrender-desktop-cpp-v*-win64") do set "DST=%%~d\cloudrender-desktop-cpp"
if not exist "%SRC%\cloudrender-desktop-server-cpp.exe" (
    echo [switch] no new build at "%SRC%"
    echo [switch] run: powershell -File tools\release\build_release.ps1
    pause
    exit /b 1
)
if not defined DST (
    echo [switch] no deployed release folder under "%ROOT%\dist"
    pause
    exit /b 1
)
echo [switch] stopping server on 8081...
powershell -NoProfile -Command "$l = Get-NetTCPConnection -LocalPort 8081 -State Listen -EA 0 | Select-Object -First 1; if ($l) { Stop-Process -Id $l.OwningProcess -Force -EA 0; Write-Host ('[switch] stopped pid ' + $l.OwningProcess) } else { Write-Host '[switch] no listener on 8081' }"
ping -n 3 127.0.0.1 >nul
echo [switch] copying new build to "%DST%"
robocopy "%SRC%" "%DST%" /E /NFL /NDL /NJH /NJS /NP >nul
if errorlevel 8 (
    echo [switch] copy failed - is the old server still running?
    pause
    exit /b 1
)
echo [switch] starting server (8081) in a new window...
start "" "%DST%\start_server.cmd"
echo [switch] DONE - wait for the server window banner, then reload http://localhost:8081/
timeout /t 5 /nobreak >nul