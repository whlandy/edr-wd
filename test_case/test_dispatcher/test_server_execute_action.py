"""
P1.1 acceptance gate — execute_action MCP tool (server.py wiring).

P1.1 review #1 (blocker 6): the MCP tool surface for the unified
dispatcher must be tested through the actual FastMCP registration,
not only via the underlying dispatch() function.

We exercise:

  * The tool is registered as `execute_action` on the FastMCP
    instance.
  * Calling `server.execute_action.fn(...)` runs the dispatcher and
    returns an `ActionReceipt` JSON envelope with the expected
    stable codes.
  * The tool exposes the canonical action_id / action_code /
    args / target_ref / request_id parameters.
  * Failure modes (unknown action_id, backend_disabled, etc.)
    surface as stable codes.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest


_REPO = Path(__file__).resolve().parents[2]
_TARGET = _REPO / "target"

if str(_TARGET) not in sys.path:
    sys.path.insert(0, str(_TARGET))


import action_dispatcher as ad          # noqa: E402
import observation_bridge as ob         # noqa: E402
import server                            # noqa: E402


@pytest.fixture(autouse=True)
def _reset_state():
    ad.cache_clear()
    ad.reset_server_instance_id_for_tests()
    ob.reset_for_tests()
    yield
    ad.cache_clear()
    ob.reset_for_tests()


# ---------------------------------------------------------------------------
# Registration surface
# ---------------------------------------------------------------------------


def test_server_exposes_execute_action_tool():
    """FastMCP 3.4 keeps the decorated symbol callable and stores the
    FunctionTool in the server registry."""
    assert callable(server.execute_action)
    tool = asyncio.run(server.mcp.get_tool("execute_action"))
    assert tool is not None
    assert tool.fn is server.execute_action


def test_execute_action_tool_name_is_execute_action():
    """The FastMCP `name` attribute is the canonical MCP tool name."""
    tool = asyncio.run(server.mcp.get_tool("execute_action"))
    assert tool is not None
    assert tool.name == "execute_action"


def test_execute_action_signature_has_canonical_params():
    """Inspecting the underlying function signature keeps the
    MCP contract stable."""
    import inspect
    sig = inspect.signature(server.execute_action)
    params = sig.parameters
    for required in ("action_id",):
        assert required in params, (
            f"execute_action missing canonical param {required!r}"
        )
    for optional in ("action_code", "args", "target_ref", "request_id"):
        assert optional in params, (
            f"execute_action missing optional param {optional!r}"
        )


# ---------------------------------------------------------------------------
# End-to-end through the tool
# ---------------------------------------------------------------------------


def test_execute_action_returns_action_receipt_json():
    """Without a backend resolver, the receipt is
    dispatch_target_missing — wrapped as a JSON string."""
    tool = server.execute_action
    result = tool(
        action_id="gui.click",
        args={"control_id": "btn-ok"},
        target_ref={
            "expected_process_name": "X.exe",
            "selector_hint": {"control_id": "btn-ok"},
        },
    )
    assert isinstance(result, str)
    parsed = json.loads(result)
    assert parsed["action_id"] == "gui.click"
    assert parsed["code"] in {
        ad.CODE_DISPATCH_TARGET_MISSING,
        ad.CODE_MISSING_SELECTOR_HINT,
        ad.CODE_PRECONDITION_FAILED,
    }
    assert parsed["server_instance_id"].startswith("inst-")


def test_execute_action_unknown_action_id():
    tool = server.execute_action
    result = tool(action_id="foo.bar")
    parsed = json.loads(result)
    assert parsed["code"] == ad.CODE_UNKNOWN_ACTION_ID
    assert parsed["ok"] is False


def test_execute_action_disabled_action():
    """macos_accessibility: gui.type_text is disabled."""
    class _Stub:
        backend_kind = "macos_accessibility"
        def get_window_lock(self):
            return {"ok": True, "process_name": "X.exe"}
    ad.set_backend_resolver(lambda: _Stub())
    tool = server.execute_action
    result = tool(
        action_id="gui.type_text",
        args={"control_id": "tb"},
        target_ref={
            "expected_process_name": "X.exe",
            "selector_hint": {"control_id": "tb"},
        },
    )
    parsed = json.loads(result)
    assert parsed["code"] == ad.CODE_BACKEND_DISABLED
    assert "macos" in (parsed["disabled_reason"] or "")


def test_execute_action_invalid_request_id():
    tool = server.execute_action
    # request_id must be a non-empty string; pass an int.
    result = tool(action_id="gui.click", request_id=123)
    parsed = json.loads(result)
    assert parsed["code"] == ad.CODE_INVALID_REQUEST_ID


def test_execute_action_idempotent_through_tool():
    """The MCP tool honours idempotency: same request_id yields
    the same cached receipt without re-invoking the backend."""
    class _Stub:
        backend_kind = "macos_accessibility"
        def __init__(self):
            self.click_count = 0
        def click(self, **kw):
            self.click_count += 1
            return {"ok": True, "data": {"clicked": True}}
        def get_window_lock(self):
            return {"ok": True, "process_name": "X.exe"}
    stub = _Stub()
    ad.set_backend_resolver(lambda: stub)
    target = {
        "expected_process_name": "X.exe",
        "selector_hint": {"control_id": "btn-ok"},
    }
    tool = server.execute_action
    r1 = json.loads(tool(
        action_id="gui.click",
        args={"control_id": "btn-ok"},
        target_ref=target,
        request_id="R-MCP",
    ))
    r2 = json.loads(tool(
        action_id="gui.click",
        args={"control_id": "btn-ok"},
        target_ref=target,
        request_id="R-MCP",
    ))
    assert r1["code"] == ad.CODE_OK
    assert r2["code"] == ad.CODE_OK
    assert r1["server_instance_id"] == r2["server_instance_id"]
    assert stub.click_count == 1


def test_execute_action_invalidates_active_snapshot():
    """Through the tool: a successful mutating action invalidates
    the active observation snapshot, as documented."""
    class _Stub:
        backend_kind = "macos_accessibility"
        def click(self, **kw):
            return {"ok": True, "data": {"clicked": True}}
        def get_window_lock(self):
            return {"ok": True, "process_name": "X.exe"}
    ad.set_backend_resolver(lambda: _Stub())
    from observations import (
        is_live, mark_live,
    )
    mark_live("SNAP-X")
    ob.set_active_snapshot("SNAP-X")
    tool = server.execute_action
    parsed = json.loads(tool(
        action_id="gui.click",
        args={"control_id": "btn-ok"},
        target_ref={
            "expected_process_name": "X.exe",
            "selector_hint": {"control_id": "btn-ok"},
        },
        request_id="R-INV",
    ))
    assert parsed["code"] == ad.CODE_OK
    assert parsed["extras"]["invalidated_snapshot_id"] == "SNAP-X"
    assert not is_live("SNAP-X")


def test_execute_action_server_inline_routes_to_dispatch_target_missing():
    """Server-inline actions (e.g. restore_edr) are not
    backend-routed; the tool surfaces dispatch_target_missing
    so the caller knows to use the legacy inline path."""
    class _Stub:
        backend_kind = "macos_accessibility"
        def restore_edr(self):
            return {"ok": True}
        def get_window_lock(self):
            return {"ok": True, "process_name": "X.exe"}
    ad.set_backend_resolver(lambda: _Stub())
    tool = server.execute_action
    parsed = json.loads(tool(action_id="hisec.restore_edr"))
    assert parsed["code"] == ad.CODE_DISPATCH_TARGET_MISSING
