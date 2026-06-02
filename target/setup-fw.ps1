#!/usr/bin/env -pwsh
# setup-fw.ps1 — Windows 防火墙配置
# 以管理员权限运行

param(
    [switch]$ExposeMcp
)

$ErrorActionPreference = "Stop"

Write-Host "=== Windows 防火墙配置 ===" -ForegroundColor Cyan

# 规则名列表（幂等：重复运行不会重复创建）
$rules = @(
    @{ Name="EDR-WD-SSH"; DisplayName="EDR-WD SSH (22)"; Port=22; Desc="SSH remote access for EDR-WD" }
)

if ($ExposeMcp) {
    $rules += @{ Name="EDR-WD-MCP"; DisplayName="EDR-WD MCP (8765)"; Port=8765; Desc="MCP server direct access for EDR-WD" }
} else {
    Write-Host "  [跳过] MCP 端口 8765 默认只绑定 127.0.0.1，通过 SSH tunnel 访问，无需放行" -ForegroundColor Yellow
}

foreach ($rule in $rules) {
    $existing = Get-NetFirewallRule -Name $rule.Name -ErrorAction SilentlyContinue
    if ($existing) {
        Write-Host "  [跳过] $($rule.DisplayName) 已存在" -ForegroundColor Yellow
    } else {
        New-NetFirewallRule -Name $rule.Name `
            -DisplayName $rule.DisplayName `
            -Description $rule.Desc `
            -Enabled True `
            -Direction Inbound `
            -Protocol TCP `
            -Action Allow `
            -LocalPort $rule.Port | Out-Null
        Write-Host "  [创建] $($rule.DisplayName)" -ForegroundColor Green
    }
}

Write-Host ""
if ($ExposeMcp) {
    Write-Host "完成。端口 22 (SSH) 和 8765 (MCP) 已放行。" -ForegroundColor Cyan
} else {
    Write-Host "完成。端口 22 (SSH) 已放行；MCP 端口保持 loopback/tunnel 访问。" -ForegroundColor Cyan
}
