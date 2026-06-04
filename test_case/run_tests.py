#!/usr/bin/env python3
"""
run_tests.py — Profile-aware test runner (no pytest required)

Usage:
  python run_tests.py                                # use default_target, dispatch by app_profile
  python run_tests.py -v                             # verbose output
  python run_tests.py --target win-dev               # specific target
  EDR_WD_TARGET=mac-dev python run_tests.py          # macOS target
  python run_tests.py --profile macos_generic        # force a profile (overrides target.app_profile)
  python run_tests.py --legacy                       # bypass profile dispatch (raw Windows E2E)

The runner now dispatches to a per-profile test suite:
  - profile=windows_hisec  → test_case.run_windows_hisec (legacy 16/16 E2E)
  - profile=macos_generic  → test_case.run_macos_generic (v1 minimal capability set)

For platforms/profiles without a registered runner, the runner aborts
with a clear error rather than silently running the wrong tests.
"""

import sys
import os
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from test_case.conftest import (
    McpClient,
    ensure_server_running,
    get_target_name,
    mcp_initialize,
)
from test_case.test_profiles import resolve_runner, PROFILE_RUNNERS

# Target manager for health checks
from agent import target_manager
from agent.target_config import TargetConfig


def _resolve_profile(target_name: str, override: str | None) -> str:
    """
    Decide which test profile to run.

    Priority: --profile override > target.app_profile > "windows_hisec"
    (default for legacy behaviour, since the only target without an
    explicit app_profile in the wild is the original win-dev).
    """
    if override:
        return override
    tc = TargetConfig()
    try:
        profile = tc.get_target_app_profile(target_name)
    except Exception:
        profile = None
    return profile or "windows_hisec"


def run_tests(verbose: bool = False, target: str | None = None,
              profile_override: str | None = None) -> bool:
    # ── Resolve target ─────────────────────────────────────────────
    target_name = target or os.environ.get("EDR_WD_TARGET") or get_target_name()

    # ── Resolve profile ────────────────────────────────────────────
    profile = _resolve_profile(target_name, profile_override)
    runner = resolve_runner(profile)
    if runner is None:
        print(f"[FAIL] No test runner registered for profile={profile!r}.")
        print(f"       Registered profiles: {sorted(PROFILE_RUNNERS)}")
        print("       To force the Windows legacy E2E suite: --profile windows_hisec")
        return False

    # ── Print header ───────────────────────────────────────────────
    print("=" * 60)
    print(f"EDR-WD Test Runner")
    print("=" * 60)
    print(f"  Target:  {target_name}")
    print(f"  Profile: {profile}")
    print(f"  Runner:  {runner.__module__}.{runner.__name__}")
    print()

    # ── Ensure MCP server is running on target ─────────────────────
    print("=" * 60)
    print("Starting MCP Server")
    print("=" * 60)
    server_ok, srv_msg = ensure_server_running(target_name)
    print(f"  MCP Server: {'[OK]' if server_ok else '[FAIL]'} {srv_msg}")
    if not server_ok:
        print("[FAIL] Could not start MCP server on target.")
        return False

    # ── MCP initialize ─────────────────────────────────────────────
    print()
    print("=" * 60)
    print("Environment Check")
    print("=" * 60)

    health = target_manager.check_server_health(target_name)
    mcp_url = health.get("data", {}).get("mcp_url", "unknown")
    print(f"  Target:     {target_name}")
    print(f"  MCP URL:    {mcp_url}")
    print(f"  MCP Server: [{'OK' if server_ok else 'FAIL'}] {srv_msg}")
    print()

    try:
        init_result = mcp_initialize(target_name)
        if not init_result["ok"]:
            print(f"[FAIL] MCP initialize failed: {init_result.get('error')}")
            return False

        session_id = init_result["data"]["session_id"]
        print(f"[OK] MCP session: {session_id}")
        print()

        client = McpClient(mcp_init_result=init_result)

    except Exception as e:
        print(f"[FAIL] initialize exception: {e}")
        return False

    # ── Dispatch to per-profile test suite ─────────────────────────
    try:
        passed, failed, errors, ok = runner(client, verbose=verbose)
    finally:
        client.close()

    # ── Summary ───────────────────────────────────────────────────
    print()
    print("=" * 60)
    print(f"Results: {passed} passed, {failed} failed")
    if errors:
        print(f"Failed: {', '.join(errors)}")
    print("=" * 60)
    return ok


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="EDR-WD profile-aware test runner")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--target", help="Target name (overrides EDR_WD_TARGET and default_target)")
    parser.add_argument("--profile",
                        help="Force a test profile (overrides target.app_profile). "
                             f"Choices: {sorted(PROFILE_RUNNERS)}")
    parser.add_argument("--legacy", action="store_true",
                        help="Shorthand for --profile windows_hisec")
    args = parser.parse_args()

    profile = "windows_hisec" if args.legacy else args.profile
    ok = run_tests(verbose=args.verbose, target=args.target, profile_override=profile)
    sys.exit(0 if ok else 1)
