"""
test_profiles.py — Dispatch table from target.app_profile to test suite.

Each test suite is a function with the same signature as the existing
run_tests() body: it receives a McpClient (already initialized) and a
verbose flag, and returns (passed, failed, skipped, errors, ok_bool)
where ok_bool is True if the runner should exit 0 (all pass) and False
otherwise.

The Windows profile keeps the legacy behaviour — all 16/16 steps from
the original run_tests.py — to avoid regressing the HiSec EDR workflow.

The macOS profile runs a minimal v1 capability set:
    - mcp initialize      (handled by conftest.mcp_initialize)
    - tools/list          (lists advertised tools)
    - screenshot          (verifies screencapture)
    - list_windows        (verifies System Events enumeration)
    - activate_app Finder (verifies osascript activate)
    - is_window_open Finder (verifies filter logic)
    - status              (verifies backend reporting)
"""

from __future__ import annotations

from typing import Callable


# Import the runner modules — they self-register on import. Using
# importlib inside resolve_runner() keeps startup cost zero for the
# case where the caller only wants the registry.

from .run_windows_hisec import run_windows_hisec_tests  # noqa: F401
from .run_macos_generic import run_macos_generic_tests  # noqa: F401
from .run_macos_hisec import run_macos_hisec_tests  # noqa: F401
from .run_registered_product import run_registered_product_tests
from target.product_profiles import BUILTIN_CAPABILITIES


# Registry: profile name -> runner function.
# Each runner signature:
#   (client, verbose) -> (passed, failed, skipped, errors_list, ok_bool)
PROFILE_RUNNERS: dict[str, Callable] = {
    "windows_hisec": run_windows_hisec_tests,
    "macos_generic": run_macos_generic_tests,
    # macos_hisec is a real HiSecEndpoint.app workflow invoked explicitly via
    # --profile macos_hisec.  It is NOT the default macOS profile because not
    # all macOS targets have HiSecEndpoint installed.
    "macos_hisec": run_macos_hisec_tests,
    # "macos_app_specific" is intentionally not registered yet — it will
    # be added when the first app-specific workflow lands.
}


def resolve_runner(profile: str | None):
    """
    Resolve a profile-specific suite or the installed product smoke test.
    Unknown profiles return None.
    """
    if not profile:
        return None
    dedicated = PROFILE_RUNNERS.get(profile)
    if dedicated is not None:
        return dedicated
    if not any(candidate_id == profile for candidate_id, _ in BUILTIN_CAPABILITIES):
        return None
    def registered_runner(client, verbose=False):
        return run_registered_product_tests(client, profile=profile, verbose=verbose)
    registered_runner.__name__ = "run_registered_product_tests"
    return registered_runner
