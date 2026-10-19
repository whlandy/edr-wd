"""Zero-input guard decisions for moved, covered and disconnected windows."""

from __future__ import annotations

import pytest

from target.automation.window_scope import WindowScopeError
from target.windowing import WindowIdentity
from target.windowing.guard import WindowGuard


def _identity():
    return WindowIdentity.from_native(
        {"handle": 10, "pid": 4, "process_name": "Demo", "title": "Main",
         "rect": [100, 100, 300, 300]},
        session_id="s", backend="windows_pywinauto",
    )


def _guard(*, windows=None, hit=10, foreground=True):
    target = {"handle": 10, "pid": 4,
              "rect": {"left": 100, "top": 100, "right": 300, "bottom": 300}}
    return WindowGuard(
        verify_lock=lambda activate=False: {"ok": True, "locked": True,
                                            "foreground_verified": foreground},
        get_lock=lambda: {"lock": {"handle": 10, "pid": 4, "generation": 1}},
        enumerate_windows=lambda: windows if windows is not None else [target],
        hit_test=lambda x, y: hit,
    )


def test_guard_accepts_owned_window_relative_point():
    assert _guard().authorize_pointer(_identity(), ((20, 20),), "window", "click") == ((120, 120),)


@pytest.mark.parametrize("guard,points,space", [
    (_guard(), ((250, 0),), "window"),
    (_guard(hit=11), ((120, 120),), "screen"),
    (_guard(foreground=False), ((120, 120),), "screen"),
    (_guard(windows=[]), ((120, 120),), "screen"),
])
def test_guard_rejects_unsafe_pointer_without_dispatch(guard, points, space):
    injected = 0
    for _ in range(5):
        with pytest.raises(WindowScopeError):
            guard.authorize_pointer(_identity(), points, space, "click")
    assert injected == 0


def test_drag_rejects_midpoint_overlay():
    target = {"handle": 10, "pid": 4,
              "rect": {"left": 100, "top": 100, "right": 300, "bottom": 300}}
    overlay = {"handle": 11, "pid": 5,
               "rect": {"left": 185, "top": 100, "right": 215, "bottom": 300}}
    guard = _guard(windows=[target, overlay], hit=11)
    with pytest.raises(WindowScopeError):
        guard.authorize_drag_path(_identity(), (120, 150), (280, 150))
