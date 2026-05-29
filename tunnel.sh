#!/bin/bash
# tunnel.sh — EDR-WD SSH Tunnel 管理脚本
# 用法:
#   bash tunnel.sh start   # 启动隧道
#   bash tunnel.sh status  # 查看状态
#   bash tunnel.sh stop    # 停止隧道
#   bash tunnel.sh test    # 测试连接

HOST_ALIAS="edr-win"
LOCAL_PORT=18765

check_tunnel() {
    if lsof -i :$LOCAL_PORT >/dev/null 2>&1; then
        return 0
    else
        return 1
    fi
}

case "${1:-status}" in
    start)
        if check_tunnel; then
            echo "✅ Tunnel 已在运行 (端口 $LOCAL_PORT)"
        else
            echo "启动 SSH tunnel..."
            ssh -N -f "$HOST_ALIAS"
            sleep 1
            if check_tunnel; then
                echo "✅ Tunnel 启动成功 (端口 $LOCAL_PORT)"
            else
                echo "❌ Tunnel 启动失败"
                echo "检查: ssh -N -f $HOST_ALIAS"
            fi
        fi
        ;;
    stop)
        echo "停止 SSH tunnel..."
        pkill -f "ssh -N -f $HOST_ALIAS" 2>/dev/null
        sleep 1
        if check_tunnel; then
            echo "❌ Tunnel 仍在运行"
        else
            echo "✅ Tunnel 已停止"
        fi
        ;;
    status)
        if check_tunnel; then
            echo "✅ Tunnel 运行中 (端口 $LOCAL_PORT)"
            lsof -i :$LOCAL_PORT
        else
            echo "⬜ Tunnel 未运行"
        fi
        ;;
    test)
        echo "测试 MCP server 连接..."
        HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:$LOCAL_PORT 2>/dev/null || echo "000")
        if [ "$HTTP_CODE" = "404" ]; then
            echo "✅ MCP server 正常 (HTTP $HTTP_CODE — MCP 协议响应)"
        elif [ "$HTTP_CODE" = "000" ]; then
            echo "❌ 无法连接。请确认:"
            echo "  - Tunnel 已启动: bash $0 start"
            echo "  - Windows MCP server 正在运行"
        else
            echo "⚠️  意外响应 (HTTP $HTTP_CODE)"
        fi
        ;;
    *)
        echo "用法: bash $0 {start|stop|status|test}"
        ;;
esac
