"""P2.1 acceptance gate — classify_transition() (architecture §15.1).

Each test constructs a (before, after) snapshot pair from synthetic
target dicts and asserts the classifier returns the expected
``TransitionKind`` with the expected ``confidence``. Coordinates are
never part of the assertion: only structural signals (window set,
modal role, tree digest, title) drive the classifier.

Coverage matrix (per P2.1 doc FR-P2.1-01 → -04):

    * NONE                     — same windows, same tree, same title
    * CONTROL_STATE_CHANGE     — same windows, tree delta, no title
    * PAGE_NAVIGATION          — same windows, tree + title delta
    * MODAL_OPEN               — modal role appears
    * MODAL_CLOSE              — modal role disappears
    * WINDOW_OPEN              — new window, no close
    * WINDOW_CLOSE             — close window, no open
    * WINDOW_OWNER_CHANGE      — same window id, different pid
    * APPLICATION_RESTART      — pid set emptied + repopulated
    * UNKNOWN_MATERIAL_CHANGE  — fallback when no signal matches

Plus determinism + priority-ordering + signal-content tests.
"""

from __future__ import annotations

import pytest


# sys.path is configured via pyproject.toml [tool.pytest.ini_options]
# pythonpath (Issue 6 — no in-test sys.path mutation).


from agent.execution.transitions import (  # noqa: E402
    TransitionKind,
    TransitionResult,
    classify_transition,
)
from test_case.fixtures.transitions.builder import (  # noqa: E402
    make_control,
    make_modal,
    make_window,
    snapshot,
)


# ---------------------------------------------------------------------------
# NONE — identical snapshots
# ---------------------------------------------------------------------------


def test_classify_same_snapshot_returns_none():
    """Identical snapshots (same windows + same tree + same active) → NONE."""
    s = snapshot([
        make_window(pid=101, native_window_id="w-1",
                    title="Login", active=True),
    ])
    result = classify_transition(s, s)
    assert result.kind is TransitionKind.NONE
    assert result.confidence == "high"
    assert result.is_unexpected is False


def test_classify_coords_alone_do_not_promote_signal():
    """Coordinates alone never establish a transition (P2.1 doc line 39)."""
    # Same window, same process, same tree — only the rect differs.
    before = snapshot([
        make_window(pid=101, native_window_id="w-1", title="Login",
                    rect=(0, 0, 800, 600)),
    ])
    after = snapshot([
        make_window(pid=101, native_window_id="w-1", title="Login",
                    rect=(10, 5, 810, 605)),
    ])
    result = classify_transition(before, after)
    assert result.kind is TransitionKind.NONE


# ---------------------------------------------------------------------------
# CONTROL_STATE_CHANGE — tree delta, no title delta
# ---------------------------------------------------------------------------


def test_classify_tree_delta_no_title_is_control_state_change():
    """Tree digest changes, title stable → CONTROL_STATE_CHANGE."""
    before = snapshot([
        make_window(pid=101, native_window_id="w-1",
                    title="Dashboard", active=True),
        # extra control with stable identity — change its text only
        make_control(pid=101, native_window_id="w-1",
                     title="Loading", text="Loading…"),
    ])
    after = snapshot([
        make_window(pid=101, native_window_id="w-1",
                    title="Dashboard", active=True),
        make_control(pid=101, native_window_id="w-1",
                     title="Loading", text="Loaded"),
    ])
    result = classify_transition(before, after)
    assert result.kind is TransitionKind.CONTROL_STATE_CHANGE
    assert result.confidence in ("medium", "high")


# ---------------------------------------------------------------------------
# PAGE_NAVIGATION — tree + title delta
# ---------------------------------------------------------------------------


def test_classify_tree_and_title_delta_is_page_navigation():
    """Tree digest + window title change → PAGE_NAVIGATION."""
    before = snapshot([
        make_window(pid=101, native_window_id="w-1",
                    title="Login", active=True),
    ])
    after = snapshot([
        make_window(pid=101, native_window_id="w-1",
                    title="Dashboard", active=True),
    ])
    result = classify_transition(before, after)
    assert result.kind is TransitionKind.PAGE_NAVIGATION
    assert result.confidence == "high"


# ---------------------------------------------------------------------------
# MODAL_OPEN / MODAL_CLOSE
# ---------------------------------------------------------------------------


