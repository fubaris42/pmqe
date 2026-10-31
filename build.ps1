# Build the Windows app with PyInstaller, then verify it with its self-test.
#
# Usage:
#   .\build.ps1             incremental build + self-test
#   .\build.ps1 -Clean      wipe dist\ and build\ first
#   .\build.ps1 -SkipTest   build only
#
# Output: dist\pmqe\pmqe.exe and its support files (zip the dist\pmqe folder
# to distribute).

param([switch]$Clean, [switch]$SkipTest)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if ($Clean) {
    foreach ($dir in "dist", "build") {
        if (Test-Path $dir) { Remove-Item -Recurse -Force $dir }
    }
}

uv run --group build pyinstaller pmqe.spec --noconfirm
if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed (exit $LASTEXITCODE)" }

$exe = Join-Path $PSScriptRoot "dist\pmqe\pmqe.exe"
if (-not (Test-Path $exe)) { throw "Build finished but $exe is missing" }

$size = "{0:N1} MB" -f ((Get-ChildItem -Recurse "dist\pmqe" | Measure-Object Length -Sum).Sum / 1MB)
Write-Host "Built $exe (folder size $size)"

if (-not $SkipTest) {
    # A windowed exe has no console, so the self-test reports through a file.
    $report = Join-Path $PSScriptRoot "build\selftest.json"
    $proc = Start-Process -FilePath $exe -ArgumentList "--self-test", "`"$report`"" -Wait -PassThru
    if ($proc.ExitCode -ne 0) {
        if (Test-Path $report) { Get-Content $report }
        throw "Self-test failed (exit $($proc.ExitCode))"
    }
    Write-Host "Self-test passed"
}
