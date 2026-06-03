#!/usr/bin/env -pwsh
# health.ps1 — Check if the EDR-WD MCP server is reachable
#
# Checks:
#   1. Port 8765 is listening.
#   2. MCP / initialize succeeds (HTTP 200/400 with Mcp-Session-Id header).
#
# Usage:
#   .\health.ps1
#   powershell -ExecutionPolicy Bypass -File health.ps1

$ErrorActionPreference = 'Continue'

$Port    = 8765
$BaseUrl = "http://127.0.0.1:$Port"

# ── Port check ────────────────────────────────────────────────────────────────
$conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
        Select-Object -First 1

if (-not $conn) {
    Write-Host '[FAIL] Port 8765 not listening.'
    exit 1
}

# ── MCP initialize check ─────────────────────────────────────────────────────
try {
    $headers = @{ Accept = 'application/json, text/event-stream' }
    $resp = Invoke-WebRequest -Uri "$BaseUrl/mcp" -Method Get `
                             -Headers $headers -TimeoutSec 5

    $session = $resp.Headers['Mcp-Session-Id']
    if ($session) {
        Write-Host '[OK] MCP server healthy (session: ' -NoNewline
        Write-Host "$session" -NoNewline
        Write-Host ')'
        exit 0
    } else {
        Write-Host '[WARN] Port 8765 open but no MCP session header received.'
        exit 1
    }
} catch {
    $status = $_.Exception.Response.StatusCode.value__
    $session = $_.Exception.Response.Headers['Mcp-Session-Id']

    if ($status -and $session) {
        Write-Host '[OK] MCP server responding (HTTP ' -NoNewline
        Write-Host "$status" -NoNewline
        Write-Host ', session: ' -NoNewline
        Write-Host "$session" -NoNewline
        Write-Host ')'
        exit 0
    }

    Write-Host "[FAIL] HTTP check failed: $_"
    exit 1
}
