#!/bin/bash
# register-openclaw.sh — 可选：注册 edr-wd MCP 到 OpenClaw
#
# 用法:
#   bash agent/register-openclaw.sh
#
# 前置条件：tunnel 已启动（EDR_WD_LOCAL_PORT 默认 18765）

set -e

LOCAL_PORT="${EDR_WD_LOCAL_PORT:-18765}"
SERVER_URL="http://127.0.0.1:${LOCAL_PORT}/mcp"

echo "=== OpenClaw MCP Registration ==="
echo "URL: $SERVER_URL"

if command -v openclaw >/dev/null 2>&1; then
    openclaw mcp set edr-wd "{\"url\":\"${SERVER_URL}\",\"transport\":\"streamable-http\"}" 2>/dev/null && \
        echo "[OK] Registered" || \
        echo "[Note] Run manually: openclaw mcp set edr-wd '{\"url\":\"${SERVER_URL}\",\"transport\":\"streamable-http\"}'"
else
    echo "[Note] OpenClaw not found. Configure manually:"
    echo "  openclaw mcp set edr-wd '{\"url\":\"${SERVER_URL}\",\"transport\":\"streamable-http\"}'"
fi
