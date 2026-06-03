#!/usr/bin/env -pwsh
# start_server.ps1 — Start EDR-WD MCP server in the interactive desktop
#
# This script runs inside the user's interactive session when triggered by
# the StartEDRMCP scheduled task.  DO NOT run this script directly over SSH.
#
# Behaviour:
#   1. Change to the target/ directory.
#   2. Create logs/ and screenshots/ if absent.
#   3. Check if port 8765 is already listening; exit if so.
#   4. Start python -m server --http --port 8765.
#   5. Redirect stdout/stderr to logs/edr-wd.{timestamp}.log.

$ErrorActionPreference = 'Stop'

$Port       = 8765
$LogDir     = Join-Path $PSScriptRoot 'logs'
$Screenshots = Join-Path $PSScriptRoot 'screenshots'
$LogFile    = Join-Path $LogDir ('edr-wd.' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '.log')

# ── Bootstrap ────────────────────────────────────────────────────────────────
New-Item -ItemType Directory -Force -Path $LogDir    | Out-Null
New-Item -ItemType Directory -Force -Path $Screenshots | Out-Null

# ── Port check ───────────────────────────────────────────────────────────────
$listening = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
             Select-Object -First 1

if ($listening) {
    $pid = $listening.OwningProcess
    Write-Host "[SKIP] Port $Port already listening (PID $pid). Server already running."
    exit 0
}

# ── Start ────────────────────────────────────────────────────────────────────
Write-Host "[INFO] Starting EDR-WD MCP server on port $Port..."
Write-Host "[INFO] Log  : $LogFile"

$env:EDR_WD_ENABLE_POWERSHELL = '1'

$proc = Start-Process `
    -FilePath 'python' `
    -ArgumentList '-m server --http --port 8765' `
    -WorkingDirectory $PSScriptRoot `
    -PassThru `
    -RedirectStandardOutput $LogFile `
    -RedirectStandardError ($LogFile + '.stderr')

Start-Sleep -Seconds 3

if ($proc.HasExited) {
    Write-Host '[ERROR] Server process exited immediately.' -ForegroundColor Red
    if (Test-Path $LogFile) {
        Get-Content $LogFile -Tail 20 | ForEach-Object { Write-Host "  $_" -ForegroundColor DarkYellow }
    }
    exit 1
}

Write-Host "[OK] Server started (PID $($proc.Id))"
Write-Host "[INFO] Stdout/stderr → $LogFile"
exit 0
