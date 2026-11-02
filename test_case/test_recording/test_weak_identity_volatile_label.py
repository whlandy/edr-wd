"""Weak-identity dynamic-label honesty: a label that changes inside the
recording must not compile to a promised-`ready` step.

The replay locator itself documents (``agent/locator/hybrid.py``) that
"Recorded labels (notably clocks, log rows and status text) may legitimately
change by replay" — and therefore drops the ``name`` comparison only for
strong-ID controls. A weak-identity control (no automationId/identifier)
resolves at replay *by* its label, controlType and ancestry, so when the
label at the same structural position is volatile the step can never resolve:
replay sees a different name, ``candidate_count=0``,
``replay_target_not_found``.

The recorder already persists multiple deduplicated control snapshots of the
same window as page state changes (``ControlInventory``). The compiler's
weak-identity gate (``_weak_selector_candidate_count``) judges uniqueness
against exactly ONE event-bound frame — the frame at the click. If the same
structural position (same controlType + same ancestry) carried a *different*
label in an earlier frame of the same window, that is direct recorded
evidence the label is volatile, and promising `ready` is dishonest — the
same class of blindside the 11.26 six-Details live replay produced
(compile `ready`, replay blocked).

Contract under test:

- Weak-identity selector whose (controlType, ancestry) position shows a
  different ``text`` in another complete frame of the SAME window ->
  ``compile_weak_identity_volatile_label`` + `incomplete`.
- Strong-ID controls keep `ready` — a stable id owns identity, and the
  locator drops name comparison for them by design.
- Weak-identity control whose label is stable across all frames of the same
  window keeps `ready` — no false positives.
- Incomplete/other-window frames must not trigger the gate (a different
  window's same-shaped tree is not evidence about this one; a truncated
  frame that happens to miss the alternate label carries no volatility
  signal).

Fix scope: additive check inside the existing weak-identity block of
``_compile_event``; the single-frame uniqueness count is unchanged.
"""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest

# Module-identity trap (pyproject pythonpath has both agent/ and ./):
# always import through `agent.*`.
from agent.recording.compiler import compile_recording
from target.recording.models import (
    CaptureScope,
    ObservedTarget,
    RawCaptureEvent,
    RawRecording,
)

_CHAIN = (
    {"controlType": "Table", "automationId": "grid"},
    {"controlType": "Group", "automationId": "base.page"},
)

_WINDOW = {"handle": 4242, "title": "Scan", "processName": "EDRClient.exe",
           "rootAutomationId": "base"}


def _snapshot(controls: list[dict[str, Any]], captured_ms: int, reason: str,
              complete: bool = True) -> dict[str, Any]:
    return {
        "snapshotId": f"CS-{captured_ms:06d}",
        "capturedAtMs": captured_ms,
        "reason": reason,
        "durationMs": 0,
        "window": dict(_WINDOW),
        "contentHash": "h",
        "stateHash": "h",
        "controlCount": len(controls),
        "complete": complete,
        "controls": controls,
    }


def _status_cell(text: str, automation_id: str | None = None) -> dict[str, Any]:
    control: dict[str, Any] = {
        "controlType": "Text",
        "text": text,
        "ancestry": [dict(layer) for layer in _CHAIN],
    }
    if automation_id is not None:
        control["automationId"] = automation_id
    return control


_SCOPE = CaptureScope("t", "windows_pywinauto", "EDRClient.exe", "^Scan$")


_LABEL_CHAIN = tuple(dict(layer) for layer in _CHAIN)


def _click_event(monotonic_ms: int = 1500,
                 label: str = "Running",
                 automation_id: str | None = None) -> RawCaptureEvent:
    return RawCaptureEvent(
        sequence=1,
        wall_time="2026-10-30T00:00:00Z",
        monotonic_ms=monotonic_ms,
        type="pointer_click",
        input={"button": "left"},
        observed_target=ObservedTarget(
            "OBS-1", "T0001", "sha256:x",
            automation_id=automation_id, identifier=None,
            control_type="Text", text=label,
            ancestry=_LABEL_CHAIN,
        ),
        scope=_SCOPE,
    )


def _recording(monotonic_ms: int = 1500) -> RawRecording:
    return RawRecording(
        name="t", session_id="s-dyn",
        events=[_click_event(monotonic_ms)],
        control_snapshots=(),
    )


