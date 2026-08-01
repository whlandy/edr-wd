"""
P1.1 acceptance gate — pre-dispatch condition checks.

FR-P1.1-04 (live enablement), -05 (requires), -06 (action_code),
-07 (ownership).
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
from action_catalog import ACTIONS_V1    # noqa: E402


def _spec(action_id: str):
    return next(s for s in ACTIONS_V1 if s.action_id == action_id)


# ---------------------------------------------------------------------------
# FR-P1.1-04: live enablement
# ---------------------------------------------------------------------------


def test_disabled_action_returns_backend_disabled():
    spec = _spec("gui.type_text")
    r = ad.check_live_enablement(
        spec, enabled=False,
        disabled_reason="not supported by macos_accessibility",
        action_id="gui.type_text",
        action_code=spec.action_code,
        request_id=None,
    )
    assert r is not None
    assert r.code == ad.CODE_BACKEND_DISABLED
    assert r.disabled_reason == "not supported by macos_accessibility"


def test_enabled_action_passes():
    spec = _spec("gui.click")
    r = ad.check_live_enablement(
        spec, enabled=True, disabled_reason=None,
        action_id="gui.click",
        action_code=spec.action_code,
        request_id=None,
    )
    assert r is None


# ---------------------------------------------------------------------------
# FR-P1.1-05: requires
# ---------------------------------------------------------------------------


def test_connected_window_violation():
    spec = _spec("gui.click")  # requires connected_window, window_lock, target_ref
    r = ad.check_requires(
        spec,
        action_id="gui.click", action_code=spec.action_code, request_id=None,
        has_connected_window=False,
        has_window_lock=True,
        target_ref={"control_id": "btn"},
    )
    assert r is not None
    # When target_ref lacks a selector_hint, conditions surfaces
    # missing_selector_hint first (more specific than the generic
    # precondition_failed).
    assert r.code == ad.CODE_MISSING_SELECTOR_HINT


def test_window_lock_violation():
    spec = _spec("gui.click")
    r = ad.check_requires(
        spec,
        action_id="gui.click", action_code=spec.action_code, request_id=None,
        has_connected_window=True,
        has_window_lock=False,
        target_ref={"control_id": "btn"},
    )
    assert r is not None
    assert r.code == ad.CODE_MISSING_SELECTOR_HINT


def test_target_ref_missing_violation():
    spec = _spec("gui.click")
    r = ad.check_requires(
        spec,
        action_id="gui.click", action_code=spec.action_code, request_id=None,
        has_connected_window=True,
        has_window_lock=True,
        target_ref=None,
    )
    assert r is not None
    assert r.code == ad.CODE_PRECONDITION_FAILED
    assert "target_ref" in r.missing


def test_missing_selector_hint_for_semantic_input():
    """gui.click requires target_ref + selector_hint. Without
    selector_hint -> missing_selector_hint."""
    spec = _spec("gui.click")
    r = ad.check_requires(
        spec,
        action_id="gui.click", action_code=spec.action_code, request_id=None,
        has_connected_window=True,
        has_window_lock=True,
        target_ref={"control_id": "btn"},  # no selector_hint
    )
    assert r is not None
    assert r.code == ad.CODE_MISSING_SELECTOR_HINT


def test_all_requires_satisfied_passes():
    spec = _spec("gui.click")
    r = ad.check_requires(
        spec,
        action_id="gui.click", action_code=spec.action_code, request_id=None,
        has_connected_window=True,
        has_window_lock=True,
        target_ref={
            "control_id": "btn",
            "selector_hint": {"control_id": "btn"},
        },
    )
    assert r is None


# ---------------------------------------------------------------------------
# FR-P1.1-06: action_code match
# ---------------------------------------------------------------------------


def test_action_code_match_passes_when_equal():
    spec = _spec("gui.click")
    r = ad.check_action_code_match(
        spec, "A020",
        action_id="gui.click", request_id=None,
    )
    assert r is None


def test_action_code_match_passes_when_none():
    """Caller may omit action_code; the dispatcher uses the
    catalog's code."""
    spec = _spec("gui.click")
    r = ad.check_action_code_match(
        spec, None,
        action_id="gui.click", request_id=None,
    )
    assert r is None


def test_action_code_mismatch_raises_action_code_mismatch():
    spec = _spec("gui.click")
    r = ad.check_action_code_match(
        spec, "A030",  # wrong code
        action_id="gui.click", request_id=None,
    )
    assert r is not None
    assert r.code == ad.CODE_ACTION_CODE_MISMATCH
    assert "A030" in r.message


# ---------------------------------------------------------------------------
# FR-P1.1-07: ownership
# ---------------------------------------------------------------------------


class _StubBackend:
    def __init__(self, lock=None):
        self._lock = lock

    def get_window_lock(self):
        return self._lock


def test_ownership_mismatch_pre_dispatch():
    backend = _StubBackend(lock={
        "ok": True,
        "process_name": "RealApp.exe",
    })
    r = ad.check_ownership(
        {"expected_process_name": "WrongApp.exe"},
        backend,
        action_id="gui.click",
        action_code="A020",
        request_id=None,
    )
    assert r is not None
    assert r.code == ad.CODE_OWNERSHIP_MISMATCH
    assert r.extras["expected_process_name"] == "WrongApp.exe"
    assert r.extras["actual_process_name"] == "RealApp.exe"


def test_ownership_match_passes():
    backend = _StubBackend(lock={"ok": True, "process_name": "X.exe"})
    r = ad.check_ownership(
        {"expected_process_name": "X.exe"},
        backend,
        action_id="gui.click",
        action_code="A020",
        request_id=None,
    )
    assert r is None


def test_ownership_check_skipped_when_no_lock():
    """No active lock -> fall through (the requires-checks have
    already gated this)."""
    backend = _StubBackend(lock=None)
    r = ad.check_ownership(
        {"expected_process_name": "X.exe"},
        backend,
        action_id="gui.click",
        action_code="A020",
        request_id=None,
    )
    assert r is None


def test_ownership_check_skipped_when_no_expected_process():
    backend = _StubBackend(lock={"ok": True, "process_name": "X.exe"})
    r = ad.check_ownership(
        {},  # no expected_process_name
        backend,
        action_id="gui.click",
        action_code="A020",
        request_id=None,
    )
    assert r is None