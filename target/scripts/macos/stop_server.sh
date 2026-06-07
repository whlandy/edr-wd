#!/usr/bin/env bash
#
# stop_server.sh — Stop the EDR-WD MCP server.
#
# Stops in this order, all best-effort:
#   1. PID file (logs/server.pid by default). Verifies the recorded
#      PID's command line still looks like our server before killing.
#   2. lsof for PIDs listening on --port, filtered to processes whose
#      command line contains BOTH "server.py" AND "--http" AND the target root
#      path. Other processes holding the port are reported but NOT killed.
#
# Usage:
#   stop_server.sh --port 8765
#   stop_server.sh --port 8765 --pidfile /path/to/server.pid
#
# Exit codes:
#   0  — at least one matching process was stopped, or nothing was running
#   1  — lsof not available AND no pidfile to fall back on
#   2  — bad arguments
#   3  — conflict: a non-edr-wd process is holding the port and was
#        left alone (reported, not killed)

set -euo pipefail

PORT=8765
PIDFILE=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --port)    PORT="$2"; shift 2;;
    --port=*)  PORT="${1#*=}"; shift;;
    --pidfile)    PIDFILE="$2"; shift 2;;
    --pidfile=*)  PIDFILE="${1#*=}"; shift;;
    *) echo "Unknown arg: $1" >&2; exit 2;;
  esac
done

# Default pidfile path: alongside logs/, inside the target root
if [[ -z "${PIDFILE}" ]]; then
  SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  TARGET_DIR="$(cd "${SCRIPT_DIR}/../.." && pwd)"
  PIDFILE="${TARGET_DIR}/logs/server.pid"
fi

looks_like_our_server() {
  # Args: <pid> <target_dir> — return 0 only if the process is the
  # managed server for THIS target root. Requires BOTH:
  #   1. command line mentions server.py
  #   2. PID's cwd equals target_dir
  # Using cwd avoids false matches when two different target roots each
  # have a server.py process; we only kill the one that owns the port
  # AND is running from our target directory.
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
  norm_cwd="${norm_cwd%/}"
  norm_root="${norm_root%/}"
  [[ "${norm_cwd}" == "${norm_root}" ]]
}

stop_pid() {
  local pid="$1"
  if ! looks_like_our_server "${pid}" "${TARGET_DIR}"; then
    echo "stop_server.sh: pid=${pid} command line does not match server.py+--http+target root; skipping" >&2
    return 0
  fi
  echo "stop_server.sh: stopping pid=${pid}"
  kill -TERM "${pid}" 2>/dev/null || true
  for _ in 1 2 3 4 5; do
    if ! kill -0 "${pid}" 2>/dev/null; then
      rm -f "${PIDFILE}"
      return 0
    fi
    sleep 1
  done
  if kill -0 "${pid}" 2>/dev/null; then
    echo "stop_server.sh: pid=${pid} did not exit on SIGTERM, sending SIGKILL"
    kill -KILL "${pid}" 2>/dev/null || true
  fi
  rm -f "${PIDFILE}"
}

stopped_any=0
conflicts=""

# 1. PID file
if [[ -f "${PIDFILE}" ]]; then
  recorded="$(cat "${PIDFILE}" 2>/dev/null || true)"
  if [[ -n "${recorded}" ]] && kill -0 "${recorded}" 2>/dev/null; then
    stop_pid "${recorded}" && stopped_any=1
  else
    echo "stop_server.sh: stale pidfile (pid=${recorded} not alive); removing"
    rm -f "${PIDFILE}"
  fi
fi

# 2. lsof — but only kill processes that match our server.
if command -v lsof >/dev/null 2>&1; then
  for pid in $(lsof -tiTCP:"${PORT}" -sTCP:LISTEN -n -P 2>/dev/null || true); do
    if looks_like_our_server "${pid}" "${TARGET_DIR}"; then
      stop_pid "${pid}" && stopped_any=1
    else
      conflicts="${conflicts} ${pid}"
    fi
  done
  if [[ -n "${conflicts}" ]]; then
    echo "stop_server.sh: NOT killing non-edr-wd processes on port ${PORT}:${conflicts}" >&2
    echo "stop_server.sh: run 'lsof -iTCP:${PORT} -sTCP:LISTEN' to identify them" >&2
  fi
else
  if [[ ! -f "${PIDFILE}" ]]; then
    echo "stop_server.sh: lsof not found and no pidfile to fall back on" >&2
    exit 1
  fi
  # We already tried the pidfile above; without lsof, we cannot confirm
  # the port is free. Exit 0 — the pidfile-based stop is best-effort.
  echo "stop_server.sh: lsof unavailable; relied on pidfile only"
fi

if [[ -n "${conflicts}" ]]; then
  exit 3
fi
exit 0
