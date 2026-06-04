$ErrorActionPreference = 'Stop'

$Port = 8765

function Get-TargetRoot {
    $scriptsDir = $PSScriptRoot
    $targetRoot = Split-Path $scriptsDir -Parent
    return $targetRoot
}

$TargetRoot = Get-TargetRoot

# Stop
$listening = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
if ($listening) {
    Write-Host "Stopping PID $($listening.OwningProcess)..."
    Stop-Process -Id $listening.OwningProcess -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 2
}

# Start via start_server.ps1
& (Join-Path $PSScriptRoot 'start_server.ps1')
