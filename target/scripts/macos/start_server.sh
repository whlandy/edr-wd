#!/usr/bin/env bash
#
# start_server.sh — Start the EDR-WD MCP server in the foreground.
#
# Designed to be invoked by macOS launchd (LaunchAgent). launchd will
# see this script's PID and its child's PID (the python server); launchd
# tracks the immediate child, so we exec python at the end.
#
# Lifecycle:
#   - If our own PID file is stale (recorded PID is no longer alive),
#     unlink it and proceed.
#   - If the port is held by a process whose command line looks like
#     our server.py, treat that as "already running" and exit 0 — the
#     matching PID file is refreshed.
#   - If the port is held by something else, log a clear conflict and
#     exit 0. With SuccessfulExit=false, launchd would restart us on non-zero
#     exit, so we exit 0 to avoid a restart/throttle loop. The agent-side
#     MCP initialize/health-check will report the conflict.
#   - Otherwise, write the PID file, then exec the python server.
#
# Environment overrides:
#   EDR_WD_PYTHON              — Python interpreter (default: /opt/homebrew/bin/python3)
#   EDR_WD_MCP_PORT           — MCP HTTP port (default: 8765)
#   EDR_WD_MCP_HOST           — MCP HTTP bind host (default: 0.0.0.0)
#   EDR_WD_AUTOMATION_BACKEND — automation backend (default: macos_accessibility)
#   EDR_WD_TARGET_DIR         — Override target root
#   EDR_WD_PIDFILE            — Override PID file path
#
# Exit codes:
#   0  — server is running (either we just started it, or it was
#        already up via our own PID file / a matching server.py).
#        Also 0 on port conflict, so launchd does not restart-loop.
#   3  — required tool (lsof) missing
#   4  — python interpreter not found

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET_DIR="${EDR_WD_TARGET_DIR:-$(cd "${SCRIPT_DIR}/../.." && pwd)}"

PYTHON="${EDR_WD_PYTHON:-/opt/homebrew/bin/python3}"
PORT="${EDR_WD_MCP_PORT:-8765}"
HOST="${EDR_WD_MCP_HOST:-0.0.0.0}"
AUTOMATION_BACKEND="${EDR_WD_AUTOMATION_BACKEND:-macos_accessibility}"
PIDFILE="${EDR_WD_PIDFILE:-${TARGET_DIR}/logs/server.pid}"

LOG_DIR="${TARGET_DIR}/logs"
SCREENSHOT_DIR="${TARGET_DIR}/screenshots"
mkdir -p "${LOG_DIR}" "${SCREENSHOT_DIR}"

cd "${TARGET_DIR}"

# Refresh-or-clear the PID file. If the recorded PID is no longer
# alive, unlink so a fresh start can write it.
if [[ -f "${PIDFILE}" ]]; then
  existing="$(cat "${PIDFILE}" 2>/dev/null || true)"
  if [[ -n "${existing}" ]] && kill -0 "${existing}" 2>/dev/null; then
    : # PID file is current
  else
    rm -f "${PIDFILE}"
  fi
fi

# Idempotency check: who's listening on the port?
matches_our_server() {
  # Args: <pid> <target_dir> — return 0 only if the process is the
  # managed server for THIS target root. Requires BOTH:
  #   1. command line mentions server.py
  #   2. PID's cwd equals target_dir
  # Using cwd is more reliable than scanning the command line for the
  # target path, because a server started with "cd <root>; python server.py"
  # shows no root path in ps output.
  local pid="$1"
  local target_dir="$2"
  local cmd cwd norm_cmd norm_cwd norm_root
  cmd="$(ps -p "${pid}" -o command= 2>/dev/null || true)"
  norm_cmd="${cmd//\\//}"
  if [[ "${norm_cmd}" != *"server.py"* ]]; then
    return 1
  fi
  # Get cwd via lsof
  cwd="$(lsof -a -p "${pid}" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' | head -n1 || true)"
  norm_cwd="${cwd//\\//}"
  norm_root="${target_dir//\\//}"
  # Normalize trailing slashes for comparison
  norm_cwd="${norm_cwd%/}"
  norm_root="${norm_root%/}"
  [[ "${norm_cwd}" == "${norm_root}" ]]
}

if ! command -v lsof >/dev/null 2>&1; then
  echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) start_server.sh: lsof not found in PATH" \
    >> "${LOG_DIR}/start.log"
  exit 3
fi

if lsof -iTCP:"${PORT}" -sTCP:LISTEN -n -P >/dev/null 2>&1; then
  listener_pid="$(lsof -tiTCP:"${PORT}" -sTCP:LISTEN -n -P | head -n1)"
  if matches_our_server "${listener_pid}" "${TARGET_DIR}"; then
    echo "${listener_pid}" > "${PIDFILE}"
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) start_server.sh: port ${PORT} held by our own server (pid=${listener_pid}); exiting 0" \
      >> "${LOG_DIR}/start.log"
    exit 0
  else
    # Conflict: port held by an unrelated process. Exit 0 intentionally
    # so launchd does not restart-loop (SuccessfulExit=false means non-zero
    # would trigger restart). Agent-side MCP initialize/health-check will
    # detect and report the conflict.
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) start_server.sh: port ${PORT} held by pid=${listener_pid} (NOT our server); exiting 0 to avoid launchd restart loop" \
      >> "${LOG_DIR}/start.log"
    echo "Command line: $(ps -p "${listener_pid}" -o command= 2>/dev/null || echo '<unreadable>')" \
      >> "${LOG_DIR}/start.log"
    exit 0
  fi
fi

# Record our own PID; this is the bash script, not python. The python
# server is exec'd below and inherits nothing (exec replaces the
# process image), so the PID file would become stale. Instead, the
# python server itself is expected to be the immediate child of
# launchd. We record nothing here for the python PID; see python
# server's own pidfile logic.
echo "$$" > "${PIDFILE}"

echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) start_server.sh: launching ${PYTHON} server.py --http --host ${HOST} --port ${PORT}" \
  >> "${LOG_DIR}/start.log"

# exec so launchd sees python as the child. launchd tracks the
# immediate child's lifetime for KeepAlive purposes.
exec env \
  EDR_WD_ENABLE_POWERSHELL=1 EDR_WD_AUTOMATION_BACKEND="${AUTOMATION_BACKEND}" \
  "${PYTHON}" server.py --http --host "${HOST}" --port "${PORT}" \
  >> "${LOG_DIR}/server.log" 2>&1
