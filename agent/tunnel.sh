#!/usr/bin/env bash
# Compatibility wrapper for the Paramiko tunnel manager.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMMAND="${1:-status}"
shift || true

TARGET_ARG=()
if [ "$#" -gt 0 ]; then
    TARGET_ARG=(--target "$1")
elif [ -n "${EDR_WD_TARGET_NAME:-}" ]; then
    TARGET_ARG=(--target "$EDR_WD_TARGET_NAME")
fi

python "$SCRIPT_DIR/tunnel.py" "$COMMAND" "${TARGET_ARG[@]}"
