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


# Per-platform default profile. The legacy win-dev has no app_profile
# in its config; defaulting it to windows_hisec keeps the 16/16
# behaviour bit-identical to the pre-M6 run_tests.py.
#
# The critical safety rule: a macos target without an app_profile
# MUST NOT fall through to windows_hisec. Doing so would route the
# macOS target into activate_edr / HisecEndpointAgent / EDRClient —
# a Windows-only workflow that has no meaning on macOS and would
# produce misleading "test ran but everything failed" output.
#
# The macos default is macos_generic, which runs the v1 capability
# set (screenshot / list_windows / activate_app / is_window_open /
# click_at plumbing / connect) and never touches HiSec EDR.
DEFAULT_PROFILE_BY_PLATFORM: dict[str, str] = {
    "windows": "windows_hisec",
    "macos":   "macos_generic",
}

DEFAULT_PROFILE_BY_BACKEND: dict[str, str] = {
    "windows_pywinauto": "windows_hisec",
    "uia": "windows_hisec",
    "win32": "windows_hisec",
    "macos_accessibility": "macos_generic",
}

PROFILE_BACKENDS: dict[str, set[str]] = {
    "windows_hisec": {"windows_pywinauto", "uia", "win32"},
    "macos_generic": {"macos_accessibility"},
    "macos_hisec": {"macos_accessibility"},
}


def _profile_backend_conflict(profile: str, backend: str | None) -> bool:
    if not backend:
        return False
    expected = PROFILE_BACKENDS.get(profile)
    return expected is not None and backend not in expected


def _resolve_profile(target_name: str, override: str | None) -> str:
    """
    Decide which test profile to run.

    Priority:
      1. --profile CLI override
      2. target.app_profile (explicit in config)
      3. per-platform default (windows -> windows_hisec, macos -> macos_generic)
      4. fail with a clear error (no silent fallback to a wrong-platform runner)

    A target with platform=macos and no app_profile resolves to
    macos_generic, NEVER to windows_hisec.
    """
    if override:
        return override
    tc = TargetConfig()
    # Pull platform + app_profile directly from the in-memory data so we
    # never depend on get_target_platform() (which raises KeyError when
    # the target name is not in the active config — e.g. when the only
    # declared target is win-dev but the operator passes --target mac-dev).
    tc = TargetConfig()
    targets = tc.list_targets()
    raw = targets.get(target_name, {}) if isinstance(targets, dict) else {}
    if not raw:
        # Target not declared in the active config. Only allow windows_hisec
        # fallback when explicitly requested via --legacy. Otherwise this is
        # a fatal error to prevent a mac target being silently routed to the
        # Windows EDR workflow.
        raise SystemExit(
            f"[FATAL] target '{target_name}' not found in active config.\n"
            f"       Available targets: {sorted(targets)}.\n"
            f"       Add it to config/targets.local.json, or use --legacy to "
            f"force windows_hisec for local debugging only."
        )
    platform = raw.get("platform", "windows")
    explicit = raw.get("app_profile") if isinstance(raw, dict) else None
    if explicit:
        return explicit
    default = DEFAULT_PROFILE_BY_PLATFORM.get(platform)
    if default is None:
        raise SystemExit(
            f"[FATAL] target '{target_name}' has platform='{platform}' but no app_profile "
            f"and no default profile for that platform. Set target.app_profile in "
            f"config/targets.local.json (e.g. 'macos_generic' or 'windows_hisec') "
            f"or pass --profile explicitly."
        )
    return default


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
    # Show platform + app_profile + runner explicitly so the operator
    # sees at a glance which test suite is about to run, and so that
    # a macos target can never silently fall through to windows_hisec.
    tc = TargetConfig()
    try:
        platform = tc.get_target_platform(target_name)
    except Exception:
        platform = "?"
    try:
        app_profile = tc.get_target_app_profile(target_name) or "(default by platform)"
    except Exception:
        app_profile = "?"

    print("=" * 60)
    print(f"EDR-WD Test Runner")
    print("=" * 60)
    print(f"  Target:      {target_name}")
    print(f"  Platform:    {platform}")
    print(f"  app_profile: {app_profile}")
    print(f"  Profile:     {profile}")
    print(f"  Runner:      {runner.__module__}.{runner.__name__}")
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

        status = client.call_tool("status", {})
        backend_kind = (
            status.get("backend_kind")
            or status.get("backend")
            if isinstance(status, dict)
            else None
        )
        if _profile_backend_conflict(profile, backend_kind):
            live_default = DEFAULT_PROFILE_BY_BACKEND.get(backend_kind)
            explicit_profile = bool(profile_override) or app_profile != "(default by platform)"
            if explicit_profile or live_default is None:
                print(
                    f"[FAIL] profile={profile!r} does not match live "
                    f"backend={backend_kind!r}."
                )
                print(
                    "       Fix target.platform/app_profile in config or pass the "
                    "matching --profile explicitly."
                )
                return False
            rerouted = resolve_runner(live_default)
            if rerouted is None:
                print(f"[FAIL] No test runner registered for live backend profile={live_default!r}.")
                return False
            print(
                f"[WARN] Config selected profile={profile!r}, but live MCP "
                f"backend is {backend_kind!r}; rerouting to profile={live_default!r}."
            )
            profile = live_default
            runner = rerouted
            print(f"  Runner:      {runner.__module__}.{runner.__name__}")
            print()

    except Exception as e:
        print(f"[FAIL] initialize exception: {e}")
        return False

    # ── Dispatch to per-profile test suite ─────────────────────────
    try:
        passed, failed, skipped, errors, ok = runner(client, verbose=verbose)
    finally:
        client.close()

    # ── Summary ───────────────────────────────────────────────────
    print()
    print("=" * 60)
    print(f"Results: {passed} passed, {failed} failed{', ' + str(skipped) + ' skipped' if skipped else ''}")
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