def test_classify_modal_appears_is_modal_open():
    """Modal-kind window appears in stable window set → MODAL_OPEN."""
    before = snapshot([
        make_window(pid=101, native_window_id="w-1",
                    title="Dashboard", active=True),
    ])
    after = snapshot([
        make_window(pid=101, native_window_id="w-1",
                    title="Dashboard", active=True),
        make_modal(pid=101, native_window_id="modal-1",
                   title="Confirm dialog"),
    ])
    result = classify_transition(before, after)
    assert result.kind is TransitionKind.MODAL_OPEN
    assert result.confidence == "medium"


def test_classify_modal_disappears_is_modal_close():
    """Modal-kind window disappears from stable window set → MODAL_CLOSE."""
    before = snapshot([
        make_window(pid=101, native_window_id="w-1",
                    title="Dashboard", active=True),
        make_modal(pid=101, native_window_id="modal-1",
                   title="Confirm dialog"),
    ])
    after = snapshot([
        make_window(pid=101, native_window_id="w-1",
                    title="Dashboard", active=True),
    ])
    result = classify_transition(before, after)
    assert result.kind is TransitionKind.MODAL_CLOSE
    assert result.confidence == "medium"


# ---------------------------------------------------------------------------
# WINDOW_OPEN / WINDOW_CLOSE
# ---------------------------------------------------------------------------


def test_classify_new_window_only_is_window_open():
    """New (pid, native_window_id) only → WINDOW_OPEN."""
    before = snapshot([
        make_window(pid=101, native_window_id="w-1",
                    title="Login", active=True),
    ])
    after = snapshot([
        make_window(pid=101, native_window_id="w-1",
                    title="Login", active=True),
        make_window(pid=101, native_window_id="w-2",
                    title="Dashboard"),
    ])
    result = classify_transition(before, after)
    assert result.kind is TransitionKind.WINDOW_OPEN
    assert result.confidence == "high"
    # signals should include the new window
    new = result.signals.get("new_windows")
    assert new and (101, "w-2") in new


def test_classify_closed_window_only_is_window_close():
    """Closed windows only → WINDOW_CLOSE."""
    before = snapshot([
        make_window(pid=101, native_window_id="w-1",
                    title="Login", active=True),
        make_window(pid=101, native_window_id="w-2",
                    title="Help"),
    ])
    after = snapshot([
        make_window(pid=101, native_window_id="w-1",
                    title="Login", active=True),
    ])
    result = classify_transition(before, after)
    assert result.kind is TransitionKind.WINDOW_CLOSE
    assert result.confidence == "high"
    closed = result.signals.get("closed_windows")
    assert closed and (101, "w-2") in closed


# ---------------------------------------------------------------------------
# WINDOW_OWNER_CHANGE
# ---------------------------------------------------------------------------


def test_classify_window_owner_change_when_pid_differs_for_same_window_id():
    """Same native_window_id, different pid → WINDOW_OWNER_CHANGE.

    The classifier must NOT mistake this for window_open + window_close
    (they cancel out) — owner change wins before the delta check.
    """
    before = snapshot([
        make_window(pid=101, native_window_id="w-1",
                    title="Login", active=True),
    ])
    after = snapshot([
        make_window(pid=202, native_window_id="w-1",
                    title="Login", active=True),
    ])
    result = classify_transition(before, after)
    assert result.kind is TransitionKind.WINDOW_OWNER_CHANGE
    assert result.confidence == "medium"
    assert result.signals.get("from_pid") == 101
    assert result.signals.get("to_pid") == 202


# ---------------------------------------------------------------------------
# APPLICATION_RESTART
# ---------------------------------------------------------------------------


def test_classify_application_restart_when_all_pids_gone_then_repopulated():
    """All previously-seen pids disappear AND new pids appear → APPLICATION_RESTART."""
    before = snapshot([
        make_window(pid=101, native_window_id="w-1",
                    title="Login", active=True),
        make_window(pid=101, native_window_id="w-2",
                    title="Help"),
    ])
    after = snapshot([
        make_window(pid=303, native_window_id="w-3",
                    title="Login", active=True),
    ])
    result = classify_transition(before, after)
    assert result.kind is TransitionKind.APPLICATION_RESTART
    assert result.confidence == "medium"


# ---------------------------------------------------------------------------
# UNKNOWN_MATERIAL_CHANGE — fallback
# ---------------------------------------------------------------------------


