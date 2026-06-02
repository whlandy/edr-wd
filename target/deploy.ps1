#!/usr/bin/env -pwsh
# deploy.ps1 - EDR-WD Windows one-click deployment
# Run as Administrator
# Usage: .\deploy.ps1 [-Port <port>] [-NoSsh] [-NoFw] [-AutoStart]

param(
    [string]$Port = "8765",
    [switch]$NoSsh,
    [switch]$NoFw,
    [switch]$AutoStart
)

$ErrorActionPreference = "Stop"

Write-Host "=== EDR-WD Deployment ===" -ForegroundColor Cyan
Write-Host "Port: $Port" -ForegroundColor Gray
Write-Host ""

# Check Python version
Write-Host "[0/5] Checking Python..." -ForegroundColor Cyan
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

# SSH Server
if (-not $NoSsh) {
    Write-Host "[1/5] Configuring SSH Server..." -ForegroundColor Cyan
    $cap = Get-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0 -ErrorAction SilentlyContinue
    if (-not $cap) {
        Write-Host "  Adding SSH Server capability..." -ForegroundColor Yellow
        $null = Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0 2>$null
    }
    $svc = Get-Service -Name sshd -ErrorAction SilentlyContinue
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
} else {
    Write-Host "[1/5] Skipping SSH" -ForegroundColor Gray
}

# Firewall
if (-not $NoFw) {
    Write-Host "[2/5] Configuring Firewall..." -ForegroundColor Cyan
    $rules = @(
        @{ Name="EDR-WD-SSH"; Port=22 },
        @{ Name="EDR-WD-MCP"; Port=$Port }
    )
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
} else {
    Write-Host "[2/5] Skipping Firewall" -ForegroundColor Gray
}

# Install deps
Write-Host "[3/5] Installing Python packages..." -ForegroundColor Cyan
$pkgs = @("fastmcp", "pywinauto", "psutil", "Pillow")
$failed = @()
foreach ($p in $pkgs) {
    # Use pip show to check if installed (no traceback risk)
    $status = & pip show $p 2>$null
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

# Verify
Write-Host "[4/5] Ready to start MCP Server..." -ForegroundColor Cyan
Write-Host "  Run: python -m edr_wd.server --http --host 0.0.0.0 --port $Port"
Write-Host ""

# Start
Write-Host "[5/5] Starting MCP Server..." -ForegroundColor Cyan

if ($AutoStart) {
    $env:EDR_WD_ENABLE_POWERSHELL = "1"
    $proc = Start-Process -FilePath python `
        -ArgumentList "-m edr_wd.server --http --host 0.0.0.0 --port $Port" `
        -EnvironmentVariables @{ EDR_WD_ENABLE_POWERSHELL = "1" } `
        -WindowStyle Hidden `
        -PassThru
    Start-Sleep -Seconds 2
    if ($proc.HasExited) {
        Write-Host "  [ERROR] Server failed to start" -ForegroundColor Red
        exit 1
    }
    Write-Host "  [OK] Server started in background (PID: $($proc.Id))" -ForegroundColor Green
    Write-Host "  Stop: Stop-Process -Id $($proc.Id)" -ForegroundColor Gray
    Write-Host "  Note: EDR_WD_ENABLE_POWERSHELL=1 (PowerShell tools enabled)" -ForegroundColor Gray
} else {
    $env:EDR_WD_ENABLE_POWERSHELL = "1"
    Write-Host "  EDR_WD_ENABLE_POWERSHELL=1 (PowerShell tools enabled)" -ForegroundColor Gray
    Write-Host "  Press Ctrl+C to stop" -ForegroundColor Yellow
    Write-Host ""
    python -m edr_wd.server --http --host 0.0.0.0 --port $Port
}
