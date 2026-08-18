# -*- coding: utf-8 -*-
"""P0.2 — Windows RDP foreground fallback for window-lock verification.

docs/todo/window-scoped-scroll-and-verification.md (P0.2):

  - Prefer Win32 foreground HWND, PID, title, rect over pywinauto-only active
    window detection.
  - `strict=True` blocks when foreground ownership cannot be verified.
  - `strict=False` still checks window existence, coordinate hit, and process
    ownership before dispatch.
  - Degraded verification is explicit in the result payload.

These tests exercise the WindowsPywinautoBackend.verify_window_lock() logic via
the `__new__` pattern (no pywinauto/psutil/ctypes-windll needed), with the
_*_state helpers monkeypatched to simulate Win32-foreground / pywinauto states.
"""

import pytest

# The Windows backend module does a top-level `import pyautogui` (Windows-only,
# not installed in the macOS dev venv). Stub it in sys.modules *before* the
# module-level import below so collection succeeds; the P0.1 test file uses the
# same approach via a module-scoped autouse fixture.
import sys as _sys
if "pyautogui" not in _sys.modules:
    _pag = _sys.modules.setdefault(
        "pyautogui", type("pyautogui_stub", (), {})())
    _pag.moveTo = lambda *a, **k: None
    _pag.scroll = lambda *a, **k: None
    _pag.doubleClick = lambda *a, **k: None
    _pag.rightClick = lambda *a, **k: None
    _pag.middleClick = lambda *a, **k: None
    _pag.dragTo = lambda *a, **k: None

from automation.windows_pywinauto import WindowsPywinautoBackend


# ── State helpers ────────────────────────────────────────────────────────────

def _win32_foreground(handle=1001, title="日志中心", pid=6752,
                      process_name="EDRClient.exe"):
    """A live Win32-foreground state dict (the P0.2 preferred source)."""
    return {
        "ok": True,
        "handle": handle,
        "title": title,
        "pid": pid,
        "process_name": process_name,
        "rectangle": {
            "left": 0, "top": 0, "right": 800, "bottom": 600,
            "width": 800, "height": 600,
        },
        "source": "win32_foreground",
    }


def _pywinauto_active(handle=1001, title="日志中心", pid=6752,
                      process_name="EDRClient.exe"):
    """A pywinauto-fallback active state dict (P0.2 fallback source)."""
    return {
        "ok": True,
        "handle": handle,
        "title": title,
        "pid": pid,
        "process_name": process_name,
        "rectangle": {"x": 0, "y": 0, "w": 800, "h": 600},
        "source": "pywinauto_active",
    }


def _unavailable():
    """No foreground/active window could be resolved at all."""
    return {"ok": False, "error": "no active window", "source": "win32_foreground"}


def _lock(strict=True, pid=6752, process_name="EDRClient.exe", handle=1001):
    return {
        "backend": "windows_pywinauto",
        "title_re": "^日志中心$",
        "process_name": process_name,
        "pid": pid,
        "handle": handle,
        "strict": strict,
    }


def _default_enumerate():
    return [{
        "handle": 1001, "title": "日志中心",
        "process_name": "EDRClient.exe", "pid": 6752,
        "rect": {"left": 0, "top": 0, "right": 800, "bottom": 600,
                 "width": 800, "height": 600},
    }]


def make_backend(active_state, window_lock,
                 connected_state=None, enumerate_windows=None,
                 enumerate_override=None):
    """Build a WindowsPywinautoBackend instance without pywinauto/psutil.

    Returns the impl with helper methods patched to the provided fakes and
    `_window_lock` pre-set, so callers can drive `verify_window_lock()`
    directly."""
    impl = WindowsPywinautoBackend.__new__(WindowsPywinautoBackend)
    impl._active_window_state = lambda: active_state
    impl._state_matches_lock = WindowsPywinautoBackend._state_matches_lock.__get__(
        impl, WindowsPywinautoBackend)
    impl._activate_locked_window = lambda: {"ok": True, "method": "test"}
    if connected_state is not None:
        impl._connected_window_state = lambda: connected_state
    else:
        impl._connected_window_state = WindowsPywinautoBackend._connected_window_state.__get__(
            impl, WindowsPywinautoBackend)
    if enumerate_override is not None:
        impl._enumerate_top_windows_windows = enumerate_override
    else:
        impl._enumerate_top_windows_windows = (
            enumerate_windows if enumerate_windows is not None else _default_enumerate)
    impl._locked_window_still_exists = WindowsPywinautoBackend._locked_window_still_exists.__get__(
        impl, WindowsPywinautoBackend)
    impl._window_lock = window_lock
    return impl


