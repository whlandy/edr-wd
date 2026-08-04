#!/usr/bin/env bash
# edr-wd.sh — Legacy shell facade for the cross-platform EDR-WD agent.
# Prefer the installed `edr-wd` Python CLI for new workflows.
#
# Commands:
#   up       Start the MCP server and prepare connection mode if needed
#   down     Stop the MCP server and stop tunnel if configured
#   status   Show target server and tunnel status when configured
#   push     [debug-only] Copy a file or directory to the Windows target
#   smoke    Run the MCP smoke test against the configured MCP URL
#   repair   Sync-deploy target/ and restart the MCP server (force recovery)
#
# Environment:
#   EDR_WD_TARGET_NAME   Target name from config (default: config.default_target)
#   EDR_WD_LOCAL_PORT    Local tunnel port (default: 18765)
#   EDR_WD_TARGET_DIR    Remote repo path (default: C:/path/to/edr-wd)
#   EDR_WD_START_MODE    Windows start mode: auto|process|scheduled-task (default: auto)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOCAL_PORT="${EDR_WD_LOCAL_PORT:-18765}"
EDR_WD_TARGET_DIR="${EDR_WD_TARGET_DIR:-C:/path/to/edr-wd}"
START_MODE="${EDR_WD_START_MODE:-auto}"
TARGET_NAME="${EDR_WD_TARGET_NAME:-}"
if [ -z "$TARGET_NAME" ]; then
    TARGET_NAME="$({
        cd "$SCRIPT_DIR/.." || exit 1
        python - <<'PY'
from agent.target_config import TargetConfig

name = TargetConfig().get_default_target()
if not name:
    raise SystemExit("default_target is not configured")
print(name)
PY
    })"
fi

usage() {
    cat <<EOF
Usage: bash $0 {up|down|status|push|smoke|repair}

Commands:
  up       Start MCP server and prepare configured connection mode
  down     Stop MCP server and configured tunnel if needed
  status   Show target status and tunnel status when configured
  push     [debug-only] Copy files to the Windows target via Paramiko SFTP
           (raw scp — bypasses deploy tracked-filter. Normal operation
           should use repair or run_tests.py, not push.)
  smoke    Run the MCP smoke test against the configured MCP URL
  repair   Sync-deploy target/ and restart MCP server (force recovery)
EOF
}

target_field() {
    local field="$1"
    (
        cd "$SCRIPT_DIR/.." || exit 1
        python - "$TARGET_NAME" "$field" <<'PY'
import sys
from agent.target_config import TargetConfig

target_name = sys.argv[1]
field = sys.argv[2]
tc = TargetConfig()
cfg = tc.get_target(target_name)
if field == "connect_mode":
    print(cfg.get("mcp", {}).get("connect_mode", "direct"))
elif field == "mcp_url":
    print(tc.build_mcp_url(target_name))
else:
    raise SystemExit(f"unknown field: {field}")
PY
    )
}

local_lifecycle() {
    # Call the Python lifecycle entry points locally on the agent machine.
    # target_manager is an agent-side orchestration module — it must run on
    # the agent host, not on the target.  It connects to the target via SSH
    # internally through ssh_runner.
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
    local mode
    mode="$(target_field connect_mode)"
    if [ "$mode" = "tunnel" ]; then
        bash "$SCRIPT_DIR/tunnel.sh" start "$TARGET_NAME"
    else
        echo "Connection mode is ${mode}; tunnel not required."
    fi
}

do_up() {
    echo "[1/2] Starting MCP server..."
    local_lifecycle start
    echo ""
    echo "[2/2] Preparing connection mode..."
    ensure_tunnel
    echo ""
    echo "Ready:"
    echo "  $(target_field mcp_url)"
}

do_down() {
    local mode
    mode="$(target_field connect_mode)"
    echo "[1/2] Stopping MCP server..."
    local_lifecycle stop || true
    echo ""
    if [ "$mode" = "tunnel" ]; then
        echo "[2/2] Stopping local tunnel..."
        bash "$SCRIPT_DIR/tunnel.sh" stop "$TARGET_NAME" || true
    else
        echo "[2/2] Connection mode is ${mode}; tunnel not used."
    fi
}

do_status() {
    local mode
    mode="$(target_field connect_mode)"
    echo "[1/2] Target status..."
    local_lifecycle status
    echo ""
    if [ "$mode" = "tunnel" ]; then
        echo "[2/2] Tunnel status..."
        bash "$SCRIPT_DIR/tunnel.sh" status "$TARGET_NAME"
    else
        echo "[2/2] Connection mode is ${mode}; MCP URL: $(target_field mcp_url)"
    fi
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
    local mcp_url
    mcp_url="$(target_field mcp_url)"
    python "$SCRIPT_DIR/../target/tests/smoke_mcp_client.py" \
        --base-url "$mcp_url" \
        "$@"
}

do_repair() {
    echo "[1/1] Repairing target — syncing deploy and restarting MCP server..."
    (
        cd "$SCRIPT_DIR/.." || exit 1
        python -c "from agent.target_manager import repair_target; print(repair_target('${TARGET_NAME}', repair=True))"
    )
    echo ""
    echo "Done."
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
    repair)
        do_repair
        ;;
    *)
        usage
        exit 1
        ;;
esac
