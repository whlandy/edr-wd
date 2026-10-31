#!/usr/bin/env bash
#
# start_server_local.sh — LaunchAgent wrapper: run the repo's MCP server
# on this machine (127.0.0.1:8766) with the repo venv.
#
# The shared target/scripts/macos/start_server.sh stays untouched; this
# wrapper only pins the environment for the local deployment and execs it.
#
set -euo pipefail

REPO_DIR="/Users/edr-test/ai-projects/edr-wd"

export EDR_WD_PYTHON="${REPO_DIR}/.venv/bin/python"
export EDR_WD_MCP_PORT=8766
export EDR_WD_MCP_HOST=127.0.0.1
export EDR_WD_AUTOMATION_BACKEND=macos_accessibility
export EDR_WD_TARGET_DIR="${REPO_DIR}/target"

exec "${REPO_DIR}/target/scripts/macos/start_server.sh"
