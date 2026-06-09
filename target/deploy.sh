#!/usr/bin/env bash
#
# deploy.sh — macOS unified deployment entry point for EDR-WD MCP server.
#
# Mirrors target/deploy.ps1 (Windows) in interface and behaviour.
# Does NOT install Python dependencies; fastmcp must be set up separately
# (e.g. via `pip install -e .` in the project venv).
#
# Usage:
#   deploy.sh --action <ACTION> [--target-root <PATH>] [--port <PORT>] [--python <PYTHON>]
#
# Actions:
#   guide      — print usage
#   install    — register LaunchAgent
#   start      — start MCP server
#   stop       — stop MCP server
#   health     — three-layer health check
#   status     — alias for health
#   restart    — stop + start + health
#   bootstrap  — install + start + health
#
# Exit codes:
#   0  — success
#   1  — missing arguments
#   2  — unknown action
#   3  — dependency check failed (fastmcp missing)
#   4  — LaunchAgent not registered (install first)
#   5  — server not reachable
#   6  — port conflict (non-edr-wd process on port)

set -euo pipefail

# ── Defaults ──────────────────────────────────────────────────────────────────

ACTION=""
TARGET_ROOT=""
PORT="8765"
PYTHON_PATH=""

# ── Parse arguments ─────────────────────────────────────────────────────────

while [[ $# -gt 0 ]]; do
  case "$1" in
    --action)      ACTION="$2";      shift 2;;
    --target-root)  TARGET_ROOT="$2"; shift 2;;
    --port)        PORT="$2";         shift 2;;
    --python)      PYTHON_PATH="$2";  shift 2;;
    --action=*)    ACTION="${1#*=}"; shift;;
    --target-root=*) TARGET_ROOT="${1#*=}"; shift;;
    --port=*)      PORT="${1#*=}";    shift;;
    --python=*)    PYTHON_PATH="${1#*=}"; shift;;
    -h|--help)     ACTION="guide";   shift;;
    *) echo "Unknown argument: $1" >&2; exit 1;;
  esac
done

# ── Derive target root ──────────────────────────────────────────────────────

if [[ -z "${TARGET_ROOT}" ]]; then
  SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  TARGET_ROOT="${SCRIPT_DIR}"
else
  TARGET_ROOT="$(cd "${TARGET_ROOT}" && pwd)"
fi

if [[ ! -d "${TARGET_ROOT}" ]]; then
  err "target root does not exist"
  exit 1
fi

