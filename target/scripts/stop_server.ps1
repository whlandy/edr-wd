param(
    [int]$Port = 8765
)

$ErrorActionPreference = 'Continue'

function Get-TargetRoot {
    $scriptsDir = $PSScriptRoot
    $targetRoot = Split-Path $scriptsDir -Parent
    return $targetRoot
}

$TargetRoot = Get-TargetRoot
$listening = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $listening) {
    Write-Host "[SKIP] Port $Port not in use"
    exit 0
}

Write-Host "Stopping PID $($listening.OwningProcess) on port $Port"
Stop-Process -Id $listening.OwningProcess -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 2

$remaining = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
if ($remaining) {
    Write-Host "[WARN] Port still held by PID $($remaining.OwningProcess)"
    exit 1
} else {
    Write-Host "[OK] Port $Port is free"
    exit 0
}
