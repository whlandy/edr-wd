#!/usr/bin/env bash
#
# install_launch_agent.sh — Register a per-user LaunchAgent that runs the
# EDR-WD MCP server in the current GUI session.
#
# This script is invoked by the Windows/Linux/Mac agent over SSH.
# It assumes the calling user is the same as the user that owns the
# Aqua/GUI session (since the LaunchAgent is bootstrapped into
# gui/$(id -u)).
#
# Arguments (set by agent/lifecycle/macos.py):
#   --label  com.edr-wd.target          (required)
#   --root   /Users/<u>/edr-wd/target   (required)
#   --python /opt/homebrew/bin/python3  (required)
#
# Steps:
#   1. Render the plist template by substituting __LABEL__, __ROOT__,
#      __SCRIPT_PATH__, __STDOUT__, __STDERR__.
#   2. Copy the rendered plist to ~/Library/LaunchAgents/<label>.plist.
#   3. Run `launchctl bootstrap gui/$(id -u)` (idempotent: ignore "already
#      bootstrapped" errors).
#   4. `launchctl enable gui/$(id -u)/<label>` and `launchctl kickstart`
#      so the server starts immediately.

set -euo pipefail

LABEL=""
ROOT=""
PYTHON_PATH=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --label)  LABEL="$2"; shift 2;;
    --root)   ROOT="$2"; shift 2;;
    --python) PYTHON_PATH="$2"; shift 2;;
    *) echo "Unknown arg: $1" >&2; exit 2;;
  esac
done

if [[ -z "${LABEL}" || -z "${ROOT}" || -z "${PYTHON_PATH}" ]]; then
  echo "install_launch_agent.sh: --label, --root, --python are all required" >&2
  exit 2
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATE="${SCRIPT_DIR}/com.edr-wd.target.plist.template"
if [[ ! -f "${TEMPLATE}" ]]; then
  echo "install_launch_agent.sh: template not found at ${TEMPLATE}" >&2
  exit 1
fi

LAUNCH_AGENTS_DIR="${HOME}/Library/LaunchAgents"
mkdir -p "${LAUNCH_AGENTS_DIR}"
RENDERED="${LAUNCH_AGENTS_DIR}/${LABEL}.plist"

# Locate the start_server.sh that we just uploaded via SCP.
START_SCRIPT="${ROOT}/scripts/macos/start_server.sh"
if [[ ! -f "${START_SCRIPT}" ]]; then
  echo "install_launch_agent.sh: start_server.sh missing at ${START_SCRIPT}" >&2
  exit 1
fi

LOG_DIR="${ROOT}/logs"
mkdir -p "${LOG_DIR}"

# Render template. We use sed for substitution because envsubst may not
# be installed on macOS by default. Only the four __TOKEN__ placeholders
# from the template are replaced.
sed \
  -e "s|__LABEL__|${LABEL}|g" \
  -e "s|__ROOT__|${ROOT}|g" \
  -e "s|__SCRIPT_PATH__|${START_SCRIPT}|g" \
  -e "s|__STDOUT__|${LOG_DIR}/launchd.out.log|g" \
  -e "s|__STDERR__|${LOG_DIR}/launchd.err.log|g" \
  -e "s|__WORKDIR__|${ROOT}|g" \
  "${TEMPLATE}" > "${RENDERED}"

echo "install_launch_agent.sh: rendered plist at ${RENDERED}"

UID_VAL="$(id -u)"
GUI_DOMAIN="gui/${UID_VAL}"

# `launchctl bootstrap` errors with code 37 / "already bootstrapped" if
# the agent is already registered. Treat that as success.
set +e
launchctl bootstrap "${GUI_DOMAIN}" "${RENDERED}"
BOOTSTRAP_RC=$?
set -e
if [[ ${BOOTSTRAP_RC} -ne 0 ]]; then
  # Re-load the existing definition so a freshly-uploaded plist takes effect.
  launchctl bootout "${GUI_DOMAIN}/${LABEL}" 2>/dev/null || true
  launchctl bootstrap "${GUI_DOMAIN}" "${RENDERED}"
fi

launchctl enable "${GUI_DOMAIN}/${LABEL}"
launchctl kickstart -k "${GUI_DOMAIN}/${LABEL}"

echo "install_launch_agent.sh: bootstrapped ${GUI_DOMAIN}/${LABEL}"
echo "install_launch_agent.sh: launchctl list | grep ${LABEL}"
launchctl list | grep "${LABEL}" || true
exit 0
