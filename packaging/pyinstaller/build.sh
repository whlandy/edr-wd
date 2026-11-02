#!/usr/bin/env bash
# Build the agent-side `mcp` executable (POSIX).
#
# Usage: packaging/pyinstaller/build.sh [--clean]
#
# Requirements:
#   - Python with the project's runtime dependencies installed
#     (`pip install -e .` or equivalent at the repo root)
#   - pyinstaller (pip install pyinstaller)
#
# Output: dist/mcp/ (onedir layout; the entry executable is dist/mcp/mcp).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

cd "$REPO_ROOT"

PY="${PYTHON:-python3}"
if ! "$PY" -c "import PyInstaller" >/dev/null 2>&1; then
    echo "error: PyInstaller is not installed. Run: $PY -m pip install pyinstaller" >&2
    exit 1
fi

# --clean clears PyInstaller's cache and temporary files for a fresh build.
EXTRA_ARGS=()
if [ "${1:-}" = "--clean" ]; then
    EXTRA_ARGS+=(--clean)
fi

"$PY" -m PyInstaller --noconfirm "${EXTRA_ARGS[@]}" \
    --distpath "$REPO_ROOT/build-packaging/dist" \
    --workpath "$REPO_ROOT/build-packaging/work" \
    "$SCRIPT_DIR/mcp.spec"

echo
echo "Build finished: $REPO_ROOT/build-packaging/dist/mcp/mcp"
echo "Run the smoke test: $SCRIPT_DIR/smoke.sh"
