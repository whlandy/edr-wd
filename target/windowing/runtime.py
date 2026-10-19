"""Bind the platform backend's lock to the session-owned window registry."""

from __future__ import annotations

import re
import threading
import ntpath
import os
from dataclasses import replace

import psutil

from .models import WindowIdentity, WindowIdentityError
from .registry import WindowRegistry


class WindowSession:
    def __init__(self, session_id: str, backend_kind: str, backend):
        self.backend = backend
        self.backend_kind = backend_kind
        self.registry = WindowRegistry(session_id, backend_kind)
        self._mutex = threading.RLock()

    def refresh(self, *, require_enumeration: bool = False) -> tuple[WindowIdentity, ...]:
        with self._mutex:
            if self.backend_kind == "windows_pywinauto":
                source = self.backend._enumerate_top_windows_windows(
                    strict=require_enumeration,
                ) if require_enumeration else self.backend._enumerate_top_windows_windows()
            elif self.backend_kind == "macos_accessibility":
                source = self.backend._list_windows_cg(
                    strict=require_enumeration,
                ) if require_enumeration else self.backend._list_windows_cg()
            else:
                raise WindowIdentityError("window_backend_unsupported")
            normalized = []
            for item in source:
                handle = item.get("handle")
                pid = item.get("pid")
                rect = item.get("rect") or item.get("rectangle")
                if handle is None or pid is None or not rect:
                    continue
                process_name = item.get("process_name") or item.get("app_name") or ""
                if self.backend_kind == "macos_accessibility":
                    # CGWindowOwnerName is localized (e.g. "计算器"), while a
                    # registered product role names the executable ("Calculator").
                    # PID is the exact join; never infer ownership from the
                    # translated display label alone.
                    try:
                        import psutil
                        process_name = psutil.Process(int(pid)).name()
                    except Exception as exc:
                        if require_enumeration:
                            raise WindowIdentityError("window_inventory_unavailable") from exc
                normalized.append({
                    "handle": handle, "pid": pid,
                    "process_name": process_name,
                    "title": item.get("title") or item.get("window_title") or "",
                    "rect": rect, "visible": item.get("visible", True),
                    "root_control_id": item.get("root_control_id") or "",
                    "class_name": item.get("class_name") or "",
                    "parent_native_id": item.get("parent_native_id"),
                })
            self.registry.reconcile(normalized)
            return self.registry.list()

    def _owned_product_windows(self, capability, application: dict,
                               process_exe=None) -> tuple[WindowIdentity, ...]:
        """Join live PIDs to installed paths before role or selector matching."""
        windows = self.refresh(require_enumeration=True)
        paths = [application.get(key) for key in ("path", "entry_path")]
        paths = [path for path in paths if isinstance(path, str) and path]
        if not paths:
            raise WindowIdentityError("product_window_path_unavailable")
        resolve_exe = process_exe or (lambda pid: psutil.Process(pid).exe())
        candidate_names = {
            name.lower() for rule in capability.window_roles
            for name in rule.process_names
        }

        def owned(window: WindowIdentity) -> bool:
            try:
                actual = resolve_exe(window.process_id)
            except (OSError, psutil.Error):
                if window.process_name.lower() in candidate_names:
                    raise WindowIdentityError("product_window_ownership_unverifiable")
                return False
            if not isinstance(actual, str) or not actual:
                if window.process_name.lower() in candidate_names:
                    raise WindowIdentityError("product_window_ownership_unverifiable")
                return False
            if self.backend_kind == "windows_pywinauto":
                normalized = ntpath.normcase(ntpath.normpath(actual))
                return any(normalized == ntpath.normcase(ntpath.normpath(path))
                           for path in paths)
            normalized = os.path.realpath(actual)
            return any(normalized == os.path.realpath(path)
                       or normalized.startswith(os.path.realpath(path).rstrip(os.sep) + os.sep)
                       for path in paths)

        return tuple(window for window in windows if window.visible and owned(window))

    def observe_product_windows(self, capability, application: dict,
                                process_exe=None) -> tuple[WindowIdentity, ...]:
        """Enumerate path-owned registered windows without changing the lock."""
        with self._mutex:
            owned_windows = self._owned_product_windows(capability, application, process_exe)
            by_role: dict[str, list[WindowIdentity]] = {}
            assigned: set[str] = set()
            for rule in capability.window_roles:
                matches = []
                for window in owned_windows:
                    if window.native_id in assigned:
                        continue
                    if rule.process_names and window.process_name.lower() not in {
                        name.lower() for name in rule.process_names
                    }:
                        continue
                    if rule.title_pattern and not re.search(rule.title_pattern, window.title):
                        continue
                    if rule.root_control_ids and window.root_control_id not in rule.root_control_ids:
                        continue
                    if rule.parent_role:
                        parents = by_role.get(rule.parent_role, [])
                        if window.parent_native_id is not None:
                            if not any(parent.native_id == window.parent_native_id
                                       for parent in parents):
                                continue
                        elif not (rule.title_pattern or rule.root_control_ids):
                            # A broad dialog role cannot claim every same-PID
                            # top-level window without a native parent edge.
                            continue
                    matches.append(window)
                if len(matches) > 1:
                    raise WindowIdentityError("window_role_ambiguous")
                by_role[rule.role] = matches
                assigned.update(window.native_id for window in matches)
            if any(window.native_id not in assigned for window in owned_windows):
                # An omitted or dynamically retitled product window must not
                # disappear from a negative window assertion as if it closed.
                raise WindowIdentityError("product_window_unclassified")
            return tuple(replace(window, role=role)
                         for role, matches in by_role.items() for window in matches)

    def bind_backend_lock(self) -> WindowIdentity:
        with self._mutex:
            windows = self.refresh()
            lock = self.backend.get_window_lock().get("lock") or {}
            snapshot = lock.get("snapshot") or {}
            handle = lock.get("handle") or snapshot.get("handle")
            pid = lock.get("pid") or snapshot.get("pid")
            matched = [item for item in windows if item.native_id == str(handle)
                       and item.process_id == pid]
            if len(matched) != 1:
                raise WindowIdentityError("window_identity_stale")
            identity = self.registry.focus_and_lock(matched[0])
            lock["generation"] = identity.generation
            lock["root_control_id"] = identity.root_control_id
            if isinstance(snapshot, dict):
                snapshot["root_control_id"] = identity.root_control_id
            return identity

    def verify_locked(self) -> WindowIdentity:
        with self._mutex:
            self.refresh()
            identity = self.registry.locked
            if identity is None or not self.registry.verify(identity):
                raise WindowIdentityError("window_identity_stale")
            lock = self.backend.get_window_lock().get("lock") or {}
            if (str(lock.get("handle") or (lock.get("snapshot") or {}).get("handle")) != identity.native_id
                or lock.get("pid") != identity.process_id
                or int(lock.get("generation") or 0) != identity.generation):
                raise WindowIdentityError("window_identity_stale")
            return identity

    def focus_and_lock_role(self, role: str, capability, run_token: str) -> WindowIdentity:
        try:
            from ..product_policy import authorized_dispatch
        except ImportError:
            from product_policy import authorized_dispatch
        with self._mutex, authorized_dispatch(run_token):
            self.refresh()
            identity = self.registry.resolve_unique(role, capability)
            return self._focus_and_lock_identity(identity)

    def focus_and_lock_selector(
        self, process_name: str, title_regex: str, root_control_id: str,
        capability, application: dict, run_token: str, process_exe=None,
    ) -> WindowIdentity:
        """Resolve a recorded stable root within path-owned product windows."""
        try:
            from ..product_policy import authorized_dispatch
        except ImportError:
            from product_policy import authorized_dispatch
        if not isinstance(root_control_id, str) or not root_control_id:
            raise WindowIdentityError("window_root_identity_required")
        if not isinstance(process_name, str) or not process_name or not isinstance(title_regex, str):
            raise WindowIdentityError("window_selector_invalid")
        try:
            title_pattern = re.compile(title_regex) if title_regex else None
        except re.error as exc:
            raise WindowIdentityError("window_selector_invalid") from exc
        with self._mutex, authorized_dispatch(run_token):
            windows = self._owned_product_windows(capability, application, process_exe)
            candidates = []
            for window in windows:
                if (window.process_name.lower() != process_name.lower()
                        or window.root_control_id != root_control_id
                        or (title_pattern is not None and not title_pattern.search(window.title))):
                    continue
                if not any(
                    (not rule.process_names or window.process_name.lower() in {
                        name.lower() for name in rule.process_names
                    })
                    and (not rule.title_pattern or re.search(rule.title_pattern, window.title))
                    and (not rule.root_control_ids or window.root_control_id in rule.root_control_ids)
                    for rule in capability.window_roles
                ):
                    continue
                candidates.append(window)
            if not candidates:
                raise WindowIdentityError("window_selector_not_found")
            if len(candidates) != 1:
                raise WindowIdentityError("window_selector_ambiguous")
            if self.backend_kind == "windows_pywinauto" and not callable(
                getattr(self.backend, "connect_window_identity", None)
            ):
                raise WindowIdentityError("window_identity_unavailable")
            return self._focus_and_lock_identity(candidates[0])

    def _focus_and_lock_identity(self, identity: WindowIdentity) -> WindowIdentity:
        title_re = "^" + re.escape(identity.title) + "$" if identity.title else None
        exact_connect = getattr(self.backend, "connect_window_identity", None)
        if self.backend_kind == "windows_pywinauto" and callable(exact_connect):
            connected = exact_connect(int(identity.native_id), identity.process_id)
        else:
            connected = self.backend.connect(pid=identity.process_id, title_re=title_re,
                                             process_name=identity.process_name)
        if not connected.get("ok"):
            raise WindowIdentityError("window_focus_failed")
        locked = self.backend.lock_window(pid=identity.process_id,
                                          title_re=title_re,
                                          process_name=identity.process_name,
                                          strict=True, activate=True)
        if not locked.get("ok"):
            raise WindowIdentityError("window_focus_failed")
        actual = self.bind_backend_lock()
        if actual.binding != identity.binding:
            self.backend.unlock_window()
            raise WindowIdentityError("window_identity_stale")
        return actual
