#!/bin/bash
# setup-mac.sh — Mac 配置：SSH config + 启动 tunnel
#
# 用法:
#   bash setup-mac.sh <WINDOWS_IP> <WINDOWS_USER>
#
# 示例:
#   bash agent/setup-mac.sh 170.170.11.26 admin

set -e

WIN_IP=""
WIN_USER=""

while [ $# -gt 0 ]; do
    case "$1" in
        -*)
            echo "Usage: bash $0 <WINDOWS_IP> <WINDOWS_USER>"
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
    echo "Usage: bash $0 <WINDOWS_IP> <WINDOWS_USER>"
    exit 1
fi

LOCAL_PORT="${EDR_WD_LOCAL_PORT:-18765}"
REMOTE_PORT="${EDR_WD_REMOTE_PORT:-8765}"
HOST_ALIAS="edr-wd"

echo "=== EDR-WD Mac Setup ==="
echo "Windows IP: $WIN_IP"
echo "Windows user: $WIN_USER"
echo ""

# ── SSH config ──────────────────────────────────────────────
echo "[1/2] Configuring SSH tunnel..."

SSH_CONF_DIR="$HOME/.ssh"
SSH_CONF="$SSH_CONF_DIR/config"
mkdir -p "$SSH_CONF_DIR"
chmod 700 "$SSH_CONF_DIR"

TUNNEL_LINE="LocalForward ${LOCAL_PORT} 127.0.0.1:${REMOTE_PORT}"

if grep -q "^Host $HOST_ALIAS$" "$SSH_CONF" 2>/dev/null; then
    if grep -q "$TUNNEL_LINE" "$SSH_CONF" 2>/dev/null; then
        echo "  [OK] SSH config already has correct LocalForward"
    else
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

# ── Start tunnel ────────────────────────────────────────────
echo ""
echo "[2/2] Starting SSH tunnel..."

export EDR_WD_HOST="$WIN_IP"
export EDR_WD_USER="$WIN_USER"

cd "$(dirname "$0")"
bash tunnel.sh start

echo ""
echo "=== Setup complete ==="
echo ""
echo "To register an MCP client (optional):"
echo "  bash agent/register-openclaw.sh    # OpenClaw"
echo "  bash agent/register-hermes.sh     # Hermes"
