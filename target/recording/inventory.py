"""Whole-window control capture, off the correlation path.

A recording's events name one control each: the one the user acted on. That is
enough to replay the click, and not enough to *generate* anything — a MAA node
table or a pytest script written from the recording can only refer to controls
the recording knows exist. This module captures the complete control tree of
each window the flow visits, so the generated trace is written against the
whole page rather than the handful of controls that happened to be touched.

Two properties matter more than completeness here:

* It never runs on the correlator's worker thread. Correlation hit-tests the
  live UI, so delaying it by the ~0.4-1.2s a tree walk costs would resolve
  input against a screen that has already moved on. Capture therefore owns a
  thread, and a slow walk delays only the next walk.
* It is debounced and deduplicated. A window is walked when the flow enters it
  and after the input settles, not once per event, and an unchanged tree is
  not stored twice.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from typing import Any, Mapping

#: Fields kept for each control. The walk reports more than this; what is
#: dropped is either per-run noise (`depth`) or unusable for identifying a
#: control later.
_CONTROL_FIELDS = (
    ("automation_id", "automationId"),
    ("control_type", "controlType"),
    ("class_name", "className"),
    ("text", "text"),
    ("control_id", "controlId"),
    ("is_enabled", "enabled"),
    ("is_visible", "visible"),
    ("is_password", "protected"),
)

#: Content-identity fields. A window that merely moved or resized is not new
#: material — the generated trace resolves controls by identity, not by
#: coordinate — so geometry is recorded but does not trigger a new snapshot.
_IDENTITY_FIELDS = (
    "automationId", "controlType", "className", "text", "enabled", "visible",
)


def _rect(value: object) -> list[int] | None:
    if not isinstance(value, Mapping):
        return None
    try:
        x, y = int(value["x"]), int(value["y"])
        return [x, y, x + int(value["w"]), y + int(value["h"])]
    except Exception:
        return None


def normalize_controls(controls: object) -> list[dict[str, Any]]:
    """Project a raw tree walk onto the fields a trace can be written from."""
    normalized: list[dict[str, Any]] = []
    for source in controls if isinstance(controls, (list, tuple)) else ():
        if not isinstance(source, Mapping):
            continue
        control: dict[str, Any] = {}
        for raw_key, key in _CONTROL_FIELDS:
            value = source.get(raw_key)
            if value in (None, ""):
                continue
            control[key] = value
        if control.get("protected"):
            # A password field's contents must not reach the recording
            # document. The control is still listed — a generated script has
            # to be able to name the field it types into.
            control.pop("text", None)
        rect = _rect(source.get("rectangle") or source.get("rect"))
        if rect is not None:
            control["rect"] = rect
        depth = source.get("depth")
        if isinstance(depth, int) and not isinstance(depth, bool):
            control["depth"] = depth
        if control:
            normalized.append(control)
    return normalized


def content_hash(controls: list[Mapping[str, Any]]) -> str:
    payload = [
        {key: control.get(key) for key in _IDENTITY_FIELDS if key in control}
        for control in controls
    ]
    digest = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode()
    ).hexdigest()
    return "sha256:" + digest


def _root_identity(controls: list[Mapping[str, Any]]) -> str | None:
    """The window's own name, taken from the first component of its ids.

    Same invariant the selectors use: a title changes with the interface
    language and with what the window is showing, an automation-id root does
    not.
    """
    for control in controls:
        identifier = str(control.get("automationId") or "")
        if "." in identifier:
            root = identifier.split(".", 1)[0]
            if root:
                return root
    for control in controls:
        identifier = str(control.get("automationId") or "")
        if identifier:
            return identifier
    return None


class ControlInventory:
    """Capture complete control trees on a thread of its own."""

    def __init__(
        self,
        resolver: object,
        *,
        max_depth: int = 12,
        debounce_ms: int = 1500,
        max_snapshots: int = 48,
        stop_timeout: float = 5.0,
    ) -> None:
        if max_depth < 1:
            raise ValueError("max_depth must be positive")
        if debounce_ms < 0:
            raise ValueError("debounce_ms must be non-negative")
        if max_snapshots < 1:
            raise ValueError("max_snapshots must be positive")
        self._resolver = resolver
        self._max_depth = max_depth
        self._debounce_ms = debounce_ms
        self._max_snapshots = max_snapshots
        self._stop_timeout = stop_timeout
        self._lock = threading.Lock()
        self._wake = threading.Condition(self._lock)
        self._pending: dict[int, dict[str, Any]] = {}
        self._thread: threading.Thread | None = None
        self._stopping = False
        self._counter = 0
        self._snapshots: list[dict[str, Any]] = []
        self._hashes: dict[str, str] = {}
        #: Walks that were asked for but never taken, and why. A snapshot
        #: table that is simply short is indistinguishable from a flow that
        #: visited fewer windows, which is exactly the ambiguity that made the
        #: last recording hard to explain.
        self.skipped: dict[str, int] = {}
        self.errors: list[str] = []

    # -- request side ---------------------------------------------------

    def request(
        self,
        *,
        handle: object,
        title: str = "",
        process_name: str = "",
        monotonic_ms: int | None = None,
        reason: str = "step",
    ) -> None:
        """Ask for a walk of one window. Never blocks, never raises.

        Called from the correlator's worker, so it does exactly one lock
        acquisition and no UI work.
        """
        if not isinstance(handle, int) or isinstance(handle, bool) or handle <= 0:
            self._skip("no_window_handle")
            return
        now_ms = int(time.monotonic() * 1000)
        # A window the flow has just entered is walked at once: what changed is
        # the window itself, and waiting out a debounce would miss a page that
        # is closed again before it expires. Ordinary input is debounced —
        # a click and the six that follow it describe one settled page.
        due_ms = now_ms if reason == "window_changed" else now_ms + self._debounce_ms
        with self._wake:
            if self._stopping:
                return
            existing = self._pending.get(handle)
            if existing is not None:
                existing["dueMs"] = min(int(existing["dueMs"]), due_ms)
                if reason == "window_changed":
                    existing["reason"] = reason
                if title:
                    existing["title"] = title
                if process_name:
                    existing["processName"] = process_name
            else:
                self._pending[handle] = {
                    "handle": handle,
                    "title": title,
                    "processName": process_name,
                    "reason": reason,
                    "dueMs": due_ms,
                    "requestedAtMs": monotonic_ms,
                }
            self._wake.notify()

    def _skip(self, reason: str) -> None:
        self.skipped[reason] = self.skipped.get(reason, 0) + 1

    # -- lifecycle ------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("control inventory already started")
        self._stopping = False
        self._thread = threading.Thread(
            target=self._run, name="edr-wd-recording-inventory", daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        thread = self._thread
        if thread is None:
            return
        with self._wake:
            self._stopping = True
            self._wake.notify_all()
        # A walk already in flight is allowed to finish, but never at the cost
        # of the whole stop: the snapshots collected so far are intact either
        # way, and a recording must not fail because one tree was slow.
        thread.join(timeout=self._stop_timeout)
        if thread.is_alive():
            self.errors.append(
                f"control inventory did not stop within {self._stop_timeout:g}s"
            )
        self._thread = None

    def _run(self) -> None:
        while True:
            with self._wake:
                while True:
                    if self._stopping and not self._pending:
                        return
                    now_ms = int(time.monotonic() * 1000)
                    due = [
                        request for request in self._pending.values()
                        # A stop takes everything still queued: the last page
                        # the user reached is the one a trace most needs, and
                        # it is always the one whose debounce has not expired.
                        if self._stopping or int(request["dueMs"]) <= now_ms
                    ]
                    if due:
                        break
                    if self._pending:
                        wait = min(
                            int(request["dueMs"]) for request in self._pending.values()
                        ) - now_ms
                        self._wake.wait(timeout=max(wait, 1) / 1000)
                    else:
                        self._wake.wait(timeout=0.5)
                for request in due:
                    self._pending.pop(int(request["handle"]), None)
            for request in due:
                self._capture(request)

    # -- capture side ---------------------------------------------------

    def _capture(self, request: Mapping[str, Any]) -> None:
        walk = getattr(self._resolver, "control_tree", None)
        if not callable(walk):
            self._skip("resolver_cannot_walk")
            return
        started = time.monotonic()
        try:
            tree = walk(int(request["handle"]), self._max_depth)
        except Exception as exc:
            self.errors.append(f"{type(exc).__name__}: {exc}")
            self._skip("walk_failed")
            return
        if not isinstance(tree, Mapping) or not tree.get("ok"):
            self._skip("walk_failed")
            return
        controls = normalize_controls(tree.get("controls"))
        if not controls:
            self._skip("no_controls")
            return
        digest = content_hash(controls)
        root = _root_identity(controls)
        key = root or f"handle:{int(request['handle'])}"
        with self._lock:
            if self._hashes.get(key) == digest:
                # Unchanged since the last walk of this window. The earlier
                # snapshot still describes it, so consumers joining by time
                # keep resolving to that one.
                self._skip("unchanged")
                return
            if len(self._snapshots) >= self._max_snapshots:
                self._skip("snapshot_limit")
                return
            self._hashes[key] = digest
            self._counter += 1
            window: dict[str, Any] = {
                "handle": int(request["handle"]),
                "title": str(tree.get("title") or request.get("title") or ""),
                "processName": str(request.get("processName") or ""),
            }
            if root:
                window["rootAutomationId"] = root
            self._snapshots.append({
                "snapshotId": f"CS-{self._counter:04d}",
                "capturedAtMs": int(time.monotonic() * 1000),
                "reason": str(request.get("reason") or "step"),
                "durationMs": int((time.monotonic() - started) * 1000),
                "window": window,
                "contentHash": digest,
                "controlCount": len(controls),
                "controls": controls,
            })

    @property
    def snapshots(self) -> tuple[Mapping[str, Any], ...]:
        with self._lock:
            return tuple(dict(snapshot) for snapshot in self._snapshots)

    def diagnostics(self) -> dict[str, Any]:
        with self._lock:
            return {
                "snapshotCount": len(self._snapshots),
                "skipped": dict(self.skipped),
                "errors": list(self.errors[-8:]),
            }


__all__ = ["ControlInventory", "normalize_controls", "content_hash"]
