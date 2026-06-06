#!/usr/bin/env -pwsh
# deploy.ps1 - EDR-WD Windows lifecycle manager
# Run as Administrator
# Usage:
#   .\deploy.ps1 -Action bootstrap
#   .\deploy.ps1 -Action start
#   .\deploy.ps1 -Action status
#   .\deploy.ps1 -Action stop

param(
    [ValidateSet("bootstrap", "start", "status", "stop")]
    [string]$Action = "start",
    [string]$BindHost = "127.0.0.1",
    [string]$Port = "8765",
    [ValidateSet("auto", "process", "scheduled-task")]
    [string]$StartMode = "auto",
    [string]$TaskName = "EDR-WD-MCP",
    [switch]$NoSsh,
    [switch]$NoFw,
    [switch]$AutoStart
)

$ErrorActionPreference = "Stop"
$StatePath = Join-Path $PSScriptRoot ".edr-wd-state.json"
$LogDir = Join-Path $PSScriptRoot "logs"
$StdoutLog = Join-Path $LogDir "edr-wd.stdout.log"
$StderrLog = Join-Path $LogDir "edr-wd.stderr.log"
$LauncherPath = Join-Path $LogDir "start-edr-wd-server.ps1"

function Write-Section([string]$Label) {
    Write-Host $Label -ForegroundColor Cyan
}

function Write-State([int]$ProcessId, [string]$Mode) {
    New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
    $payload = [ordered]@{
        pid = $ProcessId
        mode = $Mode
        host = $BindHost
        port = [int]$Port
        started_at = (Get-Date).ToString("o")
        stdout_log = $StdoutLog
        stderr_log = $StderrLog
    }
    $payload | ConvertTo-Json -Depth 5 | Set-Content -Encoding UTF8 $StatePath
}

function Read-State {
    if (Test-Path $StatePath) {
        try {
            return (Get-Content $StatePath -Raw | ConvertFrom-Json)
        } catch {
            return $null
        }
    }
    return $null
}

function Get-ServerPid {
    $state = Read-State
    if ($state -and $state.pid) {
        try {
            $proc = Get-Process -Id $state.pid -ErrorAction Stop
            if ($proc) {
                return [int]$state.pid
            }
        } catch {
        }
    }

    try {
        $conn = Get-NetTCPConnection -LocalPort [int]$Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($conn) {
            return [int]$conn.OwningProcess
        }
    } catch {
    }

    return $null
}

function Test-McpHealth {
    $url = "http://127.0.0.1:$Port/mcp"
    try {
        $resp = Invoke-WebRequest -Uri $url -Method Get -Headers @{ Accept = "text/event-stream" } -TimeoutSec 5
        $session = $resp.Headers["Mcp-Session-Id"]
        return @{
            ok = $true
            status = [int]$resp.StatusCode
            session = $session
            note = "HTTP probe returned success"
        }
    } catch {
        $response = $_.Exception.Response
        if ($response) {
            $status = [int]$response.StatusCode
            $session = $response.Headers["Mcp-Session-Id"]
            return @{
                ok = ($status -eq 400 -or $status -eq 200)
                status = $status
                session = $session
                note = "HTTP probe returned expected MCP handshake response"
            }
        }

        return @{
            ok = $false
            error = $_.Exception.Message
            note = "HTTP probe failed"
        }
    }
}

function Test-InteractiveSession {
    $session = $env:SESSIONNAME
    if (-not $session) {
        Write-Host "  [WARN] SESSIONNAME is empty. Make sure this runs in an RDP or local interactive desktop session." -ForegroundColor Yellow
        return
    }
    if ($session -notmatch "^(Console|RDP-Tcp(#\d+)?)$") {
        Write-Host "  [WARN] SESSIONNAME=$session. GUI automation expects an interactive desktop session." -ForegroundColor Yellow
    } else {
        Write-Host "  [OK] Interactive session: $session" -ForegroundColor Green
    }
}

function Test-CurrentSessionInteractive {
    $session = $env:SESSIONNAME
    if (-not $session) {
        return $false
    }
    return ($session -match "^(Console|RDP-Tcp(#\d+)?)$")
}

