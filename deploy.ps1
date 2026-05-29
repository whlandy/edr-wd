#!/usr/bin/env -pwsh
# deploy.ps1 — EDR-WD Windows 一键部署脚本
# 以管理员权限运行
# 用法: .\deploy.ps1 [-Port <端口>] [-NoSsh] [-NoFw]

param(
    [string]$Port = "8765",
    [switch]$NoSsh,
    [switch]$NoFw,
    [switch]$AutoStart
)

$ErrorActionPreference = "Stop"

Write-Host "=== EDR-WD 一键部署 ===" -ForegroundColor Cyan
Write-Host "端口: $Port" -ForegroundColor Gray
Write-Host ""

# -------------------------------------------------------------------
# 0. 检查 Python 版本
# -------------------------------------------------------------------
Write-Host "[0/5] 检查 Python..." -ForegroundColor Cyan
try {
    $pythonVersion = python --version 2>&1
    if ($pythonVersion -match "Python (\d+)\.(\d+)") {
        $major = [int]$Matches[1]
        $minor = [int]$Matches[2]
        if ($major -lt 3 -or ($major -eq 3 -and $minor -lt 10)) {
            Write-Host "  ❌ Python $major.$minor 不支持，需要 Python 3.10+" -ForegroundColor Red
            Write-Host "  请升级: https://www.python.org/downloads/" -ForegroundColor Yellow
            exit 1
        }
        Write-Host "  ✅ $pythonVersion" -ForegroundColor Green
    }
} catch {
    Write-Host "  ❌ 未找到 Python，请先安装 Python 3.10+" -ForegroundColor Red
    exit 1
}

# -------------------------------------------------------------------
# 1. SSH Server 配置
# -------------------------------------------------------------------
if (-not $NoSsh) {
    Write-Host "[1/5] 配置 SSH Server..." -ForegroundColor Cyan
    $cap = Get-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0 -ErrorAction SilentlyContinue
    if (-not $cap) {
        Write-Host "  添加 SSH Server 功能..."
        Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0 | Out-Null
    }
    $svc = Get-Service -Name sshd -ErrorAction SilentlyContinue
    if ($svc.Status -ne 'Running') {
        Start-Service sshd
        Write-Host "  ✅ sshd 已启动" -ForegroundColor Green
    } else {
        Write-Host "  ✅ sshd 已在运行" -ForegroundColor Green
    }
    if ($AutoStart -or $svc.StartType -ne 'Automatic') {
        Set-Service -Name sshd -StartupType Automatic
        Write-Host "  ✅ 开机自启已设置" -ForegroundColor Green
    }
} else {
    Write-Host "[1/5] 跳过 SSH 配置" -ForegroundColor Gray
}

# -------------------------------------------------------------------
# 2. 防火墙配置
# -------------------------------------------------------------------
if (-not $NoFw) {
    Write-Host "[2/5] 配置防火墙..." -ForegroundColor Cyan
    $fwRules = @(
        @{ Name="EDR-WD-SSH"; DisplayName="EDR-WD SSH (22)"; Port=22 },
        @{ Name="EDR-WD-MCP"; DisplayName="EDR-WD MCP ($Port)"; Port=$Port }
    )
    foreach ($rule in $fwRules) {
        $existing = Get-NetFirewallRule -Name $rule.Name -ErrorAction SilentlyContinue
        if ($existing) {
            Write-Host "  [跳过] $($rule.DisplayName) 已存在" -ForegroundColor Gray
        } else {
            New-NetFirewallRule -Name $rule.Name `
                -DisplayName $rule.DisplayName `
                -Description "EDR-WD automation" `
                -Enabled True `
                -Direction Inbound `
                -Protocol TCP `
                -Action Allow `
                -LocalPort $rule.Port | Out-Null
            Write-Host "  ✅ $($rule.DisplayName) 已放行" -ForegroundColor Green
        }
    }
} else {
    Write-Host "[2/5] 跳过防火墙配置" -ForegroundColor Gray
}

# -------------------------------------------------------------------
# 3. 安装依赖
# -------------------------------------------------------------------
Write-Host "[3/5] 安装 Python 依赖..." -ForegroundColor Cyan
$mods = @("fastmcp", "pywinauto", "psutil", "Pillow")
$allOk = $true
foreach ($mod in $mods) {
    python -c "import ${mod}" 2>&1 | Out-Null
    if ($LASTEXITCODE -eq 0) {
        Write-Host "  ✅ $mod" -ForegroundColor Green
    } else {
        Write-Host "  ⬇️  安装 $mod..." -ForegroundColor Yellow
        pip install $mod --quiet
        if ($LASTEXITCODE -eq 0) {
            Write-Host "  ✅ $mod 安装完成" -ForegroundColor Green
        } else {
            Write-Host "  ❌ $mod 安装失败" -ForegroundColor Red
            $allOk = $false
        }
    }
}

if (-not $allOk) {
    Write-Host ""
    Write-Host "部分依赖安装失败，请手动检查" -ForegroundColor Red
}

# -------------------------------------------------------------------
# 4. 验证服务
# -------------------------------------------------------------------
Write-Host "[4/5] 验证服务..." -ForegroundColor Cyan
$serverRunning = $false
$boundAddr = "127.0.0.1"

# 检测是否需要绑定 0.0.0.0（外部访问）
$bindAll = $true  # 默认允许外部直连

$serviceCmd = "python -m edr_wd.server --http --host $boundAddr --port $Port"

Write-Host "  启动命令: $serviceCmd"
Write-Host "  按 Ctrl+C 停止" -ForegroundColor Yellow
Write-Host ""

# -------------------------------------------------------------------
# 5. 启动 MCP Server
# -------------------------------------------------------------------
Write-Host "[5/5] 启动 MCP Server..." -ForegroundColor Cyan

if ($AutoStart) {
    # 后台运行模式（用于开机自启）
    $proc = Start-Process -FilePath python -ArgumentList "-m edr_wd.server --http --host $boundAddr --port $Port" -WindowStyle Hidden -PassThru
    Start-Sleep -Seconds 2
    if ($proc.HasExited) {
        Write-Host "  ❌ 服务启动失败" -ForegroundColor Red
        exit 1
    }
    Write-Host "  ✅ 服务已在后台启动 (PID: $($proc.Id))" -ForegroundColor Green
    Write-Host "  停止命令: Stop-Process -Id $($proc.Id)" -ForegroundColor Gray
} else {
    # 前台运行模式
    python -m edr_wd.server --http --host $boundAddr --port $Port
}