# ── _win32_foreground_state shape ────────────────────────────────────────────

class TestWin32ForegroundResolution:
    def test_no_crash_and_degraded_off_win32(self):
        """On a non-Windows host this must degrade (ok=False), never crash."""
        out = WindowsPywinautoBackend._win32_foreground_state()
        assert "ok" in out
        assert out.get("source") == "win32_foreground"
        if not out.get("ok"):
            assert "error" in out


# ── verify_window_lock: preference for Win32 foreground ──────────────────────

class TestPreferWin32Foreground:
    def test_no_lock(self):
        b = make_backend(_win32_foreground(), None)
        r = b.verify_window_lock()
        assert r["ok"] is True
        assert r["locked"] is False

    def test_matched_win32_foreground_is_full_verify(self):
        b = make_backend(_win32_foreground(), _lock(strict=True))
        r = b.verify_window_lock(activate=True)
        assert r["ok"] is True
        assert r["locked"] is True
        assert r["degraded"] is False
        assert r["verification"]["source"] == "win32_foreground"
        assert r["foreground"]["handle"] == 1001
        assert r["active"]["source"] == "win32_foreground"

    def test_pywinauto_fallback_match_is_still_full_verify(self):
        b = make_backend(_pywinauto_active(), _lock(strict=True))
        r = b.verify_window_lock(activate=True)
        assert r["ok"] is True
        assert r["degraded"] is False
        assert r["verification"]["source"] == "pywinauto_active"


# ── strict=True blocks ───────────────────────────────────────────────────────

class TestStrictBlocking:
    def test_mismatch_blocks_with_ownership_mismatch(self):
        b = make_backend(
            _win32_foreground(handle=9000, title="记事本",
                              pid=7777, process_name="Notepad.exe"),
            _lock(strict=True),
        )
        r = b.verify_window_lock(activate=True)
        assert r["ok"] is False
        assert r["locked"] is True
        assert r["code"] == "ownership_mismatch"
        assert r["degraded"] is True

    def test_unavailable_foreground_blocks_with_verification_unavailable(self):
        b = make_backend(_unavailable(), _lock(strict=True))
        r = b.verify_window_lock(activate=True)
        assert r["ok"] is False
        assert r["code"] == "verification_unavailable"
        assert r["degraded"] is True

    def test_strict_defaults_true_when_not_recorded(self):
        lock = _lock()
        del lock["strict"]
        b = make_backend(
            _win32_foreground(handle=9000, title="记事本",
                              pid=7777, process_name="Notepad.exe"),
            lock,
        )
        r = b.verify_window_lock(activate=True)
        assert r["ok"] is False
        assert r["code"] == "ownership_mismatch"


# ── strict=False degraded path ───────────────────────────────────────────────

class TestStrictFalseDegraded:
    def test_mismatch_but_window_exists_allows_ok_degraded(self):
        b = make_backend(
            _win32_foreground(handle=9000, title="记事本",
                              pid=7777, process_name="Notepad.exe"),
            _lock(strict=False),
            connected_state=_win32_foreground(),  # connected main window
        )
        r = b.verify_window_lock(activate=True)
        assert r["ok"] is True
        assert r["locked"] is True
        assert r["degraded"] is True
        assert r["degraded_reason"]
        assert r["verification"]["method"] == "degraded_existence"
        assert r["verification"]["strict"] is False

    def test_mismatch_but_window_exists_strict_true_still_blocks(self):
        b = make_backend(
            _win32_foreground(handle=9000, title="记事本",
                              pid=7777, process_name="Notepad.exe"),
            _lock(strict=True),
            connected_state=_win32_foreground(),
        )
        r = b.verify_window_lock(activate=True)
        assert r["ok"] is False
        assert r["code"] == "ownership_mismatch"

    def test_window_gone_blocks_even_in_non_strict(self):
        b = make_backend(
            _unavailable(),
            _lock(strict=False),
            connected_state=_win32_foreground(),
            enumerate_windows=lambda: [],  # locked window no longer found
        )
        r = b.verify_window_lock(activate=True)
        assert r["ok"] is False
        assert "existence" in r["verification"]

    def test_process_ownership_mismatch_blocks_in_non_strict(self):
        # Foreground unavailable, and the connected window is a different app.
        b = make_backend(
            _unavailable(),
            _lock(strict=False),
            connected_state=_win32_foreground(handle=9000, title="记事本",
                                              pid=7777,
                                              process_name="Notepad.exe"),
            enumerate_windows=lambda: [{
                "handle": 9000, "title": "记事本",
                "process_name": "Notepad.exe", "pid": 7777,
                "rect": {},  # fails probe (title/process/pid all differ)
            }],
        )
        r = b.verify_window_lock(activate=True)
        assert r["ok"] is False
        assert r["degraded"] is True


# ── lock_window records handle ───────────────────────────────────────────────

class TestLockRecordsHandle:
    def test_lock_window_captures_handle(self):
        impl = WindowsPywinautoBackend.__new__(WindowsPywinautoBackend)
        impl._connected_window_state = lambda: _win32_foreground()
        impl._activate_locked_window = lambda: {"ok": True, "method": "test"}
        impl._window_lock = None
        out = impl.lock_window()
        assert out["lock"]["handle"] == 1001
        assert out["lock"]["strict"] is True


# ── lock_window validates the caller's criteria ──────────────────────────────


def _lockable_backend(state):
    impl = WindowsPywinautoBackend.__new__(WindowsPywinautoBackend)
    impl._connected_window_state = lambda: state
    impl._activate_locked_window = lambda: {"ok": True, "method": "test"}
    impl.verify_window_lock = lambda activate=True: {"ok": True}
    impl._window_lock = None
    return impl


class TestLockCriteriaAreValidated:
    """Two same-titled Qt windows must never produce a mislabelled lock."""

    def test_locking_a_different_process_than_requested_is_refused(self):
        # connect() resolved HiSecEndpointAgent; the caller believes EDRClient.
        impl = _lockable_backend(
            _win32_foreground(title="logo1", pid=6960,
                              process_name="HiSecEndpointAgent.exe")
        )

        out = impl.lock_window(title_re="^logo1$", process_name="EDRClient.exe")

        assert out["ok"] is False
        assert out["code"] == "window_lock_criteria_mismatch"
        assert out["mismatch"] == ["process_name"]
        assert out["actual"]["process_name"] == "HiSecEndpointAgent.exe"
        assert impl._window_lock is None

    def test_locking_a_different_pid_than_requested_is_refused(self):
        impl = _lockable_backend(_win32_foreground(pid=6960))
        out = impl.lock_window(pid=5948)
        assert out["ok"] is False and out["mismatch"] == ["pid"]

    def test_locking_a_non_matching_title_is_refused(self):
        impl = _lockable_backend(_win32_foreground(title="logo1"))
        out = impl.lock_window(title_re="^安全防护中心$")
        assert out["ok"] is False and out["mismatch"] == ["title_re"]

    def test_matching_criteria_lock_and_record_the_live_identity(self):
        impl = _lockable_backend(_win32_foreground(title="logo1", pid=5948))
        out = impl.lock_window(title_re="^logo1$", process_name="EDRClient.exe", pid=5948)
        assert out["ok"] is True
        assert out["lock"]["pid"] == 5948
        assert out["lock"]["process_name"] == "EDRClient.exe"
        assert out["lock"]["handle"] == 1001

    def test_identity_comes_from_the_window_not_the_caller(self):
        """Even without contradiction, the lock must describe what it holds."""
        impl = _lockable_backend(
            _win32_foreground(pid=5948, process_name="EDRClient.exe")
        )
        out = impl.lock_window(process_name="edrclient")
        assert out["ok"] is True
        assert out["lock"]["process_name"] == "EDRClient.exe"

    def test_unobservable_identity_cannot_contradict_the_request(self):
        """RDP-degraded states omit process/pid; locking must still work."""
        state = _win32_foreground()
        state["process_name"] = None
        state["pid"] = None
        impl = _lockable_backend(state)
        out = impl.lock_window(process_name="EDRClient.exe", pid=5948)
        assert out["ok"] is True
        assert out["lock"]["process_name"] == "EDRClient.exe"
        assert out["lock"]["pid"] == 5948


