# CloudRender server restart helper: stop old instances.
# Match by command line (do NOT kill other python processes like the static
# file server):
#   - server : python -m cloudrender.server ... (旧名 shell.server / desktop_app 一并兼容匹配)
#   - worker : python wdesktop_worker.py --parent <serverPID> ...
# Requires admin (old instances run elevated / with winlogon token).
$targets = Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -like '*cloudrender.server*' -or $_.CommandLine -like '*shell.server*' -or $_.CommandLine -like '*desktop_app*' -or $_.CommandLine -like '*wdesktop_worker*' }
foreach ($p in $targets) {
    Write-Host ("stop pid={0}" -f $p.ProcessId)
    Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
}
Start-Sleep -Seconds 8   # wait for worker to notice parent exit (5s self-check)
$left = Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -like '*cloudrender.server*' -or $_.CommandLine -like '*shell.server*' -or $_.CommandLine -like '*desktop_app*' -or $_.CommandLine -like '*wdesktop_worker*' }
if ($left) {
    Write-Host ("leftover: " + (($left | ForEach-Object { $_.ProcessId }) -join ","))
} else {
    Write-Host "old instances stopped"
}