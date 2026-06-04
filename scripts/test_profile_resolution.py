#!/usr/bin/env python3
"""
test_profile_resolution.py — Smoke tests for test_case.run_tests._resolve_profile.

Validates the M6.1 contract:
  - win-dev (legacy, no app_profile)           -> windows_hisec
  - macos target without app_profile           -> macos_generic
  - macos target with explicit app_profile     -> the explicit value
  - --profile override                         -> always wins
  - unknown platform + no app_profile          -> SystemExit
  - target not in active config                -> windows_hisec (legacy default)

Exits 0 on success, 1 on failure. No pytest required.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "test_case"))

from agent.target_config import TargetConfig  # noqa: E402

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
    print("test_profile_resolution.py")
    print("=" * 60)

    # Build a config covering each scenario. The order of `targets` does
    # not matter; what matters is whether each target has a platform and
    # an app_profile field.
    config_data = {
        "default_target": "win-legacy",
        "targets": {
            "win-legacy": {  # legacy schema, no platform, no app_profile
                "ssh": {"host": "x", "port": 22, "user": "x", "auth": {"type": "key", "key_path": "/dev/null"}},
                "server": {"python_path": "/x", "host": "0.0.0.0", "port": 8765, "command": "x"},
                "task": {"name": "T"},
                "paths": {"target_root": "/tmp", "scripts": "scripts"},
                "connection": {"preferred": "direct", "direct_url": "http://x/"},
            },
            "macos-no-profile": {  # platform=macos, NO app_profile
                "platform": "macos",
                "ssh": {"host": "x", "port": 22, "user": "x", "auth": {"type": "key", "key_path": "/dev/null"}},
                "mcp": {"host": "0.0.0.0", "port": 8765, "path": "/mcp", "connect_mode": "direct", "tunnel": {"enabled": False, "local_port": 18765}},
                "macos": {"python_path": "/usr/bin/python3", "root": "/tmp", "backend": "macos_accessibility", "launch_name": "com.test"},
            },
            "macos-with-profile": {  # platform=macos, app_profile=macos_generic
                "platform": "macos",
                "app_profile": "macos_generic",
                "ssh": {"host": "x", "port": 22, "user": "x", "auth": {"type": "key", "key_path": "/dev/null"}},
                "mcp": {"host": "0.0.0.0", "port": 8765, "path": "/mcp", "connect_mode": "direct", "tunnel": {"enabled": False, "local_port": 18765}},
                "macos": {"python_path": "/usr/bin/python3", "root": "/tmp", "backend": "macos_accessibility", "launch_name": "com.test"},
            },
            "alien-no-profile": {  # platform=linux (not in lifecycle), no app_profile
                "platform": "linux",
                "ssh": {"host": "x", "port": 22, "user": "x", "auth": {"type": "key", "key_path": "/dev/null"}},
                "mcp": {"host": "0.0.0.0", "port": 8765, "path": "/mcp", "connect_mode": "direct", "tunnel": {"enabled": False, "local_port": 18765}},
            },
        },
    }

    cfg_path = write_config(config_data)
    # Force TargetConfig to use our test file, regardless of EDR_WD_CONFIG
    # and the existence of targets.local.json.
    os.environ["EDR_WD_CONFIG"] = cfg_path

    try:
        # Re-import run_tests so it picks up the EDR_WD_CONFIG env var
        # through a fresh TargetConfig() call.
        if "run_tests" in sys.modules:
            del sys.modules["run_tests"]
        from run_tests import _resolve_profile  # noqa: E402

        cases = [
            # (target, override, expected, must_raise)
            ("win-legacy",         None,              "windows_hisec", False),
            ("macos-no-profile",   None,              "macos_generic", False),
            ("macos-with-profile", None,              "macos_generic", False),
            ("win-legacy",         "macos_generic",   "macos_generic", False),
            ("macos-no-profile",   "windows_hisec",   "windows_hisec", False),
            ("alien-no-profile",   None,              "SystemExit",    True),
            ("does-not-exist",     None,              "windows_hisec", False),
        ]

        for tgt, override, expected, must_raise in cases:
            try:
                result = _resolve_profile(tgt, override)
                if must_raise:
                    check(
                        f"{tgt!r} override={override!r} raises",
                        False,
                        f"expected SystemExit, got {result!r}",
                    )
                else:
                    check(
                        f"{tgt!r} override={override!r}",
                        result == expected,
                        f"expected {expected!r}, got {result!r}",
                    )
            except SystemExit:
                if must_raise:
                    check(
                        f"{tgt!r} override={override!r} raises",
                        True,
                        "raised SystemExit as expected",
                    )
                else:
                    check(
                        f"{tgt!r} override={override!r}",
                        False,
                        "unexpectedly raised SystemExit",
                    )

    finally:
        os.unlink(cfg_path)
        del os.environ["EDR_WD_CONFIG"]

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
