# CloudRender release packaging script (PyInstaller standalone exe + zip assembly)
# Builds the C++ edition only.
# Usage: powershell -File tools\release\build_release.ps1 [-Version 0.1.0]
#        [-SkipCpp] [-SkipZip]   for stepwise debugging
# Output: dist\cloudrender-desktop-cpp-v<ver>-win64.zip
# First run: python -m venv .venv
#            .venv\Scripts\python.exe -m pip install -r tools\release\requirements-build.txt
param(
    [string]$Version = "0.1.0",
    [switch]$SkipCpp,
    [switch]$SkipZip
)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$Py = Join-Path $Root ".venv\Scripts\python.exe"
$Rel = Join-Path $Root "tools\release"
$Build = Join-Path $Root "dist\_build"
$Stage = Join-Path $Root "dist\_stage"

if (-not (Test-Path $Py)) {
    throw "Python not found at $Py - create the venv first: python -m venv .venv"
}
if (-not $SkipCpp) {
    & $Py -c "import PyInstaller" 2>$null
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller is missing - run: $Py -m pip install -r tools\release\requirements-build.txt"
    }
}

function Invoke-PyInstaller([string]$Tag, [string[]]$PyArgs) {
    Write-Host "=== [build] $Tag ==="
    & $Py -m PyInstaller @PyArgs
    if ($LASTEXITCODE -ne 0) { throw "$Tag packaging failed (exit $LASTEXITCODE)" }
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
    Write-Host "=== [build] assembling zip ==="
    $cppDir = Join-Path $Stage "cloudrender-desktop-cpp"
    Remove-Item $cppDir -Recurse -Force -ErrorAction SilentlyContinue
    New-Item -ItemType Directory -Path $cppDir -Force | Out-Null
    Copy-Item -Recurse -Force "$Build\cpp\cloudrender-desktop-server-cpp\*" $cppDir
    Copy-Item -Force (Join-Path $Rel "start_cpp.cmd") (Join-Path $cppDir "start_server.cmd")
    Copy-Item -Force (Join-Path $Rel "README_cpp.txt") (Join-Path $cppDir "README.txt")
    # Chinese user guide sits next to the program folder at the top of the zip.
    # The name is built from code points so this script stays ASCII-only:
    # PowerShell 5.1 mis-decodes BOM-less UTF-8 literals as ANSI.
    $guideName = ([char]0x4F7F) + ([char]0x7528) + ([char]0x8BF4) + ([char]0x660E) + ".txt"
    Copy-Item -Force (Join-Path $Rel "GUIDE_cpp_zh.txt") (Join-Path $Stage $guideName)
    $zipCpp = Join-Path $Root "dist\cloudrender-desktop-cpp-v$Version-win64.zip"
    Remove-Item $zipCpp -Force -ErrorAction SilentlyContinue
    tar -a -c -f $zipCpp -C $Stage $guideName "cloudrender-desktop-cpp"
    $mb = [math]::Round((Get-Item $zipCpp).Length / 1MB, 1)
    Write-Host ("[build] {0}  ({1} MB)" -f $zipCpp, $mb)
}
Write-Host "=== [build] done ==="