function Resolve-InteractiveUser([string]$UserName) {
    if (-not $UserName) {
        return $null
    }
    if ($UserName -match "\\") {
        return $UserName
    }

    try {
        $explorers = Get-CimInstance Win32_Process -Filter "name = 'explorer.exe'" -ErrorAction SilentlyContinue
        foreach ($proc in $explorers) {
            $owner = Invoke-CimMethod -InputObject $proc -MethodName GetOwner -ErrorAction SilentlyContinue
            if ($owner -and $owner.User -eq $UserName) {
                if ($owner.Domain) {
                    return "$($owner.Domain)\$($owner.User)"
                }
                return $owner.User
            }
        }
    } catch {
    }

    try {
        $computerUser = (Get-CimInstance Win32_ComputerSystem -ErrorAction SilentlyContinue).UserName
        if ($computerUser) {
            $shortName = ($computerUser -split "\\")[-1]
            if ($shortName -eq $UserName) {
                return $computerUser
            }
        }
    } catch {
    }

    return "$env:COMPUTERNAME\$UserName"
}

function Get-ActiveInteractiveUser {
    try {
        $lines = (& quser.exe 2>$null) | Select-Object -Skip 1
        foreach ($line in $lines) {
            $normalized = ($line -replace "^\s*>", "").Trim()
            if (-not $normalized) {
                continue
            }
            $parts = $normalized -split "\s+"
            if ($parts.Count -lt 4) {
                continue
            }
            $userName = $parts[0]
            $sessionName = $parts[1]
            $stateIndex = 3
            if ($parts[2] -match "^\d+$") {
                $stateIndex = 3
            } elseif ($parts.Count -gt 3 -and $parts[3] -match "^\d+$") {
                $sessionName = "$($parts[1]) $($parts[2])"
                $stateIndex = 4
            }
            if ($parts.Count -le $stateIndex) {
                continue
            }
            $state = $parts[$stateIndex]
            if ($state -eq "Active" -and $sessionName.ToLowerInvariant() -match "^(console|rdp-tcp)") {
                return Resolve-InteractiveUser $userName
            }
        }
    } catch {
    }
    return $null
}

function Write-Launcher {
    New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
    $rootLiteral = $PSScriptRoot.Replace("'", "''")
    $stdoutLiteral = $StdoutLog.Replace("'", "''")
    $stderrLiteral = $StderrLog.Replace("'", "''")
    $launcher = @"
`$ErrorActionPreference = "Stop"
`$env:EDR_WD_ENABLE_POWERSHELL = "1"
Set-Location '$rootLiteral'
& python -m edr_wd.server --http --host $BindHost --port $Port 1> '$stdoutLiteral' 2> '$stderrLiteral'
"@
    $launcher | Set-Content -Encoding UTF8 $LauncherPath
}

function Check-Python {
    Write-Section "[0/4] Checking Python..."
    try {
        $v = python --version 2>&1
        if ($v -match "Python (\d+)\.(\d+)") {
            $maj = [int]$Matches[1]
            $min = [int]$Matches[2]
            if ($maj -lt 3 -or ($maj -eq 3 -and $min -lt 10)) {
                Write-Host "  [ERROR] Python $maj.$min found, need 3.10+" -ForegroundColor Red
                Write-Host "  Download: https://www.python.org/downloads/" -ForegroundColor Yellow
                exit 1
            }
            Write-Host "  [OK] $v" -ForegroundColor Green
        }
    } catch {
        Write-Host "  [ERROR] Python not found. Install Python 3.10+ first." -ForegroundColor Red
        exit 1
    }
}

