param(
    [ValidateSet('bootstrap', 'install', 'start', 'restart', 'status', 'health', 'stop', 'guide')]
    [string]$Action = 'bootstrap',
    [string]$TargetRoot,
    [string]$BindHost = '0.0.0.0',
    [int]$Port = 8765,
    [string]$PythonPath,
    [string]$TaskName = 'StartEDRMCP'
)

$ErrorActionPreference = 'Stop'

function Get-TargetRoot {
    $scriptsDir = $PSScriptRoot
    return Split-Path $scriptsDir -Parent
}

function Write-Guide {
    Write-Host "EDR-WD target deploy" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "Typical workflow:" -ForegroundColor Gray
    Write-Host "  1. Register the scheduled task:" -ForegroundColor Gray
    Write-Host "     .\deploy.ps1 -Action install"
    Write-Host "  2. Start the MCP server in the interactive desktop session:" -ForegroundColor Gray
    Write-Host "     .\deploy.ps1 -Action start"
    Write-Host "  3. Check health:" -ForegroundColor Gray
    Write-Host "     .\deploy.ps1 -Action status"
    Write-Host "  4. Stop the server:" -ForegroundColor Gray
    Write-Host "     .\deploy.ps1 -Action stop"
    Write-Host ""
    Write-Host "Environment / config hints:" -ForegroundColor Gray
    Write-Host "  - Keep this repo in the target desktop session (RDP / local GUI)." -ForegroundColor Gray
    Write-Host "  - Use EDR_WD_PYTHON if python.exe is not on PATH." -ForegroundColor Gray
    Write-Host "  - Use EDR_WD_MCP_HOST / EDR_WD_MCP_PORT to override the defaults." -ForegroundColor Gray
    Write-Host "  - The server must run in an interactive desktop session for GUI automation." -ForegroundColor Gray
}

function Invoke-Script {
    param(
        [string]$ScriptName,
        [hashtable]$Arguments = @{}
    )
    $scriptPath = Join-Path (Join-Path $TargetRoot 'scripts') $ScriptName
    if (-not (Test-Path $scriptPath)) {
        throw "Missing script: $scriptPath"
    }

    & $scriptPath @Arguments
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }
}

$TargetRoot = if ($TargetRoot) { $TargetRoot } else { Get-TargetRoot }

switch ($Action) {
    'guide' {
        Write-Guide
        exit 0
    }
    'install' {
        Invoke-Script 'install_task.ps1' @{
            TaskName = $TaskName
            TargetRoot = $TargetRoot
        }
        exit 0
    }
    'start' {
        Invoke-Script 'start_server.ps1' @{
            TargetRoot = $TargetRoot
            BindHost = $BindHost
            Port = $Port
            PythonPath = $PythonPath
        }
        exit 0
    }
    'health' {
        Invoke-Script 'health.ps1' @{
            Port = $Port
        }
        exit 0
    }
    'status' {
        Invoke-Script 'health.ps1' @{
            Port = $Port
        }
        exit 0
    }
    'stop' {
        Invoke-Script 'stop_server.ps1' @{
            Port = $Port
        }
        exit 0
    }
    'restart' {
        Invoke-Script 'stop_server.ps1' @{
            Port = $Port
        }
        Invoke-Script 'start_server.ps1' @{
            TargetRoot = $TargetRoot
            BindHost = $BindHost
            Port = $Port
            PythonPath = $PythonPath
        }
        exit 0
    }
    'bootstrap' {
        Invoke-Script 'install_task.ps1' @{
            TaskName = $TaskName
            TargetRoot = $TargetRoot
        }
        Invoke-Script 'start_server.ps1' @{
            TargetRoot = $TargetRoot
            BindHost = $BindHost
            Port = $Port
            PythonPath = $PythonPath
        }
        Invoke-Script 'health.ps1' @{
            Port = $Port
        }
        exit 0
    }
}
