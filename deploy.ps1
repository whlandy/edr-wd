#!/usr/bin/env -pwsh
# deploy.ps1 — EDR-WD MCP Server 一键部署脚本
# 在 Windows 上以管理员权限运行

param(
    [string]$Port = "8765",
    [switch]$AutoStart
)

$ErrorActionPreference = "Stop"

Write-Host "=== EDR-WD MCP Server 部署 ===" -ForegroundColor Cyan

# 1. 检查 Python
Write-Host "[1/4] 检查 Python..."
try {
    $pythonVersion = python --version 2>&1
    Write-Host "  Python: $pythonVersion"
} catch {
    Write-Host "[错误] 未找到 Python，请先安装 Python 3.9+" -ForegroundColor Red
    exit 1
}

# 2. 安装依赖
Write-Host "[2/4] 安装依赖..."
pip install fastmcp pywinauto psutil Pillow --quiet
if ($LASTEXITCODE -ne 0) {
    Write-Host "[错误] pip install 失败" -ForegroundColor Red
    exit 1
}
Write-Host "  依赖安装完成" -ForegroundColor Green

# 3. 验证包
Write-Host "[3/4] 验证包..."
$mods = @("fastmcp", "pywinauto", "psutil", "PIL")
foreach ($mod in $mods) {
    python -c "import $mod; print(f'  $mod: OK')" 2>&1
}

# 4. 启动服务
Write-Host "[4/4] 启动 MCP Server..."
$serviceCmd = "python -m edr_wd.server --http --port $Port"

if ($AutoStart) {
    Write-Host "  后台启动: $serviceCmd"
    Start-Process -FilePath python -ArgumentList "-m edr_wd.server --http --port $Port" -WindowStyle Hidden
    Write-Host "  服务已在后台启动 (端口 $Port)" -ForegroundColor Green
} else {
    Write-Host "  运行命令: $serviceCmd"
    Write-Host "  按 Ctrl+C 停止服务" -ForegroundColor Yellow
    python -m edr_wd.server --http --port $Port
}