function Configure-Ssh {
    if ($NoSsh) {
        Write-Host "[1/4] Skipping SSH" -ForegroundColor Gray
        return
    }

    Write-Section "[1/4] Configuring SSH Server..."
    $cap = Get-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0 -ErrorAction SilentlyContinue
    if (-not $cap) {
        Write-Host "  Adding SSH Server capability..." -ForegroundColor Yellow
        $null = Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0 2>$null
    }

    $svc = Get-Service -Name sshd -ErrorAction SilentlyContinue
    if (-not $svc) {
        Write-Host "  [ERROR] sshd service not available after capability install." -ForegroundColor Red
        exit 1
    }

    if ($svc.Status -ne 'Running') {
        $null = Start-Service sshd 2>$null
        Write-Host "  [OK] sshd started" -ForegroundColor Green
    } else {
        Write-Host "  [OK] sshd already running" -ForegroundColor Green
    }

    if ($AutoStart -or $svc.StartType -ne 'Automatic') {
        $null = Set-Service -Name sshd -StartupType Automatic 2>$null
        Write-Host "  [OK] auto-start enabled" -ForegroundColor Green
    }
}

function Configure-Firewall {
    if ($NoFw) {
        Write-Host "[2/4] Skipping Firewall" -ForegroundColor Gray
        return
    }

    Write-Section "[2/4] Configuring Firewall..."
    $rules = @(@{ Name = "EDR-WD-SSH"; Port = 22 })
    if ($BindHost -ne "127.0.0.1" -and $BindHost -ne "localhost") {
        $rules += @{ Name = "EDR-WD-MCP"; Port = [int]$Port }
    } else {
        Write-Host "  [SKIP] MCP firewall rule not needed for loopback bind ($BindHost)" -ForegroundColor Gray
    }

    foreach ($r in $rules) {
        $ex = Get-NetFirewallRule -Name $r.Name -ErrorAction SilentlyContinue
        if ($ex) {
            Write-Host "  [SKIP] Port $($r.Port) already open" -ForegroundColor Gray
        } else {
            New-NetFirewallRule -Name $r.Name `
                -DisplayName "EDR-WD ($($r.Port))" `
                -Description "EDR-WD automation" `
                -Enabled True `
                -Direction Inbound `
                -Protocol TCP `
                -Action Allow `
                -LocalPort $r.Port | Out-Null
            Write-Host "  [OK] Port $($r.Port) opened" -ForegroundColor Green
        }
    }
}

function Install-Dependencies {
    Write-Section "[3/4] Installing Python packages..."
    $pkgs = @("fastmcp", "pywinauto", "psutil", "Pillow")
    $failed = @()
    foreach ($p in $pkgs) {
        & pip show $p 2>$null | Out-Null
        if ($LASTEXITCODE -eq 0) {
            Write-Host "  [OK] $p already installed" -ForegroundColor Green
        } else {
            Write-Host "  Installing $p..." -ForegroundColor Yellow
            $null = pip install $p --quiet 2>$null
            if ($LASTEXITCODE -eq 0) {
                Write-Host "  [OK] $p installed" -ForegroundColor Green
            } else {
                Write-Host "  [FAIL] $p failed" -ForegroundColor Red
                $failed += $p
            }
        }
    }

    if ($failed.Count -gt 0) {
        Write-Host ""
        Write-Host "Failed packages: $($failed -join ', ')" -ForegroundColor Red
    }
}

function Start-ServerProcess {
    Write-Section "[4/4] Starting MCP Server..."
    $currentPid = Get-ServerPid
    if ($currentPid) {
        Write-Host "  [OK] Server already running (PID: $currentPid)" -ForegroundColor Green
        return
    }

    New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
    $env:EDR_WD_ENABLE_POWERSHELL = "1"
    $proc = Start-Process -FilePath python `
        -ArgumentList "-m edr_wd.server --http --host $BindHost --port $Port" `
        -WorkingDirectory $PSScriptRoot `
        -PassThru `
        -RedirectStandardOutput $StdoutLog `
        -RedirectStandardError $StderrLog

    Start-Sleep -Seconds 2
    if ($proc.HasExited) {
        Write-Host "  [ERROR] Server failed to start" -ForegroundColor Red
        if (Test-Path $StderrLog) {
            Write-Host "  --- stderr tail ---" -ForegroundColor DarkYellow
            Get-Content $StderrLog -Tail 40 | ForEach-Object { Write-Host "  $_" -ForegroundColor DarkYellow }
        }
        exit 1
    }

    Write-State -ProcessId $proc.Id -Mode "background"
    Write-Host "  [OK] Server started in background (PID: $($proc.Id))" -ForegroundColor Green
    Write-Host "  Log stdout: $StdoutLog" -ForegroundColor Gray
    Write-Host "  Log stderr: $StderrLog" -ForegroundColor Gray
    Write-Host "  Note: EDR_WD_ENABLE_POWERSHELL=1 (PowerShell tools enabled)" -ForegroundColor Gray
}

function Start-ServerScheduledTask {
    Write-Section "[4/4] Starting MCP Server via interactive scheduled task..."
    $currentPid = Get-ServerPid
    if ($currentPid) {
        Write-Host "  [OK] Server already running (PID: $currentPid)" -ForegroundColor Green
        return
    }

    $interactiveUser = Get-ActiveInteractiveUser
    if (-not $interactiveUser) {
        Write-Host "  [ERROR] No active Console/RDP user session found." -ForegroundColor Red
        Write-Host "  The server must run inside a logged-on Windows desktop for GUI automation." -ForegroundColor Yellow
        Write-Host "  Log in once to Windows, then run this command again from the agent side; no RDP command execution is needed." -ForegroundColor Yellow
        exit 1
    }

    Write-Launcher
    try {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue | Out-Null
        $action = New-ScheduledTaskAction `
            -Execute "powershell.exe" `
            -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$LauncherPath`""
        $principal = New-ScheduledTaskPrincipal `
            -UserId $interactiveUser `
            -LogonType Interactive `
            -RunLevel Highest
        $settings = New-ScheduledTaskSettingsSet `
            -AllowStartIfOnBatteries `
            -DontStopIfGoingOnBatteries `
            -ExecutionTimeLimit (New-TimeSpan -Days 7)

        Register-ScheduledTask `
            -TaskName $TaskName `
            -Action $action `
            -Principal $principal `
            -Settings $settings `
            -Description "EDR-WD MCP server in the active interactive desktop" `
            -Force | Out-Null

        Start-ScheduledTask -TaskName $TaskName
    } catch {
        Write-Host "  [ERROR] Failed to create or run scheduled task: $($_.Exception.Message)" -ForegroundColor Red
        exit 1
    }

    $deadline = (Get-Date).AddSeconds(20)
    $serverPid = $null
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Seconds 1
        $serverPid = Get-ServerPid
        if ($serverPid) {
            break
        }
    }

    if (-not $serverPid) {
        Write-Host "  [ERROR] Scheduled task ran, but server did not listen on port $Port" -ForegroundColor Red
        if (Test-Path $StderrLog) {
            Write-Host "  --- stderr tail ---" -ForegroundColor DarkYellow
            Get-Content $StderrLog -Tail 40 | ForEach-Object { Write-Host "  $_" -ForegroundColor DarkYellow }
        }
        exit 1
    }

    Write-State -ProcessId $serverPid -Mode "scheduled-task"
    Write-Host "  [OK] Server started in interactive session (PID: $serverPid, User: $interactiveUser)" -ForegroundColor Green
    Write-Host "  Task: $TaskName" -ForegroundColor Gray
    Write-Host "  Log stdout: $StdoutLog" -ForegroundColor Gray
    Write-Host "  Log stderr: $StderrLog" -ForegroundColor Gray
    Write-Host "  Note: EDR_WD_ENABLE_POWERSHELL=1 (PowerShell tools enabled)" -ForegroundColor Gray
}

function Start-Server {
    if ($StartMode -eq "process") {
        Start-ServerProcess
        return
    }
    if ($StartMode -eq "scheduled-task") {
        Start-ServerScheduledTask
        return
    }
    if (Test-CurrentSessionInteractive) {
        Start-ServerProcess
    } else {
        Start-ServerScheduledTask
    }
}

function Show-Status {
    Write-Section "[status] EDR-WD MCP Server"
    $serverPid = Get-ServerPid
    if ($serverPid) {
        Write-Host "  [OK] Running (PID: $serverPid)" -ForegroundColor Green
        try {
            $proc = Get-Process -Id $serverPid -ErrorAction Stop
            Write-Host "  Process: $($proc.ProcessName)" -ForegroundColor Gray
        } catch {
            Write-Host "  Process: not found in Get-Process output" -ForegroundColor Yellow
        }
    } else {
        Write-Host "  [WARN] Not running" -ForegroundColor Yellow
    }

    $state = Read-State
    if ($state) {
        Write-Host "  State file: $StatePath" -ForegroundColor Gray
        Write-Host "  Host/Port: $($state.host):$($state.port)" -ForegroundColor Gray
        Write-Host "  Started: $($state.started_at)" -ForegroundColor Gray
        Write-Host "  Logs: $($state.stdout_log) / $($state.stderr_log)" -ForegroundColor Gray
    } else {
        Write-Host "  State file: missing" -ForegroundColor Gray
    }

    try {
        $conn = Get-NetTCPConnection -LocalPort [int]$Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($conn) {
            Write-Host "  Port: listening (OwningProcess=$($conn.OwningProcess))" -ForegroundColor Green
        } else {
            Write-Host "  Port: not listening on $Port" -ForegroundColor Yellow
        }
    } catch {
        Write-Host "  Port check unavailable" -ForegroundColor Yellow
    }

    $health = Test-McpHealth
    if ($health.ok) {
        Write-Host "  HTTP/MCP: ok (status=$($health.status), session=$($health.session))" -ForegroundColor Green
    } elseif ($health.error) {
        Write-Host "  HTTP/MCP: failed ($($health.error))" -ForegroundColor Yellow
    } else {
        Write-Host "  HTTP/MCP: unexpected response (status=$($health.status), session=$($health.session))" -ForegroundColor Yellow
    }
}

function Stop-Server {
    Write-Section "[stop] Stopping MCP Server..."
    $serverPid = Get-ServerPid
    if (-not $serverPid) {
        Write-Host "  [OK] No running server found" -ForegroundColor Green
        if (Test-Path $StatePath) {
            Remove-Item $StatePath -Force
        }
        return
    }

    try {
        & taskkill /F /T /PID $serverPid | Out-Null
        Start-Sleep -Seconds 1
        if (Get-Process -Id $serverPid -ErrorAction SilentlyContinue) {
            Write-Host "  [WARN] taskkill did not fully stop PID $serverPid, trying Stop-Process" -ForegroundColor Yellow
            Stop-Process -Id $serverPid -Force -ErrorAction SilentlyContinue
        }
        Write-Host "  [OK] Server stopped (PID: $serverPid)" -ForegroundColor Green
    } catch {
        Write-Host "  [ERROR] Failed to stop PID $serverPid : $($_.Exception.Message)" -ForegroundColor Red
        exit 1
    } finally {
        if (Test-Path $StatePath) {
            Remove-Item $StatePath -Force -ErrorAction SilentlyContinue
        }
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue | Out-Null
    }
}

Write-Host "=== EDR-WD Lifecycle Manager ===" -ForegroundColor Cyan
Write-Host "Action: $Action" -ForegroundColor Gray
Write-Host "Host: $BindHost" -ForegroundColor Gray
Write-Host "Port: $Port" -ForegroundColor Gray
Write-Host "StartMode: $StartMode" -ForegroundColor Gray
Write-Host ""

switch ($Action) {
    "bootstrap" {
        Test-InteractiveSession
        Check-Python
        Configure-Ssh
        Configure-Firewall
        Install-Dependencies
        Write-Host ""
        Write-Host "Bootstrap complete. Use '.\deploy.ps1 -Action start' to launch the MCP server." -ForegroundColor Green
    }
    "start" {
        Test-InteractiveSession
        Check-Python
        Configure-Ssh
        Configure-Firewall
        Install-Dependencies
        Start-Server
    }
    "status" {
        Show-Status
    }
    "stop" {
        Stop-Server
    }
}
