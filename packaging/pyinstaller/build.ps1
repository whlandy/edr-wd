# Build the agent-side `mcp.exe` executable (Windows).
#
# Usage: powershell -ExecutionPolicy Bypass -File packaging\pyinstaller\build.ps1 [-Clean]
#
# Requirements:
#   - Python with the project's runtime dependencies installed
#     (`pip install -e .` at the repo root)
#   - pyinstaller (pip install pyinstaller)
#
# Output: build-packaging\dist\mcp\ (onedir layout; entry is mcp.exe).
[CmdletBinding()]
param(
    [switch]$Clean
)

$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Resolve-Path (Join-Path $scriptDir "..\..")
Set-Location $repoRoot

python -c "import PyInstaller" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Error "PyInstaller is not installed. Run: pip install pyinstaller"
}

$extraArgs = @()
if ($Clean) {
    $extraArgs += "--clean"
}

python -m PyInstaller --noconfirm @extraArgs `
    --distpath (Join-Path $repoRoot "build-packaging\dist") `
    --workpath (Join-Path $repoRoot "build-packaging\work") `
    (Join-Path $scriptDir "mcp.spec")

if ($LASTEXITCODE -ne 0) {
    Write-Error "PyInstaller build failed (exit $LASTEXITCODE)"
}

Write-Host ""
Write-Host "Build finished: $(Join-Path $repoRoot 'build-packaging\dist\mcp\mcp.exe')"
Write-Host "Run the smoke test: powershell -ExecutionPolicy Bypass -File $scriptDir\smoke.ps1"