# Normalize PYTHON_PATH to absolute path if provided as relative.
if [[ -n "${PYTHON_PATH}" ]]; then
  if [[ "${PYTHON_PATH}" != /* ]]; then
    PYTHON_PATH="$(cd "$(dirname "${PYTHON_PATH}")" && pwd)/$(basename "${PYTHON_PATH}")"
  fi
fi

SCRIPTS_DIR="${TARGET_ROOT}/scripts/macos"
LABEL="com.edr-wd.target"
LOG_DIR="${TARGET_ROOT}/logs"

# ── Helpers ────────────────────────────────────────────────────────────────

log()  { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*"; }
err()  { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] ERROR: $*" >&2; }

# Check fastmcp is importable.  Exit 3 if missing.
check_fastmcp() {
  local python="${PYTHON_PATH:-/opt/homebrew/bin/python3}"
  if ! "${python}" -c 'import fastmcp' 2>/dev/null; then
    err "fastmcp is not installed in selected Python runtime"
    echo ""
    echo "  Next step: run 'pip install -e .' in your project venv"
    echo "  Then re-run deploy.sh"
    return 3
  fi
  return 0
}

# Check LaunchAgent is registered in launchd.
check_launchagent_registered() {
  local uid
  uid="$(id -u)"
  if launchctl list "${LABEL}" >/dev/null 2>&1; then
    return 0
  fi
  return 1
}

# Check server process is running (launchd reports Running=true).
check_launchagent_running() {
  local uid
  uid="$(id -u)"
  local status
  status="$(launchctl list "${LABEL}" 2>/dev/null | awk '{print $1}' | head -n1 || true)"
  [[ "${status}" == "-" ]] && return 1
  return 0
}

# Check port is in LISTEN state.
check_port_listening() {
  if lsof -iTCP:"${PORT}" -sTCP:LISTEN -n -P -q >/dev/null 2>&1; then
    return 0
  fi
  return 1
}

# Check MCP HTTP responds to initialize.
check_mcp_responding() {
  local python="${PYTHON_PATH:-/opt/homebrew/bin/python3}"
  local url="http://127.0.0.1:${PORT}/mcp"
  local body
  body="$(
    curl -s --max-time 5 -X POST "${url}" \
      -H "Content-Type: application/json" \
      -H "Accept: application/json, text/event-stream" \
      -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"edr-wd-deploy","version":"1.0"}}}' \
      2>/dev/null || true
  )"
  if [[ -z "${body}" ]]; then
    return 1
  fi
  # Parse JSON (plain or SSE-wrapped) with Python to verify result.protocolVersion exists.
  printf '%s' "${body}" | "${python}" -c '
import json, sys

raw = sys.stdin.read().strip()

# Strip SSE data: prefix if present
if raw.startswith("data:") or "\ndata:" in raw:
    lines = []
    for line in raw.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            lines.append(line[5:].strip())
    raw = "\n".join(lines).strip()

data = json.loads(raw)
result = data.get("result") or {}
if not result.get("protocolVersion"):
    raise SystemExit(1)
' 2>/dev/null
}

# ── Actions ─────────────────────────────────────────────────────────────────

do_guide() {
  echo "EDR-WD target deploy (macOS)"
  echo ""
  echo "Usage: $0 --action <ACTION> [OPTIONS]"
  echo ""
  echo "Actions:"
  echo "  guide     — print this message"
  echo "  bootstrap — install LaunchAgent, start server, run health check"
  echo "  install   — register LaunchAgent (does not start server)"
  echo "  start     — start MCP server"
  echo "  stop      — stop MCP server"
  echo "  health    — three-layer health check"
  echo "  status    — alias for health"
  echo "  restart   — stop + start + health"
  echo ""
  echo "Options:"
  echo "  --target-root <PATH>   Target root (default: auto-detected)"
  echo "  --port <PORT>          MCP port (default: 8765)"
  echo "  --python <PYTHON>      Python interpreter (default: /opt/homebrew/bin/python3)"
  echo ""
  echo "Typical workflow:"
  echo "  1. Ensure project dependencies are installed:"
  echo "       pip install -e ."
  echo "  2. Register the LaunchAgent:"
  echo "       $0 --action install"
  echo "  3. Start the server:"
  echo "       $0 --action start"
  echo "  4. Check health:"
  echo "       $0 --action health"
  echo ""
  echo "Exit codes:"
  echo "  0 — success"
  echo "  1 — missing arguments"
  echo "  2 — unknown action"
  echo "  3 — fastmcp not installed (run: pip install -e .)"
  echo "  4 — LaunchAgent not registered (run: --action install first)"
  echo "  5 — server not reachable"
  echo "  6 — port conflict (non-edr-wd process holding the port)"
  exit 0
}

do_install() {
  log "install: registering LaunchAgent"
  local install_script="${SCRIPTS_DIR}/install_launch_agent.sh"
  if [[ ! -f "${install_script}" ]]; then
    err "install script not found at <TARGET_ROOT>/scripts/macos/"
    exit 4
  fi
  local python="${PYTHON_PATH:-/opt/homebrew/bin/python3}"
  bash "${install_script}" \
    --label "${LABEL}" \
    --root "${TARGET_ROOT}" \
    --python "${python}" \
    2>&1 | while IFS= read -r line; do log "install: ${line}"; done
  local rc=${PIPESTATUS[0]}
  if [[ ${rc} -ne 0 ]]; then
    err "install failed (exit ${rc})"
    exit 4
  fi
  log "install: done"
  exit 0
}

do_start() {
  # start_server.sh is a launchd-compatible foreground script; it must not
  # be called directly.  Instead, control the LaunchAgent via launchctl.

  log "start: checking dependencies"
  check_fastmcp || exit 3

  # Ensure LaunchAgent is registered.
  if ! check_launchagent_registered; then
    err "LaunchAgent not registered"
    echo ""
    echo "  Next step: deploy.sh --action install"
    exit 4
  fi

  # Start/restart the LaunchAgent.  kickstart -k = kill + restart atomically.
  log "start: starting LaunchAgent via launchctl"
  if ! launchctl kickstart -k "gui/$(id -u)/${LABEL}" 2>/dev/null; then
    err "start: failed to kickstart LaunchAgent"
    echo ""
    echo "  Next step: deploy.sh --action health for diagnostics"
    exit 5
  fi

  # Wait for port + MCP to come up (up to 15 seconds).
  log "start: waiting for server to become ready (port ${PORT})"
  local deadline=$(( $(date +%s) + 15 ))
  local ready=false
  while [[ $(date +%s) -lt ${deadline} ]]; do
    if check_port_listening && check_mcp_responding; then
      ready=true
      break
    fi
    sleep 1
  done

  if [[ "${ready}" == "true" ]]; then
    log "start: server is ready"
    exit 0
  else
    err "start: server did not become ready within 15 seconds"
    echo ""
    echo "  Next step: deploy.sh --action health for diagnostics"
    exit 5
  fi
}

do_stop() {
  log "stop: stopping server via stop_server.sh"
  local stop_script="${SCRIPTS_DIR}/stop_server.sh"
  if [[ ! -f "${stop_script}" ]]; then
    err "stop script not found at <TARGET_ROOT>/scripts/macos/"
    exit 1
  fi
  bash "${stop_script}" --port "${PORT}" \
    2>&1 | while IFS= read -r line; do log "stop: ${line}"; done
  log "stop: done"
  exit 0
}

do_health() {
  log "health: starting three-layer check on port ${PORT}"

  # Layer 1: LaunchAgent registered?
  local layer1="FAIL"
  if check_launchagent_registered; then
    layer1="PASS"
    log "health: layer1 LaunchAgent registered — ${LABEL}"
  else
    log "health: layer1 LaunchAgent NOT registered — run --action install first"
  fi

  # Layer 2: port listening?
  local layer2="FAIL"
  if check_port_listening; then
    layer2="PASS"
    log "health: layer2 port ${PORT} listening"
  else
    log "health: layer2 port ${PORT} NOT listening"
  fi

  # Layer 3: MCP HTTP responding?
  local layer3="FAIL"
  if check_mcp_responding; then
    layer3="PASS"
    log "health: layer3 MCP /mcp responds to initialize"
  else
    log "health: layer3 MCP /mcp NOT responding"
  fi

  echo ""
  if [[ "${layer1}" == "PASS" && "${layer2}" == "PASS" && "${layer3}" == "PASS" ]]; then
    log "health: ALL PASS — server is healthy"
    exit 0
  else
    err "health: FAILED (L1=${layer1} L2=${layer2} L3=${layer3})"
    [[ "${layer1}" == "FAIL" ]] && echo "  L1: LaunchAgent not registered — run: $0 --action install"
    [[ "${layer2}" == "FAIL" ]] && echo "  L2: port ${PORT} not listening — run: $0 --action start"
    [[ "${layer3}" == "FAIL" ]] && echo "  L3: MCP not responding — fastmcp may be missing"
    [[ "${layer3}" == "FAIL" ]] && echo "  Tip: ensure project dependencies installed: pip install -e ."
    exit 5
  fi
}

do_restart() {
  do_stop || true
  do_start || { err "restart: start failed"; exit 5; }
  do_health
}

do_bootstrap() {
  log "bootstrap: starting"
  do_install || { err "bootstrap: install failed"; exit 4; }
  do_start  || { err "bootstrap: start failed";  exit 5; }
  do_health || { err "bootstrap: health failed"; exit 5; }
  log "bootstrap: complete"
  exit 0
}

# ── Dispatch ───────────────────────────────────────────────────────────────

case "${ACTION}" in
  guide)     do_guide;;
  install)   do_install;;
  start)     do_start;;
  stop)      do_stop;;
  health)    do_health;;
  status)    do_health;;
  restart)   do_restart;;
  bootstrap) do_bootstrap;;
  "")        echo "Error: --action is required. Use --action guide for usage." >&2; exit 1;;
  *)         echo "Error: unknown action '${ACTION}'. Use --action guide." >&2; exit 2;;
esac
