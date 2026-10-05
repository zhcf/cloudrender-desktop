@echo off
rem CloudRender C++ server (8081) restart - admin (self-elevating).
rem Admin is required for the lock-screen worker: spawning the SYSTEM worker
rem via the winlogon token needs SeDebugPrivilege (see winlogon.py).
rem Without it the server degrades: lock screen shows a still frame only.
rem Keep this window open while using the SDK (server runs inside it); an
rem async watcher prints a "DONE - server is UP" banner once port 8081 listens,
rem and server logs are redirected to tools\_cpp_server_console.log.
fltmc >nul 2>&1
if errorlevel 1 (
    echo Requesting administrator privileges...
    powershell -NoProfile -Command "Start-Process -Verb RunAs -FilePath '%~f0'"
    exit /b
)
title CloudRender C++ Server 8081 (admin)
echo [restart] stopping old 8081 server instance...
powershell -NoProfile -Command "$l = Get-NetTCPConnection -LocalPort 8081 -State Listen -EA 0 | Select-Object -First 1; if ($l) { $srv = $l.OwningProcess; $p = Get-CimInstance Win32_Process -Filter ('ProcessId=' + $srv) -EA 0; if ($p) { $pp = Get-CimInstance Win32_Process -Filter ('ProcessId=' + $p.ParentProcessId) -EA 0; if ($pp -and $pp.Name -like 'python*') { Stop-Process -Id $pp.ProcessId -Force -EA 0; Write-Host ('[restart] stopped parent pid ' + $pp.ProcessId) } }; Stop-Process -Id $srv -Force -EA 0; Write-Host ('[restart] stopped server pid ' + $srv) } else { Write-Host '[restart] no listener on 8081' }"
ping -n 3 127.0.0.1 >nul
if not exist "%~dp0..\server\cpp\sdk\build_sw\Release\cloudrender_session.dll" goto :no_stage
    echo [restart] applying staged DLLs: build_sw to build
    copy /y "%~dp0..\server\cpp\sdk\build_sw\Release\cloudrender_session.dll" "%~dp0..\server\cpp\sdk\build\Release\cloudrender_session.dll"
    copy /y "%~dp0..\server\cpp\sdk\build_sw\Release\nativecore.dll" "%~dp0..\server\cpp\sdk\build\Release\nativecore.dll"
    echo [restart] staged DLLs applied
:no_stage
cd /d "%~dp0..\server\cpp\shell"
set PYTHONIOENCODING=utf-8
echo [restart] launching C++ server (8081)...
rem Async readiness watcher: prints a UP banner into this console once port 8081
rem is listening (runs in parallel with the foreground server process below).
start "" /b powershell -NoProfile -Command "$b='[restart] '; for($i=0;$i -lt 120;$i++){ if(Get-NetTCPConnection -LocalPort 8081 -State Listen -EA 0){ Write-Host ''; Write-Host ($b+'============================================================'); Write-Host ($b+'DONE - server is UP: http://localhost:8081/'); Write-Host ($b+'Logs: tools\_cpp_server_console.log'); Write-Host ($b+'Keep this window open while using it; closing it stops the server.'); Write-Host ($b+'============================================================'); exit 0 }; Start-Sleep -Milliseconds 500 }; Write-Host ''; Write-Host ($b+'WARNING: port 8081 did not come up within 60s - check _cpp_server_console.log'); exit 1"
"%~dp0..\.venv\Scripts\python.exe" -u -X utf8 cpp_server.py --port 8081 --fps 60 --bitrate 10000 > "%~dp0_cpp_server_console.log" 2>&1
echo.
echo [restart] server exited (press any key to close this window).
pause