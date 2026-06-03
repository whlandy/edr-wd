#!/usr/bin/env -pwsh
# install_task.ps1 — Install or update StartEDRMCP scheduled task
#
# This script MUST run inside an interactive Windows desktop session
# (RDP or Console) — not over SSH interactively.
#
# Task: StartEDRMCP
#   Runs as the currently logged-on interactive user.
#   Triggered manually by the agent via: schtasks /Run /TN StartEDRMCP
#
# Usage:
#   .\install_task.ps1
#   powershell -ExecutionPolicy Bypass -File install_task.ps1

$ErrorActionPreference = 'Stop'
$TaskName = 'StartEDRMCP'
$Root = Split-Path -Parent $PSScriptRoot

# ── Resolve the interactive user ────────────────────────────────────────────
function Get-InteractiveUser {
    try {
        $lines = (& quser.exe 2>$null) | Select-Object -Skip 1
        foreach ($line in $lines) {
            $normalized = ($line -replace '^\s*>', '').Trim()
            if (-not $normalized) { continue }
            $parts = $normalized -split '\s+'
            if ($parts.Count -lt 4) { continue }
            $userName = $parts[0]
            $sessionName = $parts[1]
            $stateIndex = 3
            if ($parts[2] -match '^\d+$') {
                $stateIndex = 3
            } elseif ($parts.Count -gt 3 -and $parts[3] -match '^\d+$') {
                $sessionName = "$($parts[1]) $($parts[2])"
                $stateIndex = 4
            }
            if ($parts.Count -le $stateIndex) { continue }
            $state = $parts[$stateIndex]
            if ($state -eq 'Active' -and $sessionName.ToLower() -match '^(console|rdp-tcp)') {
                return $userName
            }
        }
    } catch {}
    return $null
}

$user = Get-InteractiveUser
if (-not $user) {
    Write-Error '[install_task] No active Console/RDP session found. Log in to Windows first.'
    exit 1
}

# Determine drive letter of $Root (so the task works regardless of which drive edr-wd lives on)
$drive = Split-Path -Qualifier $Root
$rootEscaped = "${drive}\$($Root.Substring(3))"   # e.g. C:\Users\whl\edr-wd\target

# ── Build paths ─────────────────────────────────────────────────────────────
$startScript = Join-Path $PSScriptRoot 'start_server.ps1'
$startScriptEscaped = $startScript -replace "'", "''"

# ── Unregister existing task (if any) ───────────────────────────────────────
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue

# ── Create the task ─────────────────────────────────────────────────────────
$action = New-ScheduledTaskAction `
    -Execute 'powershell.exe' `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$startScriptEscaped`""

# Run in the logged-on user's interactive session
$principal = New-ScheduledTaskPrincipal `
    -UserId $user `
    -LogonType Interactive `
    -RunLevel Highest

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Days 7) `
    -Hidden

$description = 'Starts EDR-WD MCP server in the interactive desktop. ' +
               'Triggered by agent via: schtasks /Run /TN StartEDRMCP'

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Principal $principal `
    -Settings $settings `
    -Description $description `
    -Force | Out-Null

$registered = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($registered) {
    Write-Host "[OK] Task '$TaskName' registered (User: $user, RunLevel: Highest)" -ForegroundColor Green
    Write-Host "  Action : $startScriptEscaped"
    Write-Host "  Trigger: Manual (agent calls schtasks /Run /TN $TaskName)"
    exit 0
} else {
    Write-Error '[install_task] Failed to register scheduled task.'
    exit 1
}
