#!/usr/bin/env bash
# edr-wd.sh — Agent side control plane for Windows EDR-WD target.
#
# Commands:
#   up       Start the MCP server on the Windows target and ensure tunnel is up
#   down     Stop the MCP server on the Windows target and stop tunnel
#   status   Show Windows server and tunnel status
#   push     Copy a file or directory to the Windows target
#   smoke    Run the MCP smoke test against the local tunnel
#
# Environment:
#   EDR_WD_HOST          Windows host/IP (default: 170.170.11.26)
#   EDR_WD_USER          Windows username (default: admin)
#   EDR_WD_SSH_PORT      SSH port (default: 22)
#   EDR_WD_LOCAL_PORT    Local tunnel port (default: 18765)
#   EDR_WD_REMOTE_PORT   Windows MCP port (default: 8765)
#   EDR_WD_TARGET_DIR    Remote repo path (default: C:/path/to/edr-wd)
#   EDR_WD_PASSFILE      Password file for tunnel.sh (default: $HOME/.ssh/.tunnelpass)
#   EDR_WD_START_MODE    Windows start mode: auto|process|scheduled-task (default: auto)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOST="${EDR_WD_HOST:-170.170.11.26}"
USER="${EDR_WD_USER:-admin}"
SSH_PORT="${EDR_WD_SSH_PORT:-22}"
LOCAL_PORT="${EDR_WD_LOCAL_PORT:-18765}"
REMOTE_PORT="${EDR_WD_REMOTE_PORT:-8765}"
TARGET_DIR="${EDR_WD_TARGET_DIR:-C:/path/to/edr-wd}"
PASSFILE="${EDR_WD_PASSFILE:-$HOME/.ssh/.tunnelpass}"
START_MODE="${EDR_WD_START_MODE:-auto}"

usage() {
    cat <<EOF
Usage: bash $0 {up|down|status|push|smoke}

Commands:
  up       Start Windows MCP server and local SSH tunnel
  down     Stop Windows MCP server and local SSH tunnel
  status   Show status for both Windows server and tunnel
  push     Copy files to the Windows target via scp
  smoke    Run the MCP smoke test against the local tunnel
EOF
}

remote_deploy() {
    local action="$1"
    local remote_script="$TARGET_DIR/target/deploy.ps1"
    local ssh_args=(-p "$SSH_PORT" -o StrictHostKeyChecking=no)
    local remote_cmd="powershell -NoProfile -ExecutionPolicy Bypass -File \"$remote_script\" -Action $action -BindHost 127.0.0.1 -Port $REMOTE_PORT -StartMode $START_MODE"

    if command -v sshpass >/dev/null 2>&1 && [ -f "$PASSFILE" ]; then
        sshpass -f "$PASSFILE" ssh "${ssh_args[@]}" "${USER}@${HOST}" "$remote_cmd"
    else
        ssh "${ssh_args[@]}" -o BatchMode=yes "${USER}@${HOST}" "$remote_cmd"
    fi
}

ensure_tunnel() {
    bash "$SCRIPT_DIR/tunnel.sh" start "$HOST" "$USER"
}

do_up() {
    echo "[1/2] Starting Windows MCP server..."
    remote_deploy start
    echo ""
    echo "[2/2] Starting local tunnel..."
    ensure_tunnel
    echo ""
    echo "Ready:"
    echo "  http://127.0.0.1:${LOCAL_PORT}/mcp"
}

do_down() {
    echo "[1/2] Stopping Windows MCP server..."
    remote_deploy stop || true
    echo ""
    echo "[2/2] Stopping local tunnel..."
    bash "$SCRIPT_DIR/tunnel.sh" stop || true
}

do_status() {
    echo "[1/2] Windows target status..."
    remote_deploy status
    echo ""
    echo "[2/2] Tunnel status..."
    bash "$SCRIPT_DIR/tunnel.sh" status
}

do_push() {
    local remote_path="$TARGET_DIR/incoming/"
    local sources=()

    while [ "$#" -gt 0 ]; do
        case "$1" in
            --to=*)
                remote_path="${1#--to=}"
                ;;
            --to)
                shift
                remote_path="${1:-$remote_path}"
                ;;
            *)
                sources+=("$1")
                ;;
        esac
        shift || true
    done

    if [ "${#sources[@]}" -eq 0 ]; then
        echo "push requires at least one source path"
        exit 1
    fi

    echo "Copying to ${USER}@${HOST}:$remote_path"
    scp -P "$SSH_PORT" -o StrictHostKeyChecking=no "${sources[@]}" "${USER}@${HOST}:$remote_path"
}

do_smoke() {
    python "$SCRIPT_DIR/../target/tests/smoke_mcp_client.py" \
        --base-url "http://127.0.0.1:${LOCAL_PORT}/mcp" \
        "$@"
}

case "${1:-}" in
    up)
        do_up
        ;;
    down)
        do_down
        ;;
    status)
        do_status
        ;;
    push)
        shift
        do_push "$@"
        ;;
    smoke)
        shift
        do_smoke "$@"
        ;;
    *)
        usage
        exit 1
        ;;
esac
