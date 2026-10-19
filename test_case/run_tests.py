#!/usr/bin/env python3
"""
run_tests.py — Profile-aware test runner (no pytest required)

Usage:
  python run_tests.py                                # use default_target, dispatch by app_profile
  python run_tests.py -v                             # verbose output
  python run_tests.py --target 2.26-edr-win26-win11       # specific target
  EDR_WD_TARGET=2.29-edr-mac29-macos14 python run_tests.py  # macOS target
  python run_tests.py --profile macos_generic        # force a profile (overrides target.app_profile)
  python run_tests.py --legacy                       # bypass profile dispatch (raw Windows E2E)

The runner now dispatches to a per-profile test suite:
  - profile=windows_hisec  → test_case.run_windows_hisec (legacy 16/16 E2E)
  - profile=macos_generic  → test_case.run_macos_generic (v1 minimal capability set)
  - other installed profiles → read-only product window/observation smoke test

Unknown profiles abort rather than silently running a different product's tests.
"""

import sys
import os
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from test_case.conftest import (
    McpClient,
    get_target_name,
)
from test_case.test_profiles import resolve_runner, PROFILE_RUNNERS

# Target manager for health checks
from agent import target_manager
from agent.target_config import TargetConfig
from agent.subagent import TargetSubAgent
from target.product_profiles import default_profile_for_backend


# The application catalog owns the per-platform default. Both platforms
# currently select EDRClient; a configured app_profile always wins.

def _resolve_profile(target_name: str, override: str | None) -> str:
    """
    Decide which test profile to run.

    Priority:
      1. --profile CLI override
      2. target.app_profile (explicit in config)
      3. the catalog's default EDRClient profile for the target platform
      4. fail with a clear error (no silent fallback to a wrong-platform runner)

    A macOS target without app_profile resolves to macos_hisec, never to the
    Windows EDR profile.
    """
    if override:
        return override
    tc = TargetConfig()
    try:
        target_cfg = tc.get_target(target_name)
    except KeyError:
        targets = tc.list_targets()
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
    platform = target_cfg.get("platform", "windows")
    explicit = target_cfg.get("app_profile")
    if explicit:
        return explicit
    backend = {"windows": "windows_pywinauto",
               "macos": "macos_accessibility"}.get(platform)
    if backend is None:
        raise SystemExit(
            f"[FATAL] target '{target_name}' has platform='{platform}' but no app_profile "
            f"and no default profile for that platform. Set target.app_profile in "
            f"config/targets.local.json (e.g. 'macos_generic' or 'windows_hisec') "
            f"or pass --profile explicitly."
        )
    return default_profile_for_backend(backend)


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
        app_profile = tc.get_target(target_name).get("app_profile") or "(default by platform)"
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

    subagent = TargetSubAgent.from_name(target_name, config=tc)

    # ── Ensure MCP server is running on target ─────────────────────
    print("=" * 60)
    print("Starting MCP Server")
    print("=" * 60)
    ensure_result = subagent.ensure_running()
    server_ok = bool(ensure_result.get("ok"))
    status_text = ensure_result.get("data", {}).get("status", "running")
    srv_msg = (
        f"{target_name}: {status_text}"
        if server_ok
        else f"{target_name}: {ensure_result.get('error', 'unknown error')}"
    )
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
        init_result = subagent.initialize_mcp()
        if not init_result["ok"]:
            print(f"[FAIL] MCP initialize failed: {init_result.get('error')}")
            return False

        session_id = init_result["data"]["session_id"]
        print(f"[OK] MCP session: {session_id}")
        print()

        client = McpClient(mcp_init_result=init_result)

        explicit_profile = bool(profile_override) or app_profile != "(default by platform)"
        ok_profile, profile_to_run, profile_error = subagent.resolve_profile_for_live_backend(
            profile,
            explicit_profile=explicit_profile,
        )
        if not ok_profile:
            print(f"[FAIL] {profile_error}.")
            print(
                "       Fix target.platform/app_profile in config or pass the "
                "matching --profile explicitly."
            )
            return False
        if profile_to_run != profile:
            rerouted = resolve_runner(profile_to_run)
            if rerouted is None:
                print(f"[FAIL] No test runner registered for live backend profile={profile_to_run!r}.")
                return False
            print(
                f"[WARN] Config selected profile={profile!r}, but live MCP "
                f"backend is {subagent.state.backend_kind!r}; "
                f"rerouting to profile={profile_to_run!r}."
            )
            profile = profile_to_run
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
                             "Use an installed product ID; dedicated suites: "
                             f"{sorted(PROFILE_RUNNERS)}")
    parser.add_argument("--legacy", action="store_true",
                        help="Shorthand for --profile windows_hisec")
    args = parser.parse_args()

    profile = "windows_hisec" if args.legacy else args.profile
    ok = run_tests(verbose=args.verbose, target=args.target, profile_override=profile)
    sys.exit(0 if ok else 1)
