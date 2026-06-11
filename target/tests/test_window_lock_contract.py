#!/usr/bin/env python3
"""Contract checks for target-window lock behavior.

These tests avoid real GUI dependencies. They validate the safety rule that
pointer actions must fail closed when a lock exists and the foreground window
does not match the locked target.
"""

from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from automation.macos_accessibility import MacOSAccessibilityBackend  # noqa: E402


class FakeMacBackend(MacOSAccessibilityBackend):
    def __init__(self) -> None:
        self._window_lock = None
        self.frontmost = {
            "ok": True,
            "process_name": "OtherApp",
            "pid": 200,
            "title": "Other Window",
        }
        self.connected = {
            "ok": True,
            "process_name": "Finder",
            "pid": 100,
            "title": "Finder",
        }

    def _frontmost_window_state(self) -> dict:
        return dict(self.frontmost)

    def _connected_window_state(self) -> dict:
        return dict(self.connected)

    def _activate_locked_window(self) -> dict:
        lock = self._window_lock or {}
        self.frontmost = {
            "ok": True,
            "process_name": lock.get("process_name"),
            "pid": lock.get("pid"),
            "title": "Finder",
        }
        return {"ok": True, "method": "fake_activate"}


def test_lock_allows_action_after_activation() -> None:
    backend = FakeMacBackend()
    locked = backend.lock_window(process_name="Finder", activate=False)
    assert locked["ok"] is False

    verified = backend.verify_window_lock(activate=True)
    assert verified["ok"] is True

    result = backend.click_at(10, 20)
    assert result["ok"] is True
    assert result["method"] == "dry_run"


def test_lock_fails_closed_when_activation_cannot_restore_target() -> None:
    class BrokenActivationBackend(FakeMacBackend):
        def _activate_locked_window(self) -> dict:
            return {"ok": False, "error": "activation failed"}

    backend = BrokenActivationBackend()
    backend.lock_window(process_name="Finder", activate=False)
    result = backend.click_at(10, 20)
    assert result["ok"] is False
    assert result["error"] == "Window lock mismatch"


def run_tests() -> None:
    tests = [
        test_lock_allows_action_after_activation,
        test_lock_fails_closed_when_activation_cannot_restore_target,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")


if __name__ == "__main__":
    run_tests()
