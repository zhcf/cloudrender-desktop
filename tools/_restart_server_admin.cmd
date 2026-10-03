@echo off
rem CloudRender server restart (self-elevating): double-click to run.
rem Elevated instance: stop old 8080 server (listener + its python parent),
rem then relaunch the server with the latest code.
rem Keep this window open while using the SDK (server runs inside it).
fltmc >nul 2>&1
if errorlevel 1 (
    echo Requesting administrator privileges...
    powershell -NoProfile -Command "Start-Process -Verb RunAs -FilePath '%~f0'"
    exit /b
)
title CloudRender Server (admin)
echo [restart] stopping old server instance...
powershell -NoProfile -Command "$l = Get-NetTCPConnection -LocalPort 8080 -State Listen -EA 0 | Select-Object -First 1; if ($l) { $srv = $l.OwningProcess; $p = Get-CimInstance Win32_Process -Filter ('ProcessId=' + $srv) -EA 0; if ($p) { $pp = Get-CimInstance Win32_Process -Filter ('ProcessId=' + $p.ParentProcessId) -EA 0; if ($pp -and $pp.Name -like 'python*') { Stop-Process -Id $pp.ProcessId -Force -EA 0; Write-Host ('[restart] stopped parent pid ' + $pp.ProcessId) } }; Stop-Process -Id $srv -Force -EA 0; Write-Host ('[restart] stopped server pid ' + $srv) } else { Write-Host '[restart] no listener on 8080' }"
ping -n 3 127.0.0.1 >nul
echo [restart] launching server with fixed capture code...
call "%~dp0_start_server_admin.cmd"