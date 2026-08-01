"""
P1.1 acceptance gate — observation_bridge.invalidation wires
mutating actions to P0.3's snapshot registry.

Architecture §8.4 / FR-P1.1 lifecycle: every successful mutating
action invalidates the active observation snapshot.
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
from observations import (               # noqa: E402
    is_live as p03_is_live,
    mark_live as p03_mark_live,
    reset_for_tests as p03_reset,
)


@pytest.fixture(autouse=True)
def _reset_registries():
    ad.cache_clear()
    ad.reset_server_instance_id_for_tests()
    ob.reset_for_tests()
    p03_reset()
    yield
    ad.cache_clear()
    ob.reset_for_tests()
    p03_reset()


def test_set_then_get_active_snapshot():
    ob.set_active_snapshot("SNAP-1")
    assert ob.get_active_snapshot() == "SNAP-1"


def test_invalidate_after_mutation_clears_active():
    p03_mark_live("SNAP-1")
    ob.set_active_snapshot("SNAP-1")
    assert p03_is_live("SNAP-1")
    invalidated = ob.invalidate_after_mutation("test_backend")
    assert invalidated == "SNAP-1"
    assert not p03_is_live("SNAP-1")


def test_invalidate_after_mutation_no_active_returns_none():
    assert ob.invalidate_after_mutation("test_backend") is None


# ---------------------------------------------------------------------------
# End-to-end via dispatcher: mutation action triggers invalidation.
# ---------------------------------------------------------------------------


class _LockBackend:
    """Minimal backend with lock + click that returns ok=True."""
    def __init__(self):
        self.click_count = 0

    def click(self, **kw):
        self.click_count += 1
        return {"ok": True, "data": {"clicked": True}}

    def get_window_lock(self):
        return {"ok": True, "process_name": "X.exe"}


@pytest.fixture
def backend():
    b = _LockBackend()
    ad.set_backend_resolver(lambda: b)
    return b


def test_dispatch_invalidation_full_flow(backend):
    p03_mark_live("SNAP-1")
    ob.set_active_snapshot("SNAP-1")
    assert p03_is_live("SNAP-1")

    # Pre-register the live snapshot, then dispatch a mutating action
    # that the dispatcher will treat as success.
    rec = ad.dispatch(
        "gui.click",
        args={"control_id": "btn-ok"},
        target_ref={
            "expected_process_name": "X.exe",
            "selector_hint": {"control_id": "btn-ok"},
        },
        request_id="R",
    )
    # The receipt is the success path; click_count increased; the
    # observation snapshot was invalidated by the bridge.
    assert rec.code == ad.CODE_OK
    assert backend.click_count == 1
    assert not p03_is_live("SNAP-1")
    # And the receipt surfaces the invalidated snapshot_id for
    # traceability (architecture §8.4).
    assert rec.extras.get("invalidated_snapshot_id") == "SNAP-1"


def test_dispatch_does_not_invalidate_on_failure(backend):
    """A failed mutation must NOT invalidate (the world has not
    changed; the receipt records the failure)."""
    p03_mark_live("SNAP-1")
    ob.set_active_snapshot("SNAP-1")

    # Force a precondition failure: omit target_ref's selector_hint.
    rec = ad.dispatch(
        "gui.click",
        args={"control_id": "btn-ok"},
        target_ref={"expected_process_name": "X.exe"},
        request_id="R",
    )
    assert rec.code == ad.CODE_MISSING_SELECTOR_HINT
    assert backend.click_count == 0
    # Snapshot still live because we never invoked the backend.
    assert p03_is_live("SNAP-1")


def test_dispatch_observation_action_does_not_invalidate(backend):
    """observation-only actions must not invalidate the active
    snapshot (architecture §8.4)."""
    p03_mark_live("SNAP-1")
    ob.set_active_snapshot("SNAP-1")

    # observe.window_lock is an observation action; no requires.
    rec = ad.dispatch("observe.window_lock", request_id="R")
    # The action dispatches via backend's get_window_lock which
    # returns ok; this is an observation (no snapshot invalidation).
    assert rec.code == ad.CODE_OK
    assert p03_is_live("SNAP-1")