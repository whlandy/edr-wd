"""Session-local reconciliation, lifecycle and unique role resolution."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, replace
from typing import Callable, Iterable, Mapping

from .models import WindowIdentity, WindowIdentityError
from .relations import link_parent


@dataclass(frozen=True)
class RegistryDelta:
    opened: tuple[WindowIdentity, ...]
    changed: tuple[WindowIdentity, ...]
    closed: tuple[WindowIdentity, ...]


class WindowRegistry:
    def __init__(self, session_id: str, backend: str,
                 focus_callback: Callable[[WindowIdentity], bool] | None = None):
        self.session_id = session_id
        self.backend = backend
        self._windows: dict[str, WindowIdentity] = {}
        self._tombstones: dict[str, tuple[int, float]] = {}
        self._lock: WindowIdentity | None = None
        self._focus_callback = focus_callback

    def list(self) -> tuple[WindowIdentity, ...]:
        return tuple(self._windows.values())

    @property
    def locked(self) -> WindowIdentity | None:
        return self._lock

    def reconcile(self, windows: Iterable[Mapping], *, now: float | None = None) -> RegistryDelta:
        now = time.monotonic() if now is None else now
        # Keep tombstones for the entire run. Recycling an HWND after a fixed
        # TTL must never make an old generation executable again.
        opened, changed, closed = [], [], []
        next_windows: dict[str, WindowIdentity] = {}
        for raw in windows:
            identity = WindowIdentity.from_native(raw, session_id=self.session_id,
                                                  backend=self.backend)
            key = identity.native_id
            if key in next_windows:
                raise WindowIdentityError("window_identity_ambiguous")
            previous = self._windows.get(key)
            if previous is None:
                identity = identity.with_generation(self._tombstones.get(key, (0, 0))[0] + 1)
                opened.append(identity)
            else:
                replaced = (previous.process_id != identity.process_id or
                            previous.root_control_id != identity.root_control_id or
                            previous.class_name != identity.class_name)
                identity = identity.with_generation(previous.generation + int(replaced))
                if not replaced and identity.opened_by_action_id is None:
                    identity = replace(identity,
                                       opened_by_action_id=previous.opened_by_action_id)
                if identity != previous:
                    changed.append(identity)
            next_windows[key] = identity
        for key, previous in self._windows.items():
            if key not in next_windows:
                closed.append(previous)
                self._tombstones[key] = (previous.generation, now)
        self._windows = next_windows
        if self._lock is not None and not self.verify(self._lock):
            self._lock = None
        return RegistryDelta(tuple(opened), tuple(changed), tuple(closed))

    def apply_event(self, event: Mapping) -> None:
        # Events are hints only. A subsequent enumeration is authoritative.
        if event.get("kind") == "closed":
            key = str(event.get("native_id") or "")
            if key in self._windows:
                previous = self._windows.pop(key)
                self._tombstones[key] = (previous.generation, time.monotonic())
                if self._lock and self._lock.native_id == key:
                    self._lock = None

    def resolve_unique(self, role: str, profile) -> WindowIdentity:
        rules = [item for item in profile.window_roles if item.role == role]
        if len(rules) != 1:
            raise WindowIdentityError("window_role_unknown")
        rule = rules[0]
        parent = None
        if rule.parent_role:
            try:
                parent = self.resolve_unique(rule.parent_role, profile)
            except WindowIdentityError as exc:
                if exc.code != "window_role_not_found" or not (rule.title_pattern or rule.root_control_ids):
                    raise
        matches = []
        for window in self._windows.values():
            if not window.visible:
                continue
            if rule.process_names and window.process_name.lower() not in {
                name.lower() for name in rule.process_names
            }:
                continue
            if rule.title_pattern and not re.search(rule.title_pattern, window.title):
                continue
            if rule.root_control_ids and window.root_control_id not in rule.root_control_ids:
                continue
            if parent is not None:
                if (window.parent_native_id is not None
                        and window.parent_native_id != parent.native_id):
                    continue
                relation = link_parent(window, (parent,), window.opened_by_action_id)
                if relation is not None and relation != parent.native_id:
                    continue
                # A broad role (for example an arbitrary dialog) cannot be
                # inferred from same-process ownership alone. Specific title
                # or root-control rules may identify a top-level child even
                # where the OS does not expose an owner HWND.
                if relation is None and not (rule.title_pattern or rule.root_control_ids):
                    continue
            matches.append(window)
        if not matches:
            raise WindowIdentityError("window_role_not_found")
        if len(matches) != 1:
            raise WindowIdentityError("window_role_ambiguous")
        return matches[0]

    def verify(self, identity: WindowIdentity) -> bool:
        current = self._windows.get(identity.native_id)
        return current is not None and current.binding == identity.binding and current.visible

    def focus_and_lock(self, identity: WindowIdentity) -> WindowIdentity:
        if not self.verify(identity):
            raise WindowIdentityError("window_identity_stale")
        if self._focus_callback is not None and not self._focus_callback(identity):
            raise WindowIdentityError("window_focus_failed")
        current = self._windows[identity.native_id]
        self._lock = current
        return current

    def wait_transition(self, before: WindowIdentity | None, expected: str,
                        profile, refresh: Callable[[], Iterable[Mapping]],
                        timeout: float = 5.0, interval: float = 0.1) -> WindowIdentity:
        deadline = time.monotonic() + timeout
        while True:
            self.reconcile(refresh())
            try:
                resolved = self.resolve_unique(expected, profile)
                if before is None or resolved.binding != before.binding:
                    return resolved
            except WindowIdentityError as exc:
                if exc.code == "window_role_ambiguous":
                    raise
            if time.monotonic() >= deadline:
                raise WindowIdentityError("window_transition_timeout")
            time.sleep(interval)
