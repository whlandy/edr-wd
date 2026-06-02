#!/bin/bash
# setup-mac.sh — Mac 配置脚本（tunnel + 可选 MCP client 注册）
#
# 用法:
#   bash setup-mac.sh <WINDOWS_IP> <WINDOWS_USER> [--client none|openclaw|hermes]
#
#   --client none      默认：只配 tunnel，不注册任何 MCP client
#   --client openclaw  注册到 OpenClaw
#   --client hermes    注册到 Hermes（opt-in，显式调用）
#
# 示例:
#   bash setup-mac.sh 170.170.11.26 admin
#   bash setup-mac.sh 170.170.11.26 admin --client openclaw

set -e

WIN_IP=""
WIN_USER=""
CLIENT_MODE="none"

# Parse arguments
while [ $# -gt 0 ]; do
    case "$1" in
        --client)
            CLIENT_MODE="$2"
            shift 2
            ;;
        -*)
            echo "Unknown option: $1"
            exit 1
            ;;
        *)
            if [ -z "$WIN_IP" ]; then
                WIN_IP="$1"
            elif [ -z "$WIN_USER" ]; then
                WIN_USER="$1"
            fi
            shift
            ;;
    esac
done

if [ -z "$WIN_IP" ] || [ -z "$WIN_USER" ]; then
    echo "Usage: bash $0 <WINDOWS_IP> <WINDOWS_USER> [--client none|openclaw|hermes]"
    exit 1
fi

LOCAL_PORT="${EDR_WD_LOCAL_PORT:-18765}"
REMOTE_PORT="${EDR_WD_REMOTE_PORT:-8765}"
HOST_ALIAS="edr-wd"

echo "=== EDR-WD Mac Setup ==="
echo "Windows IP: $WIN_IP"
echo "Windows user: $WIN_USER"
echo "Client mode: $CLIENT_MODE"
echo ""

# ── 1. SSH config ──────────────────────────────────────────────
echo "[1/2] Configuring SSH tunnel..."

SSH_CONF_DIR="$HOME/.ssh"
SSH_CONF="$SSH_CONF_DIR/config"
mkdir -p "$SSH_CONF_DIR"
chmod 700 "$SSH_CONF_DIR"

TUNNEL_LINE="LocalForward ${LOCAL_PORT} 127.0.0.1:${REMOTE_PORT}"

if grep -q "^Host $HOST_ALIAS$" "$SSH_CONF" 2>/dev/null; then
    # Update existing entry
    if grep -q "$TUNNEL_LINE" "$SSH_CONF" 2>/dev/null; then
        echo "  [OK] SSH config already has correct LocalForward"
    else
        # Replace old LocalForward lines for this host
        sed -i '' "/^Host $HOST_ALIAS$/,/^Host /{ /LocalForward /d; }" "$SSH_CONF"
        sed -i '' "/^Host $HOST_ALIAS$/a\\    $TUNNEL_LINE" "$SSH_CONF"
        echo "  [Updated] SSH config LocalForward"
    fi
else
    cat >> "$SSH_CONF" << EOF

Host $HOST_ALIAS
    HostName $WIN_IP
    User $WIN_USER
    $TUNNEL_LINE
    ServerAliveInterval 60
EOF
    echo "  [Created] SSH config entry"
fi

# ── 2. MCP Client registration (optional) ────────────────────
echo ""
echo "[2/2] MCP client: $CLIENT_MODE"

case "$CLIENT_MODE" in
    none)
        echo "  [Skip] No MCP client configured (tunnel only)"
        ;;
    openclaw)
        echo "  [OpenClaw] Registering edr-wd..."
        if command -v openclaw >/dev/null 2>&1; then
            openclaw mcp set edr-wd '{"url":"http://127.0.0.1:'"${LOCAL_PORT}"'/mcp","transport":"streamable-http","connectionTimeoutMs":10000}' 2>/dev/null && \
            echo "  [OK] OpenClaw registered" || \
            echo "  [Note] Run manually: openclaw mcp set edr-wd '{\"url\":\"http://127.0.0.1:${LOCAL_PORT}/mcp\",...}'"
        else
            echo "  [Note] OpenClaw not found. Install or use:"
            echo "    openclaw mcp set edr-wd '{\"url\":\"http://127.0.0.1:${LOCAL_PORT}/mcp\",\"transport\":\"streamable-http\",\"connectionTimeoutMs\":10000}'"
        fi
        ;;
    hermes)
        HERMES_CONF="$HOME/.hermes/config.yaml"
        HERMES_DIR="$HOME/.hermes"
        mkdir -p "$HERMES_DIR"

        if [ -f "$HERMES_CONF" ]; then
            if grep -q "^    edr-wd:" "$HERMES_CONF" 2>/dev/null; then
                echo "  [Skip] edr-wd already in $HERMES_CONF"
            else
                # Append mcp_servers entry
                if ! grep -q "^mcp_servers:" "$HERMES_CONF" 2>/dev/null; then
                    printf "\nmcp_servers:\n  edr-wd:\n    url: \"http://127.0.0.1:%s/mcp\"\n" "$LOCAL_PORT" >> "$HERMES_CONF"
                else
                    printf "  edr-wd:\n    url: \"http://127.0.0.1:%s/mcp\"\n" "$LOCAL_PORT" >> "$HERMES_CONF"
                fi
                echo "  [Created] Hermes MCP client entry"
            fi
        else
            cat > "$HERMES_CONF" << EOF
mcp_servers:
  edr-wd:
    url: "http://127.0.0.1:${LOCAL_PORT}/mcp"
EOF
            echo "  [Created] $HERMES_CONF"
        fi
        ;;
    *)
        echo "  [Unknown client mode: $CLIENT_MODE]"
        ;;
esac

# ── 3. Verify tunnel ───────────────────────────────────────────
echo ""
echo "[Test] Verifying SSH tunnel..."
if ssh -o ConnectTimeout=5 -o StrictHostKeyChecking=no -o BatchMode=yes "$HOST_ALIAS" "echo OK" 2>/dev/null; then
    echo "  OK: SSH connection to $HOST_ALIAS works"
else
    echo "  WARNING: Cannot connect to $HOST_ALIAS."
    echo "  Check: Windows SSH Server running, credentials correct."
fi

echo ""
echo "=== Setup complete ==="
echo ""
echo "Next steps:"
echo "  1. Start tunnel:    bash agent/tunnel.sh start"
echo "  2. Start MCP server on Windows:"
echo "       cd C:\\path\\to\\edr-wd\\target"
echo "       python -m edr_wd.server --http --port 8765"
if [ "$CLIENT_MODE" = "hermes" ]; then
    echo "  3. Restart Hermes Agent to load edr-wd tools"
fi
