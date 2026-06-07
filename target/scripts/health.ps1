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
$LogDir = Join-Path $TargetRoot 'logs'

$listening = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
if ($listening) {
    Write-Host "[OK] Port $Port listening (PID=$($listening.OwningProcess), LocalAddr=$($listening.LocalAddress))"
    $startLog = Join-Path $LogDir 'start.log'
    if (Test-Path $startLog) {
        $lastLine = Get-Content $startLog -Tail 3
        Write-Host "Recent start.log: $lastLine"
    }
    exit 0
} else {
    Write-Host "[DOWN] Port $Port not listening"
    $serverLogs = Get-ChildItem $LogDir 'server.*.log' -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if ($serverLogs) {
        $lastLines = Get-Content $serverLogs.FullName -Tail 10
        Write-Host "Last server.log lines:"
        $lastLines | ForEach-Object { Write-Host "  $_" }
    }
    exit 1
}
