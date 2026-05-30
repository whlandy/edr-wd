#!/usr/bin/env -pwsh
# setup-ssh.ps1 — Windows SSH Server 快速配置
# 以管理员权限运行

param(
    [switch]$AutoStart
)

$ErrorActionPreference = "Stop"

Write-Host "=== Windows SSH Server 配置 ===" -ForegroundColor Cyan

# 1. 检查并添加 SSH Server 功能
Write-Host "[1/3] 检查 SSH Server 功能..."
$cap = Get-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0 -ErrorAction SilentlyContinue
if (-not $cap) {
    Write-Host "  添加 SSH Server 功能..."
    Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0 | Out-Null
}

# 2. 启动并配置 sshd
Write-Host "[2/3] 启动 sshd 服务..."
$svc = Get-Service -Name sshd -ErrorAction SilentlyContinue
if ($svc.Status -ne 'Running') {
    Start-Service sshd
    Write-Host "  sshd 已启动" -ForegroundColor Green
} else {
    Write-Host "  sshd 已在运行" -ForegroundColor Green
}

if ($AutoStart -or $svc.StartType -ne 'Automatic') {
    Set-Service -Name sshd -StartupType Automatic
    Write-Host "  已设置开机自启" -ForegroundColor Green
}

# 3. 验证
Write-Host "[3/3] 验证..."
$listening = netstat -an | Select-String ":22\s+LISTEN"
if ($listening) {
    Write-Host "  ✅ SSH 端口 22 已监听" -ForegroundColor Green
} else {
    Write-Host "  ❌ SSH 端口 22 未监听，请检查" -ForegroundColor Red
}

Write-Host ""
Write-Host "完成。现在可以：ssh <用户名>@<本机IP>" -ForegroundColor Cyan
