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
#   EDR_WD_TARGET_NAME   Target name from config (default: win-dev)
#   EDR_WD_LOCAL_PORT    Local tunnel port (default: 18765)
#   EDR_WD_TARGET_DIR    Remote repo path (default: C:/path/to/edr-wd)
#   EDR_WD_START_MODE    Windows start mode: auto|process|scheduled-task (default: auto)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOCAL_PORT="${EDR_WD_LOCAL_PORT:-18765}"
EDR_WD_TARGET_DIR="${EDR_WD_TARGET_DIR:-C:/path/to/edr-wd}"
START_MODE="${EDR_WD_START_MODE:-auto}"
TARGET_NAME="${EDR_WD_TARGET_NAME:-win-dev}"

usage() {
    cat <<EOF
Usage: bash $0 {up|down|status|push|smoke}

Commands:
  up       Start Windows MCP server and local SSH tunnel
  down     Stop Windows MCP server and local SSH tunnel
  status   Show status for both Windows server and tunnel
  push     Copy files to the Windows target via Paramiko SFTP
  smoke    Run the MCP smoke test against the local tunnel
EOF
}

local_lifecycle() {
    # Call the Python lifecycle entry points locally on the agent machine.
    # target_manager is an agent-side orchestration module — it must run on
    # the agent host, not on the target.  It connects to the target via SSH
    # internally through ssh_runner; the SSH below is only for the tunnel.
    local action="$1"
    local py_cmd

    case "$action" in
        start)
            py_cmd="from agent.target_manager import ensure_server_running; print(ensure_server_running('${TARGET_NAME}'))"
            ;;
        stop)
            py_cmd="from agent.target_manager import stop_server; print(stop_server('${TARGET_NAME}'))"
            ;;
        status)
            py_cmd="from agent.target_manager import probe_target; print(probe_target('${TARGET_NAME}'))"
            ;;
        *)
            echo "Unknown action: $action" >&2
            return 1
            ;;
    esac

    # Run locally on the agent.  We need the repo root on sys.path so the
    # 'agent' package is importable; SCRIPT_DIR is .../edr-wd/agent, so the
    # repo root is one level up.
    cd "$SCRIPT_DIR/.." || return
    python -c "$py_cmd"
}

ensure_tunnel() {
    bash "$SCRIPT_DIR/tunnel.sh" start "$TARGET_NAME"
}

do_up() {
    echo "[1/2] Starting Windows MCP server..."
    local_lifecycle start
    echo ""
    echo "[2/2] Starting local tunnel..."
    ensure_tunnel
    echo ""
    echo "Ready:"
    echo "  http://127.0.0.1:${LOCAL_PORT}/mcp"
}

do_down() {
    echo "[1/2] Stopping Windows MCP server..."
    local_lifecycle stop || true
    echo ""
    echo "[2/2] Stopping local tunnel..."
    bash "$SCRIPT_DIR/tunnel.sh" stop "$TARGET_NAME" || true
}

do_status() {
    echo "[1/2] Windows target status..."
    local_lifecycle status
    echo ""
    echo "[2/2] Tunnel status..."
    bash "$SCRIPT_DIR/tunnel.sh" status "$TARGET_NAME"
}

do_push() {
    local remote_path="${EDR_WD_TARGET_DIR}/incoming/"
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

    echo "Copying ${#sources[@]} item(s) to target ${TARGET_NAME}:$remote_path"
    (
        cd "$SCRIPT_DIR/.." || exit 1
        python - "$TARGET_NAME" "$remote_path" "${sources[@]}" <<'PY'
import sys
from pathlib import Path

from agent.ssh_runner import scp_to
from agent.target_config import TargetConfig

target_name = sys.argv[1]
remote_path = sys.argv[2]
sources = sys.argv[3:]

tc = TargetConfig()
ssh_cfg = tc.resolve_auth(target_name)

failed = []
for source in sources:
    rc, msg = scp_to(ssh_cfg, Path(source), remote_path, timeout=120)
    if rc != 0:
        failed.append(f"{source}: {msg}")
    else:
        print(f"[OK] {source}: {msg}")

if failed:
    for item in failed:
        print(f"[FAIL] {item}", file=sys.stderr)
    raise SystemExit(1)
PY
    )
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