if __name__ == "__main__":
    pytest.main([__file__, "-q"])


# ── headless verification (no input desktop) ─────────────────────────────────


def _headless_backend(owner, connected=None):
    """A backend whose foreground is unresolvable, as when nothing renders."""
    impl = make_backend(_unavailable(), _lock(strict=True), connected_state=connected or {"ok": False})
    impl._win32_window_owner = staticmethod(lambda handle: owner)
    return impl


class TestHeadlessVerification:
    """A disconnected session cannot answer "is my window frontmost?".

    That is not the same as "another window owns the foreground", and must not
    be reported as one: capture and semantic actions stay possible, coordinate
    input does not.
    """

    OWNER = {
        "ok": True, "handle": 1001, "pid": 6752,
        "process_name": "EDRClient.exe", "source": "win32_owner",
    }

    def test_existing_and_correctly_owned_window_passes_without_a_foreground(self):
        r = _headless_backend(self.OWNER).verify_window_lock(activate=True)
        assert r["ok"] is True
        assert r["foreground_verified"] is False
        assert r["degraded"] is True
        assert r["verification"]["method"] == "headless_existence"

    def test_a_destroyed_window_still_blocks(self):
        owner = {"ok": False, "reason": "locked window handle no longer exists"}
        r = _headless_backend(owner).verify_window_lock(activate=True)
        assert r["ok"] is False
        assert r["code"] == "verification_unavailable"

    def test_a_handle_now_owned_by_another_process_blocks(self):
        owner = {**self.OWNER, "process_name": "HiSecEndpointAgent.exe"}
        r = _headless_backend(owner).verify_window_lock(activate=True)
        assert r["ok"] is False

    def test_a_handle_now_owned_by_another_pid_blocks(self):
        owner = {**self.OWNER, "pid": 9999}
        r = _headless_backend(owner).verify_window_lock(activate=True)
        assert r["ok"] is False

    def test_an_unresolvable_process_name_fails_closed(self):
        owner = {**self.OWNER, "process_name": None}
        r = _headless_backend(owner).verify_window_lock(activate=True)
        assert r["ok"] is False

    def test_a_resolvable_foreground_that_mismatches_is_still_an_ownership_error(self):
        """Headless leniency must not leak into the case where we *can* look."""
        b = make_backend(
            _win32_foreground(handle=9000, title="记事本", pid=7777,
                              process_name="Notepad.exe"),
            _lock(strict=True),
        )
        r = b.verify_window_lock(activate=True)
        assert r["ok"] is False
        assert r["code"] == "ownership_mismatch"


class TestCoordinateInputStillRefusesHeadless:
    def test_coordinate_dispatch_refuses_a_lock_with_no_verified_foreground(self):
        impl = _headless_backend(TestHeadlessVerification.OWNER)
        error = impl._ensure_window_lock()
        assert error is not None
        assert error["code"] == "input_desktop_unavailable"

    def test_non_coordinate_callers_may_opt_out_of_the_foreground_requirement(self):
        impl = _headless_backend(TestHeadlessVerification.OWNER)
        assert impl._ensure_window_lock(require_foreground=False) is None

    def test_a_verified_foreground_authorises_coordinate_dispatch(self):
        b = make_backend(_win32_foreground(), _lock(strict=True))
        assert b._ensure_window_lock() is None
