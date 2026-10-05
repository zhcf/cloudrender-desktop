# CloudRender release packaging script (PyInstaller standalone exe + zip assembly)
# Usage: powershell -File tools\release\build_release.ps1 [-Version 0.1.0]
#        [-SkipPy] [-SkipCpp] [-SkipZip]   for stepwise debugging
# Outputs: dist\cloudrender-desktop-python-v<ver>-win64.zip
#       dist\cloudrender-desktop-cpp-v<ver>-win64.zip
param(
    [string]$Version = "0.1.0",
    [switch]$SkipPy,
    [switch]$SkipCpp,
    [switch]$SkipZip
)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$Py = Join-Path $Root ".venv\Scripts\python.exe"
$Rel = Join-Path $Root "tools\release"
$Build = Join-Path $Root "dist\_build"
$Stage = Join-Path $Root "dist\_stage"

function Invoke-PyInstaller([string]$Tag, [string[]]$PyArgs) {
    Write-Host "=== [build] $Tag ==="
    & $Py -m PyInstaller @PyArgs
    if ($LASTEXITCODE -ne 0) { throw "$Tag packaging failed (exit $LASTEXITCODE)" }
}

if (-not $SkipPy) {
    Invoke-PyInstaller "python-exe" @(
        "--noconfirm", "--clean", "--onedir", "--name", "cloudrender-desktop-server-python",
        "--distpath", (Join-Path $Build "py"), "--workpath", (Join-Path $Build "py_work"),
        "--specpath", $Build, "--paths", (Join-Path $Root "server\python"),
        "--collect-all", "bettercam",
        "--add-binary", "$Root\server\cpp\nativecore\build\Release\nativecore.dll;cloudrender",
        "--add-binary", "$Root\server\cpp\nativecore\build\Release\nativecore.dll;.",
        "--add-data", "$Root\client\web;client/web",
        "--add-data", "$Root\client\javascript\src;client\javascript\src",
        (Join-Path $Rel "frozen_entry_python.py")
    )
}

if (-not $SkipCpp) {
    Invoke-PyInstaller "cpp-exe" @(
        "--noconfirm", "--clean", "--onedir", "--name", "cloudrender-desktop-server-cpp",
        "--distpath", (Join-Path $Build "cpp"), "--workpath", (Join-Path $Build "cpp_work"),
        "--specpath", $Build, "--paths", (Join-Path $Root "server\cpp\shell"),
        "--add-binary", "$Root\server\cpp\sdk\build_sw\Release\cloudrender_session.dll;.",
        "--add-binary", "$Root\server\cpp\sdk\build_sw\Release\nativecore.dll;.",
        "--add-binary", "$Root\server\cpp\sdk\build_sw\Release\libwebrtc.dll;.",
        "--add-data", "$Root\client\web;client/web",
        "--add-data", "$Root\client\javascript\src;client\javascript\src",
        (Join-Path $Rel "frozen_entry_cpp.py")
    )
}

if (-not $SkipZip) {
    Write-Host "=== [build] assembling zips ==="
    $pyDir = Join-Path $Stage "cloudrender-desktop-python"
    $cppDir = Join-Path $Stage "cloudrender-desktop-cpp"
    foreach ($d in @($pyDir, $cppDir)) {
        Remove-Item $d -Recurse -Force -ErrorAction SilentlyContinue
        New-Item -ItemType Directory -Path $d -Force | Out-Null
    }
    Copy-Item -Recurse -Force "$Build\py\cloudrender-desktop-server-python\*" $pyDir
    Copy-Item -Recurse -Force "$Build\cpp\cloudrender-desktop-server-cpp\*" $cppDir
    Copy-Item -Force (Join-Path $Rel "start_python.cmd") (Join-Path $pyDir "start_server.cmd")
    Copy-Item -Force (Join-Path $Rel "README_python.txt") (Join-Path $pyDir "README.txt")
    Copy-Item -Force (Join-Path $Rel "start_cpp.cmd") (Join-Path $cppDir "start_server.cmd")
    Copy-Item -Force (Join-Path $Rel "README_cpp.txt") (Join-Path $cppDir "README.txt")
    $zipPy = Join-Path $Root "dist\cloudrender-desktop-python-v$Version-win64.zip"
    $zipCpp = Join-Path $Root "dist\cloudrender-desktop-cpp-v$Version-win64.zip"
    Remove-Item $zipPy, $zipCpp -Force -ErrorAction SilentlyContinue
    tar -a -c -f $zipPy -C $Stage "cloudrender-desktop-python"
    tar -a -c -f $zipCpp -C $Stage "cloudrender-desktop-cpp"
    foreach ($z in @($zipPy, $zipCpp)) {
        $mb = [math]::Round((Get-Item $z).Length / 1MB, 1)
        Write-Host ("[build] {0}  ({1} MB)" -f $z, $mb)
    }
}
Write-Host "=== [build] done ==="