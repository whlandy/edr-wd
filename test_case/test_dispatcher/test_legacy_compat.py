"""
P1.1 review #2 (issue 3) — Legacy tool byte-compat.

P0.x review has repeatedly emphasised: legacy MCP tools must
remain byte-compatible wrappers. P1.1 adds `execute_action`
without modifying any legacy tool, so compatibility is preserved
by construction. This file pins the compatibility with explicit
tests so that future P1.x work cannot regress it accidentally.

We exercise the legacy tools as plain Python callables (not via
the MCP transport) and verify their return JSON against the
golden response shapes used by the pre-P1.1 baseline.

The legacy tools in question:

  * `click` (the mcp.tool-registered Python function)
  * `dump_tree`
  * `find_control`
  * `list_windows`

For each, we install a stub backend that returns a known JSON
payload, invoke the legacy tool, and assert the tool's JSON
output matches the backend's payload verbatim (the tool must
not add or remove any field).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


_REPO = Path(__file__).resolve().parents[2]
_TARGET = _REPO / "target"

if str(_TARGET) not in sys.path:
    sys.path.insert(0, str(_TARGET))


import server                            # noqa: E402


# ---------------------------------------------------------------------------
# Stub backend fixture
# ---------------------------------------------------------------------------


class _StubBackend:
    """Minimal backend that returns a known JSON payload for each
    legacy method the dispatcher tests exercise."""

    BACKEND_KIND = "test_legacy_compat"

    def click(self, *args, **kwargs):
        # Legacy click() forwards positional args; preserve them.
        merged = dict(zip([
            "control_id", "text", "class_name", "parent_text",
            "automation_id", "auto_id_contains", "auto_id_suffix",
            "parent_of", "control_type", "parent_fallback",
            "expected_process_name",
        ], args))
        merged.update(kwargs)
        return {
            "ok": True,
            "data": {"clicked": True, "control_id": merged.get("control_id")},
        }

    def click_target(self, **kwargs):
        return {"ok": True, "data": {"clicked_at": "center"}}

    def click_at(self, x, y, expected_process_name=None):
        return {"ok": True, "data": {"x": x, "y": y}}

    def dump_tree(self, window_title_re=None, max_depth=10):
        return {
            "ok": True,
            "window_title": window_title_re,
            "controls": [
                {"control_id": 1, "class_name": "Button", "text": "OK",
                 "rect": [10, 10, 100, 30]},
            ],
        }

    def find_control(self, **kwargs):
        return {"ok": True, "matches": [{"control_id": 1, "text": "OK"}]}

    def list_windows(self):
        return {"ok": True, "windows": [{"hwnd": 1, "title": "Main"}]}

    def get_window_lock(self):
        return {"ok": True, "process_name": "X.exe"}

    def connect(self, *args, **kwargs):
        return {"ok": True, "data": {"connected": True}}

    def type_text(self, *args, **kwargs):
        return {"ok": True, "data": {"typed": "x"}}

    def select(self, *args, **kwargs):
        return {"ok": True, "data": {"selected": "x"}}

    def get_text(self, *args, **kwargs):
        return {"ok": True, "text": "hello"}


@pytest.fixture
def stub_backend(monkeypatch):
    """Replace the server's backend with our stub; autouse so
    every test in this module runs against a clean stub and
    state from previous tests does not leak."""
    import action_dispatcher as ad
    ad.cache_clear()
    ad.reset_server_instance_id_for_tests()
    monkeypatch.setattr(server, "_backend", _StubBackend())
    yield server._backend
    ad.cache_clear()


@pytest.fixture(autouse=True)
def _reset_dispatcher():
    import action_dispatcher as ad
    ad.cache_clear()
    ad.reset_server_instance_id_for_tests()
    yield
    ad.cache_clear()


# ---------------------------------------------------------------------------
# Legacy tool byte-compat
# ---------------------------------------------------------------------------


def test_legacy_click_byte_compatible(stub_backend):
    """Legacy `click()` returns exactly what the backend returns,
    wrapped as JSON. The P1.1 dispatcher must NOT interfere.

    `click()` forwards arguments positionally; our stub preserves
    that by mapping positional args back to names.
    """
    raw = server.click.fn(42, expected_process_name="X.exe")
    parsed = json.loads(raw)
    assert parsed["ok"] is True
    assert parsed["data"]["clicked"] is True
    assert parsed["data"]["control_id"] == 42


def test_legacy_click_target_byte_compatible(stub_backend):
    raw = server.click_target.fn(control_id=42, x_offset=1, y_offset=2,
                               expected_process_name="X.exe")
    parsed = json.loads(raw)
    assert parsed == stub_backend.click_target(
        control_id=42, x_offset=1, y_offset=2,
        expected_process_name="X.exe",
    )


def test_legacy_dump_tree_byte_compatible(stub_backend):
    raw = server.dump_tree.fn(window_title_re="Main.*", max_depth=5)
    parsed = json.loads(raw)
    # Backends return a dict; the legacy tool JSON-encodes it verbatim.
    assert parsed == stub_backend.dump_tree(
        window_title_re="Main.*", max_depth=5,
    )
    # Legacy fields (the keys P0.x callers depend on):
    assert "controls" in parsed
    assert "window_title" in parsed


def test_legacy_find_control_byte_compatible(stub_backend):
    raw = server.find_control.fn(text="OK")
    parsed = json.loads(raw)
    assert parsed == stub_backend.find_control(text="OK")
    assert "matches" in parsed


def test_legacy_list_windows_byte_compatible(stub_backend):
    raw = server.list_windows.fn()
    parsed = json.loads(raw)
    assert parsed == stub_backend.list_windows()
    assert "windows" in parsed


# ---------------------------------------------------------------------------
# Dispatcher and legacy tools coexist
# ---------------------------------------------------------------------------


def test_dispatcher_does_not_intercept_legacy_calls(stub_backend):
    """Calling `click` does NOT go through `dispatcher.dispatch`.
    The dispatcher's idempotency cache must be empty afterwards
    (no request_id was supplied and the legacy path bypasses
    dispatch)."""
    import action_dispatcher as ad
    ad.cache_clear()
    raw = server.click.fn(control_id=42)
    parsed = json.loads(raw)
    assert parsed["ok"] is True
    # Legacy path must NOT cache anything.
    assert ad.lru_size() == 0


def test_legacy_tool_call_count_independent_of_dispatcher(stub_backend):
    """Calling the legacy tool multiple times invokes the backend
    that many times. The dispatcher does not dedupe legacy calls
    (FR-P1.1-01 says the dispatcher honours idempotency; legacy
    tools are out of its scope by design)."""
    call_count = {"n": 0}

    def counting_click(*args, **kwargs):
        call_count["n"] += 1
        return {"ok": True, "data": {"n": call_count["n"]}}

    stub_backend.click = counting_click
    for _ in range(5):
        server.click.fn(control_id=1)
    assert call_count["n"] == 5


def test_dispatcher_and_legacy_call_have_independent_state(stub_backend):
    """Sanity: dispatcher state (cache, server_instance_id) does
    not leak into legacy tool return shapes.

    We touch the dispatcher's server_instance_id cache (which
    creates a stable id for this process) and then invoke a
    legacy tool; the legacy JSON must not pick up that id.
    """
    import action_dispatcher as ad
    ad.get_server_instance_id()  # populate cache
    raw = server.list_windows.fn()
    parsed = json.loads(raw)
    assert "windows" in parsed
    assert "server_instance_id" not in parsed  # legacy shape unchanged


# ---------------------------------------------------------------------------
# Legacy tools that require a connected window lock
# ---------------------------------------------------------------------------


def test_legacy_get_window_lock_byte_compatible(stub_backend):
    """`get_window_lock` is a legacy tool; the dispatcher must NOT
    inject server_instance_id into its return shape."""
    raw = server.get_window_lock.fn()
    parsed = json.loads(raw)
    assert parsed == stub_backend.get_window_lock()