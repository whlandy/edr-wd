param(
    [string]$TargetRoot,
    [string]$BindHost = '0.0.0.0',
    [int]$Port = 8765,
    [string]$PythonPath
)

$ErrorActionPreference = 'Continue'

function Get-TargetRoot {
    # $PSScriptRoot = target/scripts/
    # Target root = parent of parent
    $scriptsDir = $PSScriptRoot
    $targetRoot = Split-Path $scriptsDir -Parent
    return $targetRoot
}

function Get-PythonPath {
    param(
        [string]$TargetRoot,
        [string]$PythonPath
    )
    if ($PythonPath -and (Test-Path $PythonPath)) {
        return $PythonPath
    }
    $configPath = Join-Path $TargetRoot 'config.json'
    if (Test-Path $configPath) {
        $config = Get-Content $configPath -Raw | ConvertFrom-Json
        $configured = $config.server.python_path
        if ($configured -and (Test-Path $configured)) {
            return $configured
        }
    }
    $envPath = $env:EDR_WD_PYTHON
    if ($envPath -and (Test-Path $envPath)) {
        return $envPath
    }
    $default = 'C:\Program Files\Python313\python.exe'
    if (Test-Path $default) {
        return $default
    }
    $found = Get-Command python -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($found) {
        return $found.Source
    }
    throw "Python not found. Set python_path in config.json or EDR_WD_PYTHON env var."
}

function Write-StartLog {
    param([string]$Message, [string]$TargetRoot)
    $logDir = Join-Path $TargetRoot 'logs'
    New-Item -ItemType Directory -Force -Path $logDir | Out-Null
    $logFile = Join-Path $logDir 'start.log'
    $entry = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') - $Message"
    Add-Content -Path $logFile -Value $entry
}

$TargetRoot = if ($TargetRoot) { $TargetRoot } else { Get-TargetRoot }
$ServerDir = $TargetRoot
$LogDir = Join-Path $TargetRoot 'logs'
$ScreenshotsDir = Join-Path $TargetRoot 'screenshots'
$BindHost = if ($BindHost) { $BindHost } else { '0.0.0.0' }
$Port = if ($Port -gt 0) { $Port } else { 8765 }

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
New-Item -ItemType Directory -Force -Path $ScreenshotsDir | Out-Null

$listening = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
if ($listening) {
    Write-Host "[SKIP] Port $Port already listening (PID $($listening.OwningProcess))"
    Write-StartLog "SKIP: Port $Port already listening (PID $($listening.OwningProcess))" $TargetRoot
    exit 0
}

try {
    $PythonExe = Get-PythonPath -TargetRoot $TargetRoot -PythonPath $PythonPath
} catch {
    Write-Host "[ERROR] $($_)"
    Write-StartLog "ERROR: $_" $TargetRoot
    exit 1
}

$env:EDR_WD_ENABLE_PYWINAUTO = '1'
$env:EDR_WD_ENABLE_POWERSHELL = '1'
$env:EDR_WD_AUTOMATION_BACKEND = 'windows_pywinauto'
$env:EDR_WD_TARGET_ROOT = $TargetRoot

$serverLog = Join-Path $LogDir ('server.' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '.log')
$startLog = Join-Path $LogDir 'start.log'

Write-StartLog "Python=$PythonExe TargetRoot=$TargetRoot" $TargetRoot
Write-StartLog "Command: $PythonExe server.py --http --host $BindHost --port $Port" $TargetRoot

$proc = Start-Process -FilePath $PythonExe `
    -ArgumentList 'server.py', '--http', '--host', $BindHost, '--port', $Port `
    -WorkingDirectory $ServerDir `
    -PassThru `
    -RedirectStandardOutput $serverLog `
    -RedirectStandardError ($serverLog + '.stderr') `
    -WindowStyle Hidden

Start-Sleep -Seconds 5

if ($proc.HasExited) {
    Write-Host "[ERROR] Server exited immediately with code $($proc.ExitCode)"
    Write-StartLog "ERROR: Process exited with code $($proc.ExitCode)" $TargetRoot
    exit 1
}

$stillListening = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
if ($stillListening) {
    Write-Host "[OK] Server PID=$($proc.Id) listening on $BindHost:$Port"
    Write-StartLog "OK: PID=$($proc.Id) listening on $BindHost:$Port" $TargetRoot
    exit 0
} else {
    Write-Host "[ERROR] Server PID=$($proc.Id) started but port $Port not listening"
    Write-StartLog "ERROR: PID=$($proc.Id) but port not listening. Check $serverLog" $TargetRoot
    exit 1
}
