"""
P1.1 acceptance gate — top-level dispatch() entry point.

End-to-end: every P1.1 acceptance criterion is exercised through
dispatch() against a stub backend.

FR-P1.1-01..10.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


_REPO = Path(__file__).resolve().parents[2]
_TARGET = _REPO / "target"

if str(_TARGET) not in sys.path:
    sys.path.insert(0, str(_TARGET))


import action_dispatcher as ad          # noqa: E402
import observation_bridge as ob         # noqa: E402
from action_catalog import ACTIONS_V1    # noqa: E402
from observations import (               # noqa: E402
    is_live as p03_is_live,
    mark_live as p03_mark_live,
    reset_for_tests as p03_reset,
)


@pytest.fixture(autouse=True)
def _reset_state():
    ad.cache_clear()
    ad.reset_server_instance_id_for_tests()
    ob.reset_for_tests()
    p03_reset()
    yield
    ad.cache_clear()
    ob.reset_for_tests()
    p03_reset()


# ---------------------------------------------------------------------------
# FR-P1.1-10: every catalog action resolves or surfaces
# dispatch_target_missing cleanly (covered structurally in
# test_mapping.test_dispatch_all_actions_resolve). Here we test
# the runtime path.
# ---------------------------------------------------------------------------


def test_dispatch_unknown_action_returns_unknown_action_id():
    r = ad.dispatch("foo.bar")
    assert r.code == ad.CODE_UNKNOWN_ACTION_ID
    assert r.ok is False


def test_dispatch_returns_dispatch_target_missing_when_method_absent():
    """A backend that lacks the tool_name surfaces
    dispatch_target_missing (FR-P1.1-10). The action must NOT crash
    the server.

    The backend must declare a `backend_kind` that the catalog
    recognises (macos_accessibility) so Step 4 does not surface
    backend_disabled first; only Step 8 (method resolution)
    surfaces dispatch_target_missing."""

    class NoClickBackend:
        backend_kind = "macos_accessibility"
        # No `click` method implemented.

        def get_window_lock(self):
            return {"ok": True, "process_name": "X.exe"}

    ad.set_backend_resolver(lambda: NoClickBackend())
    r = ad.dispatch(
        "gui.click",
        args={"control_id": "btn"},
        target_ref={
            "expected_process_name": "X.exe",
            "selector_hint": {"control_id": "btn"},
        },
    )
    assert r.code == ad.CODE_DISPATCH_TARGET_MISSING


def test_dispatch_server_inline_action_returns_dispatch_target_missing():
    """server-inline actions (e.g. restore_edr) are not routed
    through the dispatcher; surface dispatch_target_missing so the
    caller knows to invoke the inline path."""

    class B:
        backend_kind = "b"
        def restore_edr(self):
            return {"ok": True}

    ad.set_backend_resolver(lambda: B())
    r = ad.dispatch("hisec.restore_edr")
    assert r.code == ad.CODE_DISPATCH_TARGET_MISSING


# ---------------------------------------------------------------------------
# FR-P1.1-02: idempotency (call-count)
# ---------------------------------------------------------------------------


class _CountingBackend:
    def __init__(self):
        self.click_count = 0

    def click(self, **kw):
        self.click_count += 1
        return {"ok": True, "data": {"clicked": True}}

    def get_window_lock(self):
        return {"ok": True, "process_name": "X.exe"}


def test_idempotency_one_mutation_per_request_id():
    backend = _CountingBackend()
    ad.set_backend_resolver(lambda: backend)
    target = {
        "expected_process_name": "X.exe",
        "selector_hint": {"control_id": "btn-ok"},
    }
    rec1 = ad.dispatch("gui.click", args={"control_id": "btn-ok"},
                       target_ref=target, request_id="R1")
    rec2 = ad.dispatch("gui.click", args={"control_id": "btn-ok"},
                       target_ref=target, request_id="R1")
    assert rec1.code == ad.CODE_OK
    assert rec2.code == ad.CODE_OK
    assert backend.click_count == 1


def test_distinct_request_ids_produce_distinct_mutations():
    backend = _CountingBackend()
    ad.set_backend_resolver(lambda: backend)
    target = {
        "expected_process_name": "X.exe",
        "selector_hint": {"control_id": "btn-ok"},
    }
    ad.dispatch("gui.click", args={"control_id": "btn-ok"},
                target_ref=target, request_id="R1")
    ad.dispatch("gui.click", args={"control_id": "btn-ok"},
                target_ref=target, request_id="R2")
    assert backend.click_count == 2


# ---------------------------------------------------------------------------
# FR-P1.1-04: disabled action on macOS
# ---------------------------------------------------------------------------


def test_disabled_action_returns_backend_disabled():
    """gui.type_text is BACKEND_NOT_IMPLEMENTED on macOS (P0.1
    catalog); dispatch must surface backend_disabled."""
    # Default backend in this repo is macos_accessibility (the
    # catalog's BACKEND_NOT_IMPLEMENTED table marks type_text,
    # select, and get_text as unsupported).
    class _AnyBackend:
        backend_kind = "macos_accessibility"
        def type_text(self, **kw):
            return {"ok": True}
        def get_window_lock(self):
            return {"ok": True, "process_name": "X.exe"}

    ad.set_backend_resolver(lambda: _AnyBackend())
    r = ad.dispatch(
        "gui.type_text",
        args={"control_id": "tb"},
        target_ref={
            "expected_process_name": "X.exe",
            "selector_hint": {"control_id": "tb"},
        },
    )
    assert r.code == ad.CODE_BACKEND_DISABLED
    assert r.disabled_reason is not None
    assert "macos" in r.disabled_reason


# ---------------------------------------------------------------------------
# FR-P1.1-06: action_code mismatch
# ---------------------------------------------------------------------------


def test_action_code_mismatch():
    backend = _CountingBackend()
    ad.set_backend_resolver(lambda: backend)
    target = {
        "expected_process_name": "X.exe",
        "selector_hint": {"control_id": "btn-ok"},
    }
    r = ad.dispatch(
        "gui.click",
        action_code="A030",  # wrong code for gui.click (A020)
        args={"control_id": "btn-ok"},
        target_ref=target,
    )
    assert r.code == ad.CODE_ACTION_CODE_MISMATCH


# ---------------------------------------------------------------------------
# FR-P1.1-07: ownership mismatch pre-dispatch (no click)
# ---------------------------------------------------------------------------


def test_ownership_mismatch_blocks_dispatch():
    backend = _CountingBackend()
    ad.set_backend_resolver(lambda: backend)
    r = ad.dispatch(
        "gui.click",
        args={"control_id": "btn"},
        target_ref={
            "expected_process_name": "Wrong.exe",
            "selector_hint": {"control_id": "btn"},
        },
    )
    assert r.code == ad.CODE_OWNERSHIP_MISMATCH
    assert backend.click_count == 0  # not invoked


# ---------------------------------------------------------------------------
# FR-P1.1-05: precondition
# ---------------------------------------------------------------------------


def test_precondition_window_lock():
    backend = _CountingBackend()
    ad.set_backend_resolver(lambda: backend)
    # No target_ref at all -> precondition_failed.
    r = ad.dispatch("gui.click", args={"control_id": "btn"})
    assert r.code in (ad.CODE_PRECONDITION_FAILED,
                      ad.CODE_MISSING_SELECTOR_HINT)
    assert backend.click_count == 0


# ---------------------------------------------------------------------------
# FR-P1.1-09: every receipt carries server_instance_id
# ---------------------------------------------------------------------------


def test_receipt_carries_server_instance_id():
    backend = _CountingBackend()
    ad.set_backend_resolver(lambda: backend)
    sid = ad.get_server_instance_id()
    target = {
        "expected_process_name": "X.exe",
        "selector_hint": {"control_id": "btn-ok"},
    }
    r = ad.dispatch("gui.click", args={"control_id": "btn-ok"},
                    target_ref=target, request_id="R")
    assert r.server_instance_id == sid


# ---------------------------------------------------------------------------
# Invalid request_id
# ---------------------------------------------------------------------------


def test_invalid_request_id_type():
    r = ad.dispatch("gui.click", args={"control_id": "btn"},
                    request_id=123)  # type: ignore[arg-type]
    assert r.code == ad.CODE_INVALID_REQUEST_ID


# ---------------------------------------------------------------------------
# End-to-end with snapshot invalidation
# ---------------------------------------------------------------------------


def test_full_flow_dispatch_invalidates_snapshot():
    backend = _CountingBackend()
    ad.set_backend_resolver(lambda: backend)
    p03_mark_live("SNAP-1")
    ob.set_active_snapshot("SNAP-1")
    target = {
        "expected_process_name": "X.exe",
        "selector_hint": {"control_id": "btn-ok"},
    }
    r = ad.dispatch("gui.click", args={"control_id": "btn-ok"},
                    target_ref=target, request_id="R")
    assert r.code == ad.CODE_OK
    assert not p03_is_live("SNAP-1")
    assert r.extras.get("invalidated_snapshot_id") == "SNAP-1"