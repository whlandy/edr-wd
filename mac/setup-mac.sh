#!/bin/bash
# setup-mac.sh — Mac 上配置 EDR-WD SSH Tunnel + Hermes MCP Client
# 用法: bash setup-mac.sh <WINDOWS_IP> <WINDOWS_USER>
# 示例: bash setup-mac.sh 170.170.11.26 admin

set -e

WIN_IP="${1:?用法: bash $0 <WINDOWS_IP> <WINDOWS_USER>}"
WIN_USER="${2:?用法: bash $0 <WINDOWS_IP> <WINDOWS_USER>}"
LOCAL_PORT=18765
REMOTE_PORT=8765
HOST_ALIAS="edr-win"

echo "=== EDR-WD Mac 配置 ==="
echo "Windows IP: $WIN_IP"
echo "Windows 用户: $WIN_USER"
echo ""

# 1. 创建 SSH config 条目
echo "[1/3] 配置 SSH tunnel..."

SSH_CONF_DIR="$HOME/.ssh"
SSH_CONF="$SSH_CONF_DIR/config"

mkdir -p "$SSH_CONF_DIR"
chmod 700 "$SSH_CONF_DIR"

# 检查是否已有 edr-win 条目
if grep -q "^Host $HOST_ALIAS" "$SSH_CONF" 2>/dev/null; then
    echo "  [跳过] $HOST_ALIAS 已存在于 ~/.ssh/config"
else
    cat >> "$SSH_CONF" << EOF

Host $HOST_ALIAS
    HostName $WIN_IP
    User $WIN_USER
    LocalForward $LOCAL_PORT 127.0.0.1:$REMOTE_PORT
    ServerAliveInterval 60
EOF
    echo "  [创建] $HOST_ALIAS 条目已添加"
fi

# 2. 配置 Hermes MCP Client
echo "[2/3] 配置 Hermes MCP Client..."

HERMES_CONF="$HOME/.hermes/config.yaml"
HERMES_DIR="$HOME/.hermes"

mkdir -p "$HERMES_DIR"

if [ -f "$HERMES_CONF" ]; then
    # 检查是否已有 edr-wd 条目
    if grep -q "^    edr-wd:" "$HERMES_CONF" 2>/dev/null; then
        echo "  [跳过] edr-wd 已配置于 $HERMES_CONF"
    else
        # 追加到 yaml
        cat >> "$HERMES_CONF" << EOF
  edr-wd:
    command: "ssh"
    args: ["-N", "-f", "$HOST_ALIAS"]
    url: "http://127.0.0.1:$LOCAL_PORT"
EOF
        echo "  [创建] edr-wd 条目已追加到 $HERMES_CONF"
    fi
else
    cat > "$HERMES_CONF" << EOF
mcp:
  servers:
    edr-wd:
      command: "ssh"
      args: ["-N", "-f", "$HOST_ALIAS"]
      url: "http://127.0.0.1:$LOCAL_PORT"
EOF
    echo "  [创建] $HERMES_CONF"
fi

echo ""
echo "[3/3] 验证 tunnel..."
echo ""

# 3. 测试隧道
echo "尝试连接 tunnel..."
if ssh -o ConnectTimeout=5 -o StrictHostKeyChecking=no "$HOST_ALIAS" "echo 'SSH OK'" 2>/dev/null; then
    echo "  ✅ SSH tunnel 连接成功"
    echo ""
    echo "启动 tunnel: ssh -N -f $HOST_ALIAS"
    echo "测试连接:    curl http://127.0.0.1:$LOCAL_PORT"
    echo ""
    echo "下一步:"
    echo "  1. 重启 Hermes Agent"
    echo "  2. 确认 EDR MCP server 在 Windows 上运行: netstat -an | findstr $REMOTE_PORT"
else
    echo "  ❌ SSH tunnel 连接失败"
    echo ""
    echo "请检查:"
    echo "  - Windows SSH Server 是否运行: Get-Service sshd (Windows)"
    echo "  - 用户名/密码是否正确"
    echo "  - 端口 22 是否放行"
fi