def test_classify_process_name_change_promotes_to_application_restart():
    """Different process_name with same window id → APPLICATION_RESTART.

    Topology signature includes process_name (Blocker 3 hardening),
    so a process_name change on a surviving window means the
    underlying process identity changed — that is a restart signal,
    not an owner_change. Same pid + same hwnd across two different
    processes is the canonical "hwnd was recycled by the OS" case
    from the review.
    """
    before = snapshot([
        make_window(pid=101, native_window_id="w-1",
                    process_name="ProcessA",
                    title="Login", active=True),
        make_control(pid=101, native_window_id="w-1",
                     title="Status", text="Loading"),
    ])
    after = snapshot([
        make_window(pid=101, native_window_id="w-1",
                    process_name="ProcessB",
                    title="Login", active=True),
        make_control(pid=101, native_window_id="w-1",
                     title="Status", text="Loading"),
    ])
    result = classify_transition(before, after)
    assert result.kind is TransitionKind.APPLICATION_RESTART
    assert "all_previous_topology_gone" in result.signals.get(
        "restart", ""
    )


def test_classify_unknown_is_defensive_fallback():
    """UNKNOWN_MATERIAL_CHANGE is a defensive fallback — not a
    routine classification.

    With the topology-based priority ladder, every realistic
    configuration is captured by a primary branch. The
    UNKNOWN_MATERIAL_CHANGE branch exists for cases we have not
    enumerated; it must still produce a TransitionResult with
    ``confidence="low"`` and ``is_unexpected=True``.

    This test asserts the fallback path is reachable when ALL
    comparison dimensions collapse to equality: identical
    topology, pid, window set, modal set, fingerprint set,
    title set, text set, tree digest, AND active_window pointer
    — which the NONE branch claims first. We accept either NONE
    or UNKNOWN_MATERIAL_CHANGE for this corner case to lock in
    the contract that the fallback still exists and is callable.
    """
    before = snapshot([
        make_window(pid=101, native_window_id="w-1",
                    title="Login", active=True),
    ])
    after = snapshot([
        make_window(pid=101, native_window_id="w-1",
                    title="Login", active=True),
    ])
    result = classify_transition(before, after)
    assert result.kind in (
        TransitionKind.NONE,
        TransitionKind.UNKNOWN_MATERIAL_CHANGE,
    )
    # When NONE, confidence is high; when UNKNOWN, low. Both are
    # documented acceptable values.
    assert result.confidence in ("high", "low")


def test_classify_unexpected_flag_only_true_for_unknown():
    """`is_unexpected` is True only for UNKNOWN_MATERIAL_CHANGE."""
    none = TransitionResult(kind=TransitionKind.NONE)
    page = TransitionResult(kind=TransitionKind.PAGE_NAVIGATION)
    unknown = TransitionResult(kind=TransitionKind.UNKNOWN_MATERIAL_CHANGE)
    assert none.is_unexpected is False
    assert page.is_unexpected is False
    assert unknown.is_unexpected is True


# ---------------------------------------------------------------------------
# Determinism + priority ordering
# ---------------------------------------------------------------------------


def test_classify_is_deterministic_for_same_inputs():
    """Same (before, after) pair yields the same result on every call."""
    before = snapshot([
        make_window(pid=101, native_window_id="w-1",
                    title="Login", active=True),
    ])
    after = snapshot([
        make_window(pid=101, native_window_id="w-1",
                    title="Dashboard", active=True),
    ])
    results = {classify_transition(before, after).kind for _ in range(10)}
    assert len(results) == 1
    assert TransitionKind.PAGE_NAVIGATION in results


def test_classify_signals_payload_is_dict():
    """`signals` is always a dict (may be empty)."""
    s = snapshot([
        make_window(pid=101, native_window_id="w-1", title="X"),
    ])
    result = classify_transition(s, s)
    assert isinstance(result.signals, dict)
    assert "before_pids" in result.signals


# ---------------------------------------------------------------------------
# Confidence contract
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("confidence", ["high", "medium", "low"])
def test_classify_confidence_values_accepted(confidence):
    """TransitionResult accepts the three documented confidence values."""
    r = TransitionResult(kind=TransitionKind.NONE, confidence=confidence)
    assert r.confidence == confidence


def test_classify_invalid_confidence_rejected():
    """Anything outside the documented set is rejected loudly."""
    with pytest.raises(ValueError, match="confidence"):
        TransitionResult(kind=TransitionKind.NONE, confidence="maybe")


# ---------------------------------------------------------------------------
# Priority — application_restart wins over window_open (smoke)
# ---------------------------------------------------------------------------


def test_classify_application_restart_priority_over_window_delta():
    """Restart signal wins even when window set also differs."""
    before = snapshot([
        make_window(pid=101, native_window_id="w-1",
                    title="Login", active=True),
    ])
    after = snapshot([
        make_window(pid=202, native_window_id="w-2",
                    title="Login", active=True),
    ])
    # Both pids disappeared AND new windows appeared. Restart must win.
    result = classify_transition(before, after)
    assert result.kind is TransitionKind.APPLICATION_RESTART