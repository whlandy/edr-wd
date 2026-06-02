#!/bin/bash
# register-hermes.sh — 可选：注册 edr-wd MCP 到 Hermes
#
# 用法:
#   bash agent/register-hermes.sh
#
# 前置条件：tunnel 已启动（EDR_WD_LOCAL_PORT 默认 18765）

set -e

LOCAL_PORT="${EDR_WD_LOCAL_PORT:-18765}"
SERVER_URL="http://127.0.0.1:${LOCAL_PORT}/mcp"

echo "=== Hermes MCP Registration ==="
echo "URL: $SERVER_URL"

HERMES_CONF="$HOME/.hermes/config.yaml"
HERMES_DIR="$HOME/.hermes"
mkdir -p "$HERMES_DIR"

if [ -f "$HERMES_CONF" ]; then
    if grep -q "^    edr-wd:" "$HERMES_CONF" 2>/dev/null; then
        echo "[Skip] edr-wd already in $HERMES_CONF"
    else
        if ! grep -q "^mcp_servers:" "$HERMES_CONF" 2>/dev/null; then
            printf "\nmcp_servers:\n  edr-wd:\n    url: \"%s/mcp\"\n" "$LOCAL_PORT" >> "$HERMES_CONF"
        else
            printf "  edr-wd:\n    url: \"%s/mcp\"\n" "$LOCAL_PORT" >> "$HERMES_CONF"
        fi
        echo "[OK] Added edr-wd to $HERMES_CONF"
        echo "Restart Hermes Agent to load the new MCP server."
    fi
else
    cat > "$HERMES_CONF" << EOF
mcp_servers:
  edr-wd:
    url: "http://127.0.0.1:${LOCAL_PORT}/mcp"
EOF
    echo "[Created] $HERMES_CONF"
    echo "Restart Hermes Agent to load the new MCP server."
fi
