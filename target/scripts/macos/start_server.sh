#!/usr/bin/env bash
#
# start_server.sh — Start the EDR-WD MCP server in the foreground.
#
# Designed to be invoked by macOS launchd (LaunchAgent) — keep the parent
# process alive so launchd can supervise it. Logs are appended; pid is
# recorded so stop_server.sh can clean up.
#
# Environment overrides:
#   EDR_WD_PYTHON    — Python interpreter (default: $EDR_WD_PYTHON_DEFAULT
#                      or /opt/homebrew/bin/python3)
#   EDR_WD_MCP_PORT  — MCP HTTP port (default: 8765)
#   EDR_WD_MCP_HOST  — MCP HTTP bind host (default: 0.0.0.0)
#   EDR_WD_TARGET_DIR — Override target root (default: script's grandparent)
#
# This script is idempotent: if the port is already in use, it exits 0
# without launching a second instance.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET_DIR="${EDR_WD_TARGET_DIR:-$(cd "${SCRIPT_DIR}/../.." && pwd)}"

PYTHON="${EDR_WD_PYTHON:-${EDR_WD_PYTHON_DEFAULT:-/opt/homebrew/bin/python3}}"
PORT="${EDR_WD_MCP_PORT:-8765}"
HOST="${EDR_WD_MCP_HOST:-0.0.0.0}"

LOG_DIR="${TARGET_DIR}/logs"
SCREENSHOT_DIR="${TARGET_DIR}/screenshots"
mkdir -p "${LOG_DIR}" "${SCREENSHOT_DIR}"

cd "${TARGET_DIR}"

# Idempotent: if something is already listening on $PORT, do nothing.
if command -v lsof >/dev/null 2>&1; then
  if lsof -iTCP:"${PORT}" -sTCP:LISTEN -n -P >/dev/null 2>&1; then
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) start_server.sh: port ${PORT} already in use, exiting" \
      >> "${LOG_DIR}/start.log"
    exit 0
  fi
fi

echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) start_server.sh: launching ${PYTHON} server.py --http --host ${HOST} --port ${PORT}" \
  >> "${LOG_DIR}/start.log"

# exec so launchd sees the actual python process as the child.
exec "${PYTHON}" server.py --http --host "${HOST}" --port "${PORT}" \
  >> "${LOG_DIR}/server.log" 2>&1
