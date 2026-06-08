#!/usr/bin/env python3
"""
test_target_config_platforms.py — Smoke tests for the platform-aware
schema in agent/target_config.py.

Runs without pytest; suitable for `python scripts/test_target_config_platforms.py`.

Covers the M1 acceptance criteria:
  - legacy win-dev without platform field normalizes to platform=windows
  - macos target validates password auth and macos.{root, python_path, backend, launch_name}
  - macos target missing a required field produces a clear error
  - platform=invalid produces a clear error
  - get_target_platform / get_target_app_profile return the right values

Exit code: 0 on success, 1 on first failure.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import traceback
from pathlib import Path

# Make agent importable
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from agent.target_config import (  # noqa: E402
    TargetConfig,
    _normalize_target,
    _validate_platform_specific,
    SUPPORTED_PLATFORMS,
)


_failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  PASS  {name}")
    else:
        msg = f"  FAIL  {name}: {detail}"
        print(msg)
        _failures.append(name)


def write_config(data: dict) -> str:
    fd, path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w") as f:
        json.dump(data, f)
    return path


def main() -> int:
    print("=" * 60)
    print("test_target_config_platforms.py")
    print("=" * 60)

    # ── 1. SUPPORTED_PLATFORMS contains windows and macos ────────
    check(
        "SUPPORTED_PLATFORMS has windows + macos",
        "windows" in SUPPORTED_PLATFORMS and "macos" in SUPPORTED_PLATFORMS,
        f"got {SUPPORTED_PLATFORMS}",
    )

    # ── 2. _normalize_target defaults platform=windows for legacy ──
    legacy_raw = {
        "ssh": {"host": "x", "port": 22, "user": "x", "auth": {"type": "key", "key_path": "/dev/null"}},
        "server": {"python_path": "/x", "host": "0.0.0.0", "port": 8765, "command": "x"},
        "task": {"name": "T"},
        "paths": {"target_root": "/tmp", "scripts": "scripts"},
        "connection": {"preferred": "direct", "direct_url": "http://x/"},
    }
    norm = _normalize_target(dict(legacy_raw))
    check(
        "legacy target normalizes to platform=windows",
        norm.get("platform") == "windows",
        f"got {norm.get('platform')}",
    )
    check(
        "legacy target keeps windows.target_root",
        norm.get("windows", {}).get("target_root") == "/tmp",
        f"got {norm.get('windows')}",
    )

    # ── 3. macos target: valid → no errors ──────────────────────
    macos_raw = {
        "platform": "macos",
        "ssh": {"host": "x", "port": 22, "user": "x", "auth": {"type": "password", "password": "pw"}},
        "mcp": {"host": "0.0.0.0", "port": 8765, "path": "/mcp", "connect_mode": "direct", "tunnel": {"enabled": False, "local_port": 18765}},
        "macos": {
            "python_path": "/opt/homebrew/bin/python3",
            "root": "/Users/x/edr-wd/target",
            "backend": "macos_accessibility",
            "launch_name": "com.edr-wd.target",
        },
    }
    errs = _validate_platform_specific(_normalize_target(dict(macos_raw)))
    check("valid macos target has no platform errors", errs == [], f"got {errs}")

    # ── 4. macos target: missing launch_name → error ────────────
    bad_macos = dict(macos_raw)
    bad_macos["macos"] = {k: v for k, v in bad_macos["macos"].items() if k != "launch_name"}
    errs = _validate_platform_specific(_normalize_target(dict(bad_macos)))
    check(
        "macos missing launch_name is flagged",
        any("launch_name" in e for e in errs),
        f"got {errs}",
    )

    # ── 5. macos target: missing backend → error ───────────────
    bad_macos = dict(macos_raw)
    bad_macos["macos"] = {k: v for k, v in bad_macos["macos"].items() if k != "backend"}
    errs = _validate_platform_specific(_normalize_target(dict(bad_macos)))
    check(
        "macos missing backend is flagged",
        any("backend" in e for e in errs),
        f"got {errs}",
    )

    # ── 6. invalid platform → error ────────────────────────────
    bad_platform = dict(macos_raw)
    bad_platform["platform"] = "freebsd"
    errs = _validate_platform_specific(_normalize_target(dict(bad_platform)))
    check(
        "platform='freebsd' is rejected",
        any("freebsd" in e or "supported" in e.lower() for e in errs),
        f"got {errs}",
    )

    # ── 7. get_target_platform / get_target_app_profile on file ──
    cfg_path = write_config({
        "default_target": "mac-test",
        "targets": {
            "mac-test": macos_raw,
            "win-legacy": legacy_raw,  # no platform/app_profile
        },
    })
    try:
        tc = TargetConfig(cfg_path)
        check(
            "mac-test platform is macos",
            tc.get_target_platform("mac-test") == "macos",
            f"got {tc.get_target_platform('mac-test')}",
        )
        check(
            "mac-test app_profile is None (not set)",
            tc.get_target_app_profile("mac-test") is None,
            f"got {tc.get_target_app_profile('mac-test')}",
        )
        check(
            "win-legacy platform is windows (default)",
            tc.get_target_platform("win-legacy") == "windows",
            f"got {tc.get_target_platform('win-legacy')}",
        )

        # validate: win-legacy triggers a legacy-schema warning (expected);
        # mac-test is fully valid so there should be no other errors
        errs = tc.validate()
        legacy_warnings = [e for e in errs if "legacy" in e]
        real_errors = [e for e in errs if "legacy" not in e]
        check(
            "validate() on mixed mac+win config returns no errors",
            len(real_errors) == 0,
            f"got real_errors={real_errors}",
        )
        check(
            "validate() reports legacy schema for win-legacy",
            len(legacy_warnings) == 1 and "win-legacy" in legacy_warnings[0],
            f"got legacy_warnings={legacy_warnings}",
        )
        ssh = tc.resolve_auth("mac-test")
        check(
            "resolve_auth prefers inline password",
            ssh.get("auth", {}).get("password") == "pw" and "password_env" not in ssh.get("auth", {}),
            f"got auth keys={list(ssh.get('auth', {}).keys())}",
        )
    finally:
        os.unlink(cfg_path)

    # ── 8. macos with explicit app_profile round-trips ────────
    cfg_path = write_config({
        "default_target": "mac-with-prof",
        "targets": {
            "mac-with-prof": {
                **macos_raw,
                "app_profile": "macos_generic",
            },
        },
    })
    try:
        tc = TargetConfig(cfg_path)
        check(
            "mac-with-prof app_profile is 'macos_generic'",
            tc.get_target_app_profile("mac-with-prof") == "macos_generic",
            f"got {tc.get_target_app_profile('mac-with-prof')}",
        )
    finally:
        os.unlink(cfg_path)

    print()
    print("=" * 60)
    if _failures:
        print(f"FAILED: {len(_failures)} check(s)")
        for f in _failures:
            print(f"  - {f}")
        return 1
    print("ALL OK")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        sys.exit(1)
