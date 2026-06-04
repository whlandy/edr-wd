#!/usr/bin/env bash
#
# stop_server.sh — Stop the EDR-WD MCP server by port.
#
# Usage:  stop_server.sh --port 8765
#
# Behavior:
#   - Find PIDs listening on the given port via lsof.
#   - Send SIGTERM, wait up to 5 s, then SIGKILL.
#   - Exit 0 whether or not anything was running (idempotent).
#   - Does NOT touch the LaunchAgent — use launchctl bootout for that.

set -euo pipefail

PORT=8765
while [[ $# -gt 0 ]]; do
  case "$1" in
    --port)
      PORT="$2"; shift 2;;
    --port=*)
      PORT="${1#*=}"; shift;;
    *)
      echo "Unknown arg: $1" >&2; exit 2;;
  esac
done

if ! command -v lsof >/dev/null 2>&1; then
  echo "stop_server.sh: lsof not found; aborting" >&2
  exit 1
fi

PIDS="$(lsof -tiTCP:"${PORT}" -sTCP:LISTEN -n -P 2>/dev/null || true)"
if [[ -z "${PIDS}" ]]; then
  echo "stop_server.sh: nothing listening on port ${PORT}"
  exit 0
fi

echo "stop_server.sh: stopping PIDs ${PIDS} on port ${PORT}"
for pid in ${PIDS}; do
  kill -TERM "${pid}" 2>/dev/null || true
done

# Wait up to 5 s for graceful shutdown
for _ in 1 2 3 4 5; do
  REMAINING="$(lsof -tiTCP:"${PORT}" -sTCP:LISTEN -n -P 2>/dev/null || true)"
  if [[ -z "${REMAINING}" ]]; then
    echo "stop_server.sh: stopped"
    exit 0
  fi
  sleep 1
done

# Force-kill stragglers
REMAINING="$(lsof -tiTCP:"${PORT}" -sTCP:LISTEN -n -P 2>/dev/null || true)"
if [[ -n "${REMAINING}" ]]; then
  echo "stop_server.sh: force-killing ${REMAINING}"
  for pid in ${REMAINING}; do
    kill -KILL "${pid}" 2>/dev/null || true
  done
fi
echo "stop_server.sh: stopped"
exit 0
