# Build a distributable pmqe executable with PyInstaller.
#
# Usage:
#   .\build.ps1          incremental build
#   .\build.ps1 -Clean   wipe dist/ and build/ first
#
# Output: dist\pmqe\pmqe.exe (plus its support files — zip the whole
# dist\pmqe folder to distribute).

param([switch]$Clean)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if ($Clean) {
    foreach ($dir in "dist", "build") {
        if (Test-Path $dir) { Remove-Item -Recurse -Force $dir }
    }
}

uv run --with pyinstaller pyinstaller pmqe.spec --noconfirm
if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed (exit $LASTEXITCODE)" }

$exe = Join-Path $PSScriptRoot "dist\pmqe\pmqe.exe"
if (-not (Test-Path $exe)) { throw "Build finished but $exe is missing" }

$size = "{0:N1} MB" -f ((Get-ChildItem -Recurse "dist\pmqe" | Measure-Object Length -Sum).Sum / 1MB)
Write-Host "Build complete: $exe (folder size $size)"
