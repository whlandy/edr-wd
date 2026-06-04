$ErrorActionPreference = 'Stop'

function Get-TargetRoot {
    $scriptsDir = $PSScriptRoot
    $targetRoot = Split-Path $scriptsDir -Parent
    return $targetRoot
}

$TaskName = 'StartEDRMCP'
$TargetRoot = Get-TargetRoot
$StartScript = Join-Path $PSScriptRoot 'start_server.ps1'
$StartScriptEscaped = $StartScript -replace '\\', '\\\\'

$action = New-ScheduledTaskAction `
    -Execute 'powershell.exe' `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$StartScriptEscaped`"" `
    -WorkingDirectory $TargetRoot

$principal = New-ScheduledTaskPrincipal `
    -UserId $env:USERNAME `
    -LogonType Interactive `
    -RunLevel Highest

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::FromHours(0))

# Unregister existing task if present
$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "Unregistering existing task '$TaskName'..."
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Principal $principal `
    -Settings $settings `
    -Description 'Start EDR-WD MCP server on 0.0.0.0:8765' | Out-Null

Write-Host "[OK] Task '$TaskName' registered."
Write-Host "  TargetRoot : $TargetRoot"
Write-Host "  StartScript: $StartScript"
