"""Capture one self-consistent locked-window tree and redacted frame."""

from __future__ import annotations

import base64
import hashlib
import json
import time
import uuid
from typing import Any, Callable, Mapping

from .models import WindowIdentity, WindowIdentityError


def normalize_observed_control(source: Mapping, identity: Mapping,
                               index: int) -> dict:
    """One target-safe control projection used on both sides of MCP."""
    raw_rect = source.get("rect") or source.get("rectangle")
    if isinstance(raw_rect, Mapping):
        x = int(raw_rect.get("left", raw_rect.get("x", 0)))
        y = int(raw_rect.get("top", raw_rect.get("y", 0)))
        rect = [x, y, int(raw_rect.get("right", x + int(raw_rect.get("w", raw_rect.get("width", 0))))),
                int(raw_rect.get("bottom", y + int(raw_rect.get("h", raw_rect.get("height", 0)))))]
    elif isinstance(raw_rect, (list, tuple)) and len(raw_rect) == 4:
        rect = list(map(int, raw_rect))
    else:
        rect = None
    protected = source.get("protected", source.get("is_password"))
    item = {
        "target_id": f"T{index:04d}",
        "process_name": identity.get("process_name") or "unknown",
        "window_title": identity.get("title") or "",
        "control_type": source.get("control_type") or source.get("role") or source.get("class_name"),
        "automation_id": source.get("automation_id") or source.get("auto_id"),
        "control_id": source.get("control_id"), "class_name": source.get("class_name"),
        "identifier": source.get("identifier"),
        "text": None if protected is True else (source.get("text") or source.get("name") or source.get("title")),
        "rect": rect,
        "value": None if protected is not False else source.get("value"),
        "checked": source.get("checked", source.get("toggle_state")),
        "enabled": source.get("enabled", source.get("is_enabled")),
        "protected": protected, "ancestry": source.get("ancestry") or [],
    }
    item["fingerprint"] = "sha256:" + hashlib.sha256(
        json.dumps({key: item[key] for key in (
            "process_name", "window_title", "control_type", "automation_id",
            "control_id", "class_name", "identifier", "text", "rect",
        )}, ensure_ascii=False, sort_keys=True, default=str).encode()
    ).hexdigest()
    return item

try:
    from ..product_policy import authorized_dispatch
except ImportError:  # target-local deployment
    from product_policy import authorized_dispatch


def _identity_from_lock(backend: Any, session_id: str, backend_kind: str) -> WindowIdentity:
    verified = backend.verify_window_lock(activate=False)
    if not isinstance(verified, Mapping) or not verified.get("ok") or not verified.get("locked"):
        raise WindowIdentityError("window_identity_stale")
    lock = backend.get_window_lock().get("lock")
    if not isinstance(lock, Mapping):
        raise WindowIdentityError("window_identity_stale")
    snapshot = dict(lock.get("snapshot") or {})
    snapshot.setdefault("handle", lock.get("handle"))
    snapshot.setdefault("pid", lock.get("pid"))
    snapshot.setdefault("process_name", lock.get("process_name"))
    snapshot.setdefault("native_id", lock.get("native_id"))
    snapshot.setdefault("root_control_id", lock.get("root_control_id"))
    return WindowIdentity.from_native(snapshot, session_id=session_id,
                                      backend=backend_kind,
                                      generation=int(lock.get("generation") or 1))


def observe_window_bundle(backend: Any, *, session_id: str, backend_kind: str,
                          run_token: str | None, capture_image: bool,
                          include_ocr: bool, capture_redacted_frame: Callable,
                          ocr_extract: Callable | None = None,
                          max_duration: float = 60.0) -> dict:
    """Reject the whole observation if identity or geometry changes mid-capture."""
    started = time.monotonic()
    with authorized_dispatch(run_token):
        before = _identity_from_lock(backend, session_id, backend_kind)
        tree = backend.dump_tree(max_depth=15)
        if not isinstance(tree, Mapping) or not tree.get("ok") or not isinstance(tree.get("controls"), list):
            raise ValueError("observation_tree_unavailable")
        png = None
        metadata = {}
        ocr_boxes = []
        ocr_status = "not_requested"
        if capture_image or include_ocr:
            png, metadata = capture_redacted_frame(backend)
            if metadata.get("redacted") is not True or metadata.get("capture_scope", "window") != "window":
                raise ValueError("observation_capture_unredacted")
            if include_ocr:
                if ocr_extract is None:
                    ocr_status = "unsupported"
                else:
                    try:
                        ocr_boxes = ocr_extract(png, tuple(metadata.get("redactions") or ()))
                        ocr_status = "ok"
                    except ValueError as exc:
                        if str(exc) != "ocr_unsupported":
                            raise
                        ocr_status = "unsupported"
        after = _identity_from_lock(backend, session_id, backend_kind)
        if before.binding != after.binding or before.rect != after.rect:
            raise WindowIdentityError("observation_window_changed")
        duration = time.monotonic() - started
        if duration > max_duration:
            raise ValueError("observation_stale")
        observation_id = "OBS-" + uuid.uuid4().hex
        origin = metadata.get("origin") or [0, 0]
        normalized_ocr = []
        for box in ocr_boxes:
            x0, y0, x1, y1 = map(int, box["rect"])
            normalized_ocr.append({**box,
                                   "rect": [x0 + origin[0], y0 + origin[1],
                                            x1 + origin[0], y1 + origin[1]],
                                   "observation_id": observation_id,
                                   "window_native_id": after.native_id})
        result = {
            "ok": True, "observation_id": observation_id,
            "captured_at_monotonic": started, "duration_seconds": duration,
            "window_identity": after.__dict__, "tree": dict(tree),
            "ocr_boxes": normalized_ocr, "ocr_status": ocr_status,
        }
        if png is not None:
            result["screenshot_scope"] = "window"
            result["screenshot_redacted"] = True
            result["screenshot_sha256"] = "sha256:" + hashlib.sha256(png).hexdigest()
            result["screenshot_origin"] = metadata.get("origin")
            result["screenshot_redactions"] = metadata.get("redactions") or []
            if capture_image:
                result["screenshot_b64"] = base64.b64encode(png).decode("ascii")
        return result
