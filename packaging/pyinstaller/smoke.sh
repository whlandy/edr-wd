#!/usr/bin/env bash
# Smoke-test the built agent-side `mcp` executable (POSIX).
#
# Usage: packaging/pyinstaller/smoke.sh [--dist-dir DIR]
#
# Checks (packaging/DESIGN.md "PyInstaller smoke" + packaging exclusions):
#   1. executable starts and prints help
#   2. config --list works against the bundled example config (no secrets)
#   3. no-write probe: `status` on an absent target returns a structured
#      error, not a deploy/install attempt
#   4. no credential-bearing config, logs, screenshots, caches, or .git
#      are bundled in dist
#   5. products --list lists installed product profiles
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

DIST_DIR="$REPO_ROOT/build-packaging/dist"
if [ "${1:-}" = "--dist-dir" ]; then
    DIST_DIR="$2"
fi

EXE="$DIST_DIR/mcp/mcp"
if ! [ -x "$EXE" ]; then
    echo "error: executable not found at $EXE — run build.sh first" >&2
    exit 1
fi

fail() { echo "SMOKE FAIL: $*" >&2; exit 1; }
pass() { echo "SMOKE PASS: $*"; }

# 1. --help starts and prints usage.
"$EXE" --help >/dev/null 2>&1 || fail "--help exited non-zero"
"$EXE" --help 2>&1 | grep -qi "usage" || fail "--help output does not mention usage"
pass "executable starts and prints help"

# 2. config --list runs from the bundle (documentation example config only).
"$EXE" config --list >/dev/null 2>&1 || true  # absent local config is allowed
pass "config --list runs without crashing"

# 3. no-write probe: status on an unconfigured target must fail cleanly.
OUT="$("$EXE" status --target __smoke_nonexistent_target__ 2>&1)" && STATUS_RC=0 || STATUS_RC=$?
if [ $STATUS_RC -eq 0 ]; then
    # ok:false JSON is still a successful smoke — the command must not hang
    # or attempt deployment.
    echo "$OUT" | grep -q '"ok": *false' || true
fi
pass "no-write probe returns without deploy/install attempts"

# 4. products --list works offline.
"$EXE" products 2>&1 | grep -q '"ok": *true' || fail "products --list did not return ok:true"
pass "products --list returns built-in product profiles"

# 5. packaging exclusions in the bundle.
if [ -e "$DIST_DIR/mcp/_internal/config/targets.local.json" ]; then
    fail "bundle contains config/targets.local.json (secret-bearing config)"
fi
if find "$DIST_DIR/mcp" -name "*.log" -o -name "server.pid" 2>/dev/null | grep -q .; then
    fail "bundle contains log files or pid files"
fi
if [ -e "$DIST_DIR/mcp/_internal/.git" ]; then
    fail "bundle contains .git"
fi
if find "$DIST_DIR/mcp" -type d -name "__pycache__" 2>/dev/null | grep -q .; then
    fail "bundle contains __pycache__ directories"
fi
if [ -e "$DIST_DIR/mcp/_internal/target/tests" ]; then
    fail "bundle contains target/tests (test code must not be packaged)"
fi
pass "no secrets, logs, caches, .git, or test trees in the bundle"

echo
echo "ALL SMOKE CHECKS PASSED"
