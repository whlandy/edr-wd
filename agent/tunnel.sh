#!/bin/bash
# tunnel.sh — EDR-WD SSH Tunnel 管理脚本（参数化版）
#
# 参数化默认值（环境变量优先，命令行参数次之）：
#   EDR_WD_HOST          远程 Windows IP（默认: 170.170.11.26）
#   EDR_WD_USER          远程用户名（默认: admin）
#   EDR_WD_LOCAL_PORT    本地端口（默认: 18765）
#   EDR_WD_REMOTE_PORT   远程端口（默认: 8765）
#   EDR_WD_PASSFILE      密码文件（默认: $HOME/.ssh/.tunnelpass）
#
# 用法:
#   bash tunnel.sh start      # 使用默认值
#   bash tunnel.sh start 170.170.11.26 admin  # 覆盖 IP 和用户
#   bash tunnel.sh stop
#   bash tunnel.sh status
#   bash tunnel.sh test

set -e

EDR_WD_HOST="${EDR_WD_HOST:-${1:-170.170.11.26}}"
EDR_WD_USER="${EDR_WD_USER:-${2:-admin}}"
EDR_WD_LOCAL_PORT="${EDR_WD_LOCAL_PORT:-18765}"
EDR_WD_REMOTE_PORT="${EDR_WD_REMOTE_PORT:-8765}"
EDR_WD_PASSFILE="${EDR_WD_PASSFILE:-$HOME/.ssh/.tunnelpass}"

check_tunnel() {
    if lsof -i :"$EDR_WD_LOCAL_PORT" >/dev/null 2>&1; then
        return 0
    else
        return 1
    fi
}

do_start() {
    if check_tunnel; then
        echo "Tunnel already running on port $EDR_WD_LOCAL_PORT"
        lsof -i :"$EDR_WD_LOCAL_PORT"
        return
    fi

    echo "Starting SSH tunnel..."
    echo "  Host: $EDR_WD_USER@$EDR_WD_HOST"
    echo "  Local port: $EDR_WD_LOCAL_PORT -> Remote port: $EDR_WD_REMOTE_PORT"

    nohup sshpass -f "$EDR_WD_PASSFILE" ssh -N \
        -o StrictHostKeyChecking=no \
        -o ServerAliveInterval=60 \
        "$EDR_WD_USER@$EDR_WD_HOST" \
        -L"${EDR_WD_LOCAL_PORT}:127.0.0.1:${EDR_WD_REMOTE_PORT}" \
        > /dev/null 2>&1 &

    sleep 2

    if check_tunnel; then
        echo "Tunnel started successfully on port $EDR_WD_LOCAL_PORT"
    else
        echo "Tunnel failed to start. Check: sshpass -f $EDR_WD_PASSFILE ssh -N $EDR_WD_USER@$EDR_WD_HOST -L..."
        exit 1
    fi
}

do_stop() {
    echo "Stopping SSH tunnel..."
    # Match by the -L<port>:127.0.0.1:<port> pattern to avoid killing unrelated ssh processes
    pkill -f "-L${EDR_WD_LOCAL_PORT}:127.0.0.1:${EDR_WD_REMOTE_PORT}" 2>/dev/null || true
    sleep 1

    if check_tunnel; then
        echo "Tunnel still running, force killing..."
        pkill -9 -f "-L${EDR_WD_LOCAL_PORT}:127.0.0.1:${EDR_WD_REMOTE_PORT}" 2>/dev/null || true
        sleep 1
    fi

    if check_tunnel; then
        echo "Failed to stop tunnel"
        exit 1
    else
        echo "Tunnel stopped"
    fi
}

do_status() {
    if check_tunnel; then
        echo "Tunnel running on port $EDR_WD_LOCAL_PORT"
        lsof -i :"$EDR_WD_LOCAL_PORT"
    else
        echo "Tunnel not running"
    fi
}

do_test() {
    HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" \
        --max-time 5 \
        "http://127.0.0.1:$EDR_WD_LOCAL_PORT/mcp" 2>/dev/null || echo "000")

    if [ "$HTTP_CODE" = "404" ] || [ "$HTTP_CODE" = "406" ]; then
        echo "MCP server responding (HTTP $HTTP_CODE)"
    elif [ "$HTTP_CODE" = "000" ]; then
        echo "Cannot connect to port $EDR_WD_LOCAL_PORT. Check:"
        echo "  1. Tunnel running: bash $0 status"
        echo "  2. Windows MCP server running on port $EDR_WD_REMOTE_PORT"
    else
        echo "Unexpected HTTP $HTTP_CODE"
    fi
}

case "${1:-status}" in
    start)  do_start ;;
    stop)   do_stop ;;
    status) do_status ;;
    test)   do_test ;;
    *)      echo "Usage: bash $0 {start|stop|status|test}" ;;
esac
