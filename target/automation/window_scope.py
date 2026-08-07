"""
window_scope.py — pure helpers for window-scoped pointer actions (P0.1).

P0.1 (docs/todo/window-scoped-scroll-and-verification.md) extends/wraps the raw
`scroll(clicks, x, y)` primitive so a caller can bind the event to a specific
window. The desired request shape adds:

    coordinate_space: "window",
    window_title_re:  "...",
    expected_process_name: "...",
    expected_pid: ...

This module owns the *pure, deterministic* slice of that feature — resolving a
unique target window from title/process/PID, converting window-relative
coordinates to screen coordinates, and deciding ownership/occlusion from a set
of top-level window rectangles. It never touches a live backend; the caller
(automation/windows_pywinauto.py) gathers the top-level window infos + a Win32
point hit-test and lets these functions make the decisions.

Stable machine-readable error codes (aligned with the P0.3 envelope vocabulary):
    * `target_not_found`   — no top-level window matched the selector.
    * `target_ambiguous`   — the selector matched more than one top-level window.
    * `point_outside_window` — converted screen point falls outside the target
                               top-level window's rectangle.
    * `target_occluded`    — another top-level window covers the point, so a
                             raw pointer action could hit the wrong window.

Window info dict shape consumed here:
    {
        "handle": int,            # top-level HWND (for hit-test comparison)
        "title": str,
        "process_name": str,      # owning process name (may include .exe)
        "pid": int,
        "rect": {
            "left": int, "top": int,
            "right": int, "bottom": int,
            "width": int, "height": int,
        },
    }
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Optional


class WindowScopeError(Exception):
    """A window-scoping failure with a stable machine-readable `code`."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code

    def to_dict(self) -> dict:
        return {"ok": False, "code": self.code, "error": str(self)}


def _norm_process_name(name: Any) -> str:
    """Normalise a process name for comparison: lowercase, drop `.exe`."""
    return str(name or "").lower().removesuffix(".exe")


def matches_window(
    info: Mapping,
    *,
    title_re: Optional[str] = None,
    process_name: Optional[str] = None,
    pid: Optional[int] = None,
) -> bool:
    """True if a window info dict satisfies every provided selector."""
    if title_re:
        try:
            if not re.search(str(title_re), str(info.get("title") or ""), re.IGNORECASE):
                return False
        except re.error:
            return False
    if process_name:
        current = _norm_process_name(info.get("process_name"))
        expected = _norm_process_name(process_name)
        if current != expected:
            # accept a substring fallback so a short selector can match a
            # qualified exe path name without breaking exact matching.
            if expected not in current and current not in expected:
                return False
    if pid is not None:
        if info.get("pid") != int(pid):
            return False
    return True


def resolve_unique_window(
    windows: list[Mapping],
    *,
    title_re: Optional[str] = None,
    process_name: Optional[str] = None,
    pid: Optional[int] = None,
) -> tuple[Optional[Mapping], Optional[WindowScopeError]]:
    """Resolve the unique target window from title/process/optional PID.

    Returns `(window_info, None)` on a unique match, or `(None, err)` where
    `err.code` is `target_not_found` / `target_ambiguous`.
    """
    matched = [
        w for w in windows
        if matches_window(
            w, title_re=title_re, process_name=process_name, pid=pid
        )
    ]
    if not matched:
        return None, WindowScopeError(
            "target_not_found",
            (
                "no top-level window matched the selector"
                + (f" (title_re={title_re!r})" if title_re else "")
                + (f" (process_name={process_name!r})" if process_name else "")
                + (f" (pid={pid})" if pid is not None else "")
            ),
        )
    if len(matched) > 1:
        return None, WindowScopeError(
            "target_ambiguous",
            f"window selector matched {len(matched)} top-level windows; "
            "add an expected_pid or a more specific title_re",
        )
    return matched[0], None


def to_screen(rect: Mapping, x: int, y: int) -> tuple[int, int]:
    """Convert window-relative (x, y) to screen coordinates.

    `rect` uses the pywinauto-ish shape ({left, top, right, bottom, width,
    height}); left/top are the window's client-relative origin on screen.
    """
    left = int(rect.get("left", rect.get("x", 0)))
    top = int(rect.get("top", rect.get("y", 0)))
    return left + int(x), top + int(y)


def point_in_rect(rect: Mapping, sx: int, sy: int) -> bool:
    """True if the *screen* point lies within the window's on-screen rect."""
    left = int(rect.get("left", rect.get("x", 0)))
    top = int(rect.get("top", rect.get("y", 0)))
    right = int(rect.get("right", left + int(rect.get("width", 0))))
    bottom = int(rect.get("bottom", top + int(rect.get("height", 0))))
    return left <= int(sx) < right and top <= int(sy) < bottom


def top_windows_at_point(
    windows: list[Mapping], sx: int, sy: int
) -> list[Mapping]:
    """Top-level windows whose on-screen rect contains the screen point."""
    return [w for w in windows if point_in_rect(w.get("rect") or {}, sx, sy)]


def assert_owned_and_unoccluded(
    target: Mapping,
    windows: list[Mapping],
    sx: int,
    sy: int,
    *,
    hit_test_handle: Optional[int] = None,
) -> None:
    """Enforce the P0.1 ownership contract for a single screen point.

    1. The converted point must fall inside the target window's rect
       (else `point_outside_window`).
    2. No *other* top-level window may cover the point (else
       `target_occluded`).
    3. If a real hit-test handle was supplied (Win32 WindowFromPoint ->
       top-level ancestor), it must equal the target window's handle
       (else `target_occluded`).

    Raises `WindowScopeError` on any violation; returns None when safe.
    """
    rect = target.get("rect") or {}
    if not point_in_rect(rect, sx, sy):
        raise WindowScopeError(
            "point_outside_window",
            f"screen point ({sx},{sy}) is outside target window rect {rect}",
        )

    target_handle = target.get("handle")
    covering = [
        w for w in windows
        if w is not target
        and point_in_rect(w.get("rect") or {}, sx, sy)
    ]
    if covering:
        # Distinguish an exact hit-test conflict from a merely-enclosing rect
        # so callers can decide how strict to be.
        if hit_test_handle is not None and target_handle is not None:
            if int(hit_test_handle) != int(target_handle):
                raise WindowScopeError(
                    "target_occluded",
                    (
                        f"point ({sx},{sy}) is covered by another top-level "
                        f"window (hit-test hwnd={hit_test_handle}, "
                        f"target hwnd={target_handle})"
                    ),
                )
        else:
            raise WindowScopeError(
                "target_occluded",
                (
                    f"point ({sx},{sy}) falls within {len(covering)} other "
                    "top-level window rect(s); refusing raw scroll"
                ),
            )


__all__ = [
    "WindowScopeError",
    "matches_window",
    "resolve_unique_window",
    "to_screen",
    "point_in_rect",
    "top_windows_at_point",
    "assert_owned_and_unoccluded",
]
