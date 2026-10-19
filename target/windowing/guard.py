"""Fail-closed pointer authorization against the currently locked native window."""

from __future__ import annotations

from typing import Callable, Iterable, Mapping

try:
    from ..automation.window_scope import WindowScopeError, assert_owned_and_unoccluded
except ImportError:  # target-local deployment
    from automation.window_scope import WindowScopeError, assert_owned_and_unoccluded

from .models import WindowIdentity


class WindowGuard:
    def __init__(self, *, verify_lock: Callable, get_lock: Callable,
                 enumerate_windows: Callable, hit_test: Callable):
        self._verify_lock = verify_lock
        self._get_lock = get_lock
        self._enumerate = enumerate_windows
        self._hit_test = hit_test

    def authorize_pointer(self, identity: WindowIdentity,
                          points: Iterable[tuple[int, int]],
                          coordinate_space: str, action: str) -> tuple[tuple[int, int], ...]:
        verified = self._verify_lock(activate=False)
        if not verified.get("ok") or not verified.get("locked") or not verified.get("foreground_verified", False):
            raise WindowScopeError("input_desktop_unavailable", "foreground lock ownership is unverified")
        lock = self._get_lock().get("lock") or {}
        snapshot = lock.get("snapshot") or {}
        if (str(lock.get("handle") or snapshot.get("handle") or "") != identity.native_id or
            int(lock.get("pid") or snapshot.get("pid") or -1) != identity.process_id or
            int(lock.get("generation") or 1) != identity.generation):
            raise WindowScopeError("pointer_target_mismatch", "window lock identity changed")
        windows = self._enumerate()
        target = next((item for item in windows if str(item.get("handle")) == identity.native_id), None)
        if target is None or target.get("pid") != identity.process_id:
            raise WindowScopeError("pointer_target_mismatch", "native window disappeared or owner changed")
        rect = target.get("rect") or {}
        actual = (int(rect.get("left", rect.get("x", 0))),
                  int(rect.get("top", rect.get("y", 0))),
                  int(rect.get("right", rect.get("x", 0) + rect.get("w", 0))),
                  int(rect.get("bottom", rect.get("y", 0) + rect.get("h", 0))))
        if actual != identity.rect:
            raise WindowScopeError("observation_stale", "window geometry changed")
        if coordinate_space not in {"screen", "window"}:
            raise WindowScopeError("pointer_target_mismatch", "unsupported coordinate space")
        checked = []
        for x, y in points:
            sx, sy = (int(x), int(y)) if coordinate_space == "screen" else (
                identity.rect[0] + int(x), identity.rect[1] + int(y))
            hit = self._hit_test(sx, sy)
            if hit is None:
                raise WindowScopeError("input_desktop_unavailable", "top-level hit test unavailable")
            if str(hit) != identity.native_id:
                raise WindowScopeError("pointer_target_occluded", "hit test belongs to another window")
            assert_owned_and_unoccluded(target, windows, sx, sy, hit_test_handle=hit)
            checked.append((sx, sy))
        if not checked:
            raise WindowScopeError("pointer_target_mismatch", "pointer action has no points")
        return tuple(checked)

    def authorize_drag_path(self, identity: WindowIdentity,
                            start: tuple[int, int], end: tuple[int, int],
                            samples: int = 4, coordinate_space: str = "screen") -> tuple[tuple[int, int], ...]:
        if samples < 1:
            raise ValueError("samples must be positive")
        points = tuple((round(start[0] + (end[0] - start[0]) * index / samples),
                        round(start[1] + (end[1] - start[1]) * index / samples))
                       for index in range(samples + 1))
        return self.authorize_pointer(identity, points, coordinate_space, "drag")

    @staticmethod
    def verify_target_before_injection(resolution: Mapping,
                                       current_observation_id: str,
                                       target_rect: tuple[int, int, int, int]) -> None:
        if (resolution.get("observation_id") != current_observation_id or
            tuple(resolution.get("target_rect") or ()) != tuple(target_rect)):
            raise WindowScopeError("observation_stale", "resolved control changed before injection")
