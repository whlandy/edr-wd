#!/usr/bin/env -pwsh
# stop_server.ps1 — Stop the EDR-WD MCP server
#
# Stops ONLY the process listening on port 8765 that belongs to this skill's
# server entry point.  Does NOT kill all Python processes.
#
# Usage:
#   .\stop_server.ps1
#   powershell -ExecutionPolicy Bypass -File stop_server.ps1

$ErrorActionPreference = 'Stop'

$Port = 8765

$conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
        Select-Object -First 1

if (-not $conn) {
    Write-Host "[OK] No server listening on port $Port."
    exit 0
}

$pid = $conn.OwningProcess
Write-Host "[INFO] Stopping MCP server (PID $pid) on port $Port..."

try {
    & taskkill /F /T /PID $pid 2>$null
    Start-Sleep -Seconds 1
    $still = Get-Process -Id $pid -ErrorAction SilentlyContinue
    if ($still) {
        Stop-Process -Id $pid -Force -ErrorAction SilentlyContinue
    }
    Write-Host "[OK] Server stopped (PID $pid)." -ForegroundColor Green
    exit 0
} catch {
    Write-Error "[ERROR] Failed to stop PID $pid : $_"
    exit 1
}
