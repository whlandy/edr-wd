"""Target-local, source-redacted screenshot evidence for desktop recording.

Raw frames live only long enough to enumerate protected rectangles and paint
them.  Only the redacted PNG is retained in memory and exposed through the
recording capture MCP tool; recording JSON carries an opaque ID and digest.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import io
import threading
import time
import uuid
from dataclasses import replace
from typing import Any, Mapping

from PIL import Image, ImageDraw

from .models import RawCaptureEvent


_CAPTURE_EVENT_TYPES = frozenset({
    "pointer_click", "pointer_double_click", "text_commit", "selection_change",
    "toggle_change", "scroll_commit", "drag_commit", "assertion",
})


def _rectangle(control: Mapping[str, Any]) -> tuple[int, int, int, int] | None:
    raw = control.get("rectangle") or control.get("rect")
    if isinstance(raw, Mapping) and all(key in raw for key in ("x", "y", "w", "h")):
        x, y = int(raw["x"]), int(raw["y"])
        return x, y, x + int(raw["w"]), y + int(raw["h"])
    if isinstance(raw, (list, tuple)) and len(raw) == 4:
        return tuple(int(value) for value in raw)
    return None


def _is_protected(control: Mapping[str, Any]) -> bool:
    if control.get("protected") is True or control.get("is_password") is True:
        return True
    role = " ".join(
        str(control.get(key) or "")
        for key in ("role", "subrole", "control_type", "class_name")
    ).lower()
    return "securetextfield" in role or "password" in role


_PROTECTED_CACHE_TTL_SECONDS = 4.0


def _window_identity(backend: Any) -> tuple:
    """Which window a cached redaction answer belongs to."""
    snapshot = getattr(backend, "_connected_window_snapshot", None)
    if isinstance(snapshot, Mapping):
        return (snapshot.get("pid"), snapshot.get("title"))
    return (getattr(backend, "_connected_pid", None), None)


def _cached_protected_controls(backend: Any) -> list | None:
    """Run the slow enumeration, reusing a recent answer for the same window.

    The answer is kept on the backend, not in a module global: a global
    outlives the session that produced it and would hand one recording's
    redaction answer to the next.
    """
    identity = _window_identity(backend)
    now = time.monotonic()
    entry = getattr(backend, "_edr_protected_cache", None)
    if (
        isinstance(entry, dict)
        and entry.get("identity") == identity
        and now - entry.get("at", 0.0) < _PROTECTED_CACHE_TTL_SECONDS
    ):
        if entry.get("failed"):
            # Remember the failure too. Some applications refuse this
            # enumeration and only reveal it by timing out after 20 s; retrying
            # per step would charge every step that timeout again.
            raise ValueError("protected-control enumeration failed")
        return entry["controls"]

    def _remember(payload: dict) -> None:
        payload.update({"identity": identity, "at": time.monotonic()})
        try:
            backend._edr_protected_cache = payload
        except Exception:
            pass  # a backend that refuses attributes simply gets no caching

    try:
        result = backend.dump_tree(max_depth=15)
    except Exception:
        _remember({"failed": True})
        raise
    if not isinstance(result, Mapping) or not result.get("ok"):
        _remember({"failed": True})
        raise ValueError("protected-control enumeration failed")
    tree_controls = result.get("controls")
    if not isinstance(tree_controls, list):
        raise ValueError("protected-control enumeration returned no controls array")
    controls = [
        control for control in tree_controls
        if isinstance(control, Mapping) and _is_protected(control)
    ]
    _remember({"controls": controls})
    return controls


def protected_rectangles(backend: Any) -> list[tuple[int, int, int, int]]:
    """Rectangles that must be painted out before a frame leaves the target."""
    rectangles: list[tuple[int, int, int, int]] = []
    # Prefer a backend that can answer this directly. Deriving it from a full
    # tree dump costs 18-24 s per call on macOS — and this runs after every
    # recorded step, which is what made each step take about ten seconds to
    # appear.
    native = getattr(backend, "protected_rectangles", None)
    controls = None
    if callable(native):
        try:
            controls = native()
        except Exception as exc:
            raise ValueError("protected-control enumeration failed") from exc
    if controls is None:
        # The fast path could not answer, so the slow enumeration has to run.
        # It costs 18-24 s on macOS and this is called after every recorded
        # step, which is why a recording appeared to advance one step every
        # ten seconds. Secure fields do not appear or move between adjacent
        # steps of the same page, so the answer is reused briefly and always
        # recomputed when the window changes — freshness is traded, but the
        # enumeration itself is never skipped.
        cached = _cached_protected_controls(backend)
        if cached is not None:
            controls = cached
    if controls is not None:
        for control in controls:
            if not isinstance(control, Mapping):
                continue
            rect = _rectangle(control)
            if rect is None:
                raise ValueError("protected control has no redaction rectangle")
            rectangles.append(rect)
        # Falls through to the recorder-window mask below: skipping it would
        # leave the recorder's own UI visible in stored evidence.
        result = None
    result = None
    if result is not None:
        if not isinstance(result, Mapping) or not result.get("ok"):
            raise ValueError("protected-control enumeration failed")
        tree_controls = result.get("controls")
        if not isinstance(tree_controls, list):
            raise ValueError("protected-control enumeration returned no controls array")
        for control in tree_controls:
            if not isinstance(control, Mapping) or not _is_protected(control):
                continue
            rect = _rectangle(control)
            if rect is None:
                raise ValueError(
                    "protected control has no redaction rectangle"
                )
            rectangles.append(rect)

    # The recorder indicator is a separate process/window and therefore
    # is not part of the connected application's control tree.  Mask it
    # explicitly when a platform screenshot API can see overlapping or
    # full-screen windows.
    # Prefer a backend that can locate its own recorder windows directly:
    # deriving this from a full window enumeration costs 5.3 s per call on
    # macOS, paid after every recorded step.
    fast = getattr(backend, "recorder_ui_rectangles", None)
    if callable(fast):
        try:
            recorder_windows = fast()
        except Exception as exc:
            raise ValueError("recorder-window enumeration failed") from exc
        if recorder_windows is not None:
            for window in recorder_windows:
                if not isinstance(window, Mapping):
                    continue
                rect = _rectangle(window)
                if rect is None:
                    raise ValueError("recorder window has no redaction rectangle")
                rectangles.append(rect)
            return rectangles
    try:
        windows = backend.list_windows()
    except Exception as exc:
        raise ValueError("recorder-window enumeration failed") from exc
    if not isinstance(windows, Mapping) or not windows.get("ok"):
        raise ValueError("recorder-window enumeration failed")
    window_items = windows.get("windows")
    if not isinstance(window_items, list):
        raise ValueError("recorder-window enumeration returned no windows array")
    for window in window_items:
        if not isinstance(window, Mapping):
            continue
        title = str(
            window.get("title") or window.get("window_title")
            or window.get("name") or ""
        )
        if "[recorder_ui=true]" not in title:
            continue
        rect = _rectangle(window)
        if rect is None:
            raise ValueError("recorder window has no redaction rectangle")
        rectangles.append(rect)
    return rectangles


def capture_redacted_window_frame(
    backend: Any,
    *,
    extra_redactions: tuple[tuple[int, int, int, int], ...] = (),
) -> tuple[bytes, dict[str, Any]]:
    """Return one source-redacted, locked-window PNG and its metadata.

    This is the single source-redaction boundary on the target: recording
    evidence and replay evidence both go through it, so no runtime frame can
    reach an execution report with protected regions still visible.
    """
    protected = list(protected_rectangles(backend))
    protected.extend(extra_redactions)
    result = backend.screenshot(None)
    if not isinstance(result, Mapping) or not result.get("ok"):
        raise ValueError("recording screenshot unavailable")
    if result.get("capture_scope") != "window":
        raise ValueError("recording screenshot must be scoped to the locked window")
    encoded = result.get("image_b64") or result.get("image_base64")
    if not isinstance(encoded, str):
        raise ValueError("recording screenshot returned no in-memory PNG")
    try:
        raw_png = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("recording screenshot base64 is invalid") from exc
    if not raw_png.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("recording screenshot is not PNG")

    image = Image.open(io.BytesIO(raw_png)).convert("RGB")
    origin_raw = result.get("origin")
    if (
        not isinstance(origin_raw, (list, tuple))
        or len(origin_raw) != 2
        or not all(
            isinstance(value, int) and not isinstance(value, bool)
            for value in origin_raw
        )
    ):
        raise ValueError("recording screenshot origin is invalid")
    origin = int(origin_raw[0]), int(origin_raw[1])

    local_redactions: list[tuple[int, int, int, int]] = []
    painter = ImageDraw.Draw(image)
    for rect in protected:
        local = (
            max(0, rect[0] - origin[0]),
            max(0, rect[1] - origin[1]),
            min(image.width, rect[2] - origin[0]),
            min(image.height, rect[3] - origin[1]),
        )
        if local[0] >= local[2] or local[1] >= local[3]:
            continue
        painter.rectangle(local, fill=(0, 0, 0))
        local_redactions.append(local)

    output = io.BytesIO()
    image.save(output, format="PNG")
    redacted_png = output.getvalue()
    return redacted_png, {
        "sha256": "sha256:" + hashlib.sha256(redacted_png).hexdigest(),
        "width": image.width,
        "height": image.height,
        "origin": list(origin),
        "redacted": True,
        "redactions": [list(rect) for rect in local_redactions],
    }


class RecordingEvidenceStore:
    """Bounded in-memory store for already-redacted PNG captures."""

    def __init__(
        self,
        backend: Any,
        *,
        max_captures: int = 256,
        max_total_bytes: int = 128 * 1024 * 1024,
    ) -> None:
        self._backend = backend
        self._max_captures = max_captures
        self._max_total_bytes = max_total_bytes
        self._captures: dict[str, bytes] = {}
        self._total_bytes = 0
        self._previous_capture: dict[str, Any] | None = None
        self.initialization_error: str | None = None
        self._lock = threading.Lock()

    def clear(self) -> None:
        with self._lock:
            self._captures.clear()
            self._total_bytes = 0
            self._previous_capture = None
            self.initialization_error = None

    def _capture_frame(
        self,
        target_rect: tuple[int, int, int, int] | None = None,
    ) -> dict[str, Any]:
        # Enumerate protected regions before taking the frame.  The hook
        # action has already completed, so this observes the post-action UI.
        redacted_png, metadata = capture_redacted_window_frame(
            self._backend,
            extra_redactions=(target_rect,) if target_rect is not None else (),
        )
        capture_id = "CAP-" + uuid.uuid4().hex
        with self._lock:
            if len(self._captures) >= self._max_captures:
                raise ValueError("recording capture count limit reached")
            if self._total_bytes + len(redacted_png) > self._max_total_bytes:
                raise ValueError("recording capture byte limit reached")
            self._captures[capture_id] = redacted_png
            self._total_bytes += len(redacted_png)
        return {"id": capture_id, **metadata}

    def initialize(self) -> dict[str, Any]:
        """Capture the sole INIT/before frame for the first lifecycle step."""
        try:
            baseline = self._capture_frame()
        except Exception as exc:
            self.initialization_error = str(exc)
            return {
                "ok": False,
                "code": "recording_capture_evidence_unavailable",
                "error": str(exc),
            }
        with self._lock:
            self._previous_capture = dict(baseline)
        return {"ok": True, "capture": dict(baseline)}

    def attach(self, event: RawCaptureEvent) -> RawCaptureEvent:
        """Attach one after frame and reuse the prior after as this step's before."""
        if event.type not in _CAPTURE_EVENT_TYPES:
            return event
        try:
            protected_target = (
                tuple(event.observed_target.rect)
                if event.observed_target is not None
                and event.observed_target.protected is True
                and event.observed_target.rect is not None
                else None
            )
            current = self._capture_frame(protected_target)
            with self._lock:
                previous = (
                    dict(self._previous_capture)
                    if self._previous_capture is not None else None
                )
                self._previous_capture = dict(current)
            evidence = dict(event.evidence)
            if previous is not None:
                evidence["beforeCapture"] = previous
            evidence["capture"] = current
            return replace(event, evidence=evidence)
        except Exception as exc:
            evidence = dict(event.evidence)
            evidence["captureError"] = {
                "code": "recording_capture_evidence_unavailable",
                "message": str(exc),
            }
            return replace(event, evidence=evidence)

    def get(self, capture_id: str) -> dict[str, Any]:
        with self._lock:
            payload = self._captures.get(capture_id)
        if payload is None:
            return {
                "ok": False,
                "code": "recording_capture_missing",
                "error": "recording capture does not exist",
            }
        return {
            "ok": True,
            "captureId": capture_id,
            "sha256": "sha256:" + hashlib.sha256(payload).hexdigest(),
            "image_b64": base64.b64encode(payload).decode("ascii"),
        }


__all__ = [
    "RecordingEvidenceStore",
    "capture_redacted_window_frame",
    "protected_rectangles",
]