def _snapshot_list() -> list[dict[str, Any]]:
    # Frame 1: same structural position showed "Idle" earlier.
    # Frame 2: the click-time frame shows "Running".
    return [
        _snapshot([_status_cell("Idle")], 1000, "initial"),
        _snapshot([_status_cell("Running")], 1200, "step"),
    ]


def _compile(snapshots: list[dict[str, Any]], *, event_label: str = "Running",
             event_automation_id: str | None = None):
    recording = RawRecording(
        name="t", session_id="s-dyn",
        events=[_click_event(label=event_label,
                             automation_id=event_automation_id)],
        control_snapshots=tuple(snapshots),
    )
    return compile_recording(recording)


def test_volatile_weak_label_across_frames_is_incomplete():
    result = _compile(_snapshot_list())
    assert result.case is not None
    steps = list(result.case.steps)
    assert len(steps) == 1
    step = steps[0]
    assert step.status == "incomplete", step.issues
    assert "compile_weak_identity_volatile_label" in step.issues
    # And the honest conclusion propagates to the case.
    assert result.case.status == "incomplete"

def test_strong_id_control_keeps_ready_despite_label_change():
    # Same alternate-frame label change, but the control carries a stable
    # automationId in the EVENT (so the synthesized selector is strong and
    # the locator drops the name comparison by design): identity is owned.
    snapshots = [
        _snapshot([_status_cell("Idle", automation_id="statusText")], 1000, "initial"),
        _snapshot([_status_cell("Running", automation_id="statusText")], 1200, "step"),
    ]
    result = _compile(snapshots, event_automation_id="statusText")
    steps = list(result.case.steps)
    assert len(steps) == 1
    assert steps[0].status == "ready", steps[0].issues


def test_stable_weak_label_across_frames_keeps_ready():
    # No volatility: both frames agree on the label; uniqueness path unchanged.
    snapshots = [
        _snapshot([_status_cell("Confirm")], 1000, "initial"),
        _snapshot([_status_cell("Confirm")], 1200, "step"),
    ]
    result = _compile(snapshots, event_label="Confirm")
    steps = list(result.case.steps)
    assert len(steps) == 1
    assert steps[0].status == "ready", steps[0].issues


def test_incomplete_frame_alone_cannot_trigger_volatile_gate():
    # The earlier frame is NOT complete (e.g. a virtualized table truncated
    # the walk and missed the alternate label): no volatility signal, so the
    # gate must stay silent — but the existing single-frame rules still apply.
    snapshots = [
        _snapshot([_status_cell("Idle")], 1000, "initial", complete=False),
        _snapshot([_status_cell("Running")], 1200, "step"),
    ]
    result = _compile(snapshots)
    steps = list(result.case.steps)
    assert len(steps) == 1
    assert steps[0].status == "ready", steps[0].issues
    assert "compile_weak_identity_volatile_label" not in steps[0].issues


def test_other_window_frame_is_not_volatility_evidence():
    # A same-shaped tree in a DIFFERENT window of the same recording shows
    # another label. That window is not this one; no inference.
    other_frame = _snapshot([_status_cell("Idle")], 1000, "initial")
    other_frame["window"] = dict(_WINDOW, handle=9999, rootAutomationId="otherWindow")
    snapshots = [
        other_frame,
        _snapshot([_status_cell("Running")], 1200, "step"),
    ]
    result = _compile(snapshots)
    steps = list(result.case.steps)
    assert len(steps) == 1
    assert steps[0].status == "ready", steps[0].issues
    assert "compile_weak_identity_volatile_label" not in steps[0].issues


def test_post_event_frame_is_not_volatility_evidence():
    # The flow's OWN click changed the label ("Start" -> "Stop") AFTER the
    # event. Replay resolves the selector before performing any action, so
    # post-event frames describe the replay's own future, not its starting
    # state. Only frames at or before the event carry volatility evidence.
    snapshots = [
        _snapshot([_status_cell("Running")], 1200, "step"),
        _snapshot([_status_cell("Stopped")], 2000, "post-click"),
    ]
    result = _compile(snapshots)
    steps = list(result.case.steps)
    assert len(steps) == 1
    assert steps[0].status == "ready", steps[0].issues
    assert "compile_weak_identity_volatile_label" not in steps[0].issues
