# -*- coding: utf-8 -*-
"""Legacy observation path must refuse a frame not captured from the window.

`refresh_bundle` has always validated `screenshot_origin` against the
observed window rectangle (±32px UIA frame skew). The legacy `refresh()`
path — used by replays without a product-run token — stored the frame and
its origin after checking only that the origin was an int pair, so a stale
frame (window moved between dump and capture) or a different window's
frame could persist into an execution report as "evidence". The gate now
lives at the provider boundary: the same ±32px comparison, before the
frame is attached to any snapshot.
"""

import base64

import pytest

from agent.execution import BackendUnavailable
from agent.recording.mcp_runtime import MCPObservationProvider

pytestmark = pytest.mark.unit

PNG = base64.b64encode(b"\x89PNG\r\n\x1a\nstub").decode("ascii")


class _Agent:
    """MCP agent fake: healthy lock, tree, redacted window capture."""

    def __init__(self, *, lock_rect=None, capture_origin=(80, 14)):
        self._lock_rect = lock_rect
        self._capture_origin = list(capture_origin) if capture_origin else None

    def call_tool(self, name, arguments, timeout=None):
        if name == "verify_window_lock":
            return {"ok": True}
        if name == "get_window_lock":
            lock = {"process_name": "EDRClient.exe",
                    "snapshot": {"process_name": "EDRClient.exe",
                                 "title": "EDRClient"}}
            if self._lock_rect is not None:
                x, y, w, h = self._lock_rect
                lock["snapshot"]["rectangle"] = {"x": x, "y": y, "w": w, "h": h}
            return {"ok": True, "lock": lock}
        if name == "dump_tree":
            return {"ok": True, "backend": "windows_pywinauto", "controls": [{
                "automation_id": "btnApply", "control_type": "Button",
                "text": "应用", "rectangle": {"x": 10, "y": 20, "w": 100, "h": 40},
                "is_enabled": True,
            }]}
        if name == "replay_capture":
            if self._capture_origin is None:
                return {"ok": False, "error": "capture refused"}
            return {"ok": True, "capture_scope": "window", "redacted": True,
                    "image_b64": PNG, "origin": self._capture_origin,
                    "redactions": []}
        raise AssertionError(name)


def test_moving_capture_within_frame_skew_is_accepted():
    provider = MCPObservationProvider(
        _Agent(lock_rect=(80, 14, 920, 600), capture_origin=(90, 30)),
        capture_screenshot=True,
    )
    snapshot_id = provider.refresh()
    snapshot = provider.get_snapshot(snapshot_id)
    assert snapshot["screenshot_origin"] == [90, 30]
    assert snapshot["screenshot_bytes"].startswith(b"\x89PNG")


def test_capture_origin_far_from_window_rect_is_refused_before_persisting():
    # Window observed at [80,14]; the capture reports origin [400,14] —
    # e.g. the frame was taken before the window was moved. >32px skew.
    provider = MCPObservationProvider(
        _Agent(lock_rect=(80, 14, 920, 600), capture_origin=(400, 14)),
        capture_screenshot=True,
    )
    with pytest.raises(BackendUnavailable, match="origin does not match"):
        provider.refresh()
    assert provider._snapshots == {}
    assert provider._latest is None


def test_capture_origin_for_window_missing_rect_is_refused():
    # The lock snapshot carries no rectangle (UIA rectangle() failed at
    # lock time): an unverifiable origin must not persist as evidence.
    provider = MCPObservationProvider(
        _Agent(lock_rect=None, capture_origin=(80, 14)),
        capture_screenshot=True,
    )
    with pytest.raises(BackendUnavailable, match="cannot be verified"):
        provider.refresh()
    assert provider._snapshots == {}


def test_non_integer_capture_origin_still_refused():
    provider = MCPObservationProvider(
        _Agent(lock_rect=(80, 14, 920, 600), capture_origin=(80.5, 14)),
        capture_screenshot=True,
    )
    with pytest.raises(BackendUnavailable, match="origin is invalid"):
        provider.refresh()
    assert provider._snapshots == {}


def test_semantic_only_refresh_without_capture_does_not_require_origin():
    # No capture requested: no origin exists, nothing to verify — the
    # observation must still succeed.
    provider = MCPObservationProvider(_Agent())
    snapshot_id = provider.refresh()
    snapshot = provider.get_snapshot(snapshot_id)
    assert "screenshot_bytes" not in snapshot
    assert snapshot["controls"][0]["automation_id"] == "btnApply"
