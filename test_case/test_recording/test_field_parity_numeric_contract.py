"""Field-semantics parity contract pinned at the node level.

Background (2026-10-28): ``test_compile_vs_replay_field_parity.py`` pinned the
compiler-candidate-count <-> replay-locator parity for mixed-type values
(numeric ``text`` / ``control_type``). It routed non-string values through
``ObservedTarget(...)``; the recording model now hard-rejects non-string
``text``/``control_type`` at the boundary (a stricter upstream guard), so that
old fixture can no longer construct an event. Those non-string cases are
therefore UNREACHABLE from the production recording path — the model is a
stronger guarantee than the compiler-level parity.

These tests pin the correct, reachable contract at the node-dict level (the
level the compiler and locator actually operate on) without relying on
constructing an ``ObservedTarget`` that production would refuse:

1. A weak selector name is a string (derived from string text). A snapshot
   node whose ``text`` is a non-string (e.g. an int) must NOT match it on
   either side: compiler count == locator count == 0, so the step is never
   promised ``ready``.
2. The recording model rejects non-string ``text`` / ``control_type`` — this
   is what makes the numeric-value case unreachable upstream.
3. An empty-name weak control yields no selector, so it cannot be ``ready``.
"""

from __future__ import annotations

import pytest

from agent.locator.hybrid import HybridLocator, LocatorRequest
from agent.recording.compiler import (
    _weak_selector_candidate_count,
    compile_recording,
)
from agent.recording.selectors import synthesize_selector
from target.recording.models import (
    CaptureScope,
    ObservedTarget,
    RawCaptureEvent,
    RawRecording,
    RecordingModelError,
)

_ANCHOR_CHAIN = (
    {"controlType": "Table", "automationId": "table"},
    {"controlType": "Group", "automationId": "base.tablePage"},
)


def _event(text="Details", ctype="DataItem", ss=None):
    target = ObservedTarget(
        "OBS-1", "T0001", "sha256:x", automation_id=None,
        control_type=ctype, text=text, ancestry=tuple(_ANCHOR_CHAIN),
        same_name_siblings=ss,
    )
    return RawCaptureEvent(
        sequence=1, wall_time="2026-08-18T00:00:00Z", monotonic_ms=1,
        type="pointer_click", scope=CaptureScope(
            "t", "windows_pywinauto", "EDRClient.exe", "^logo1$"),
        input={"button": "left"}, observed_target=target,
    )


def _node(text="Details", ctype="DataItem", ancestry=list(_ANCHOR_CHAIN)):
    n = {"control_type": ctype, "text": text, "automation_id": None,
         "target_id": "T1"}
    if ancestry is not None:
        n["ancestry"] = list(ancestry)
    return n


def _frame(node):
    return {"snapshotId": "S1", "capturedAtMs": 0, "reason": "probe",
            "durationMs": 0, "window": {"title": "logo1",
                                         "processName": "EDRClient.exe"},
            "contentHash": "h", "stateHash": "h", "controlCount": 1,
            "complete": True, "controls": [node]}


def _locator_count(control, node):
    bundle = {"observation_id": "OBS",
              "window_identity": {"native_id": "w"},
              "active_window": {"process_name": "EDRClient.exe",
                                 "title": "logo1"},
              "controls": [node]}
    req = LocatorRequest("gui.click", {"processName": "EDRClient.exe"}, control)
    return len(HybridLocator().collect_candidates(req, bundle))


def test_numeric_node_text_matches_no_one_compiler_and_locator_agree():
    """A snapshot node whose text is an int must not resolve for a weak
    selector whose name is the string '123' -- on BOTH the compiler and the
    locator. The compiler counts 0 (never ``ready``) exactly as the locator
    resolves 0 at replay."""
    ev = _event(text="123")               # selector name is "123" (str)
    control = synthesize_selector(ev).control
    assert control["name"] == "123"
    node = _node(text=123)                # raw snapshot node holds an int
    cc = _weak_selector_candidate_count(control, [_frame(node)], ev)
    lc = _locator_count(control, node)
    assert cc == lc == 0  # type: ignore[comparison-overlap]


def test_string_node_text_matches_on_both_sides():
    """The control case is symmetric: a string node text matches both sides
    (compiler 1 == locator 1), so matching is not accidentally disabled."""
    ev = _event(text="123")
    control = synthesize_selector(ev).control
    node = _node(text="123")
    cc = _weak_selector_candidate_count(control, [_frame(node)], ev)
    lc = _locator_count(control, node)
    assert cc == lc == 1  # type: ignore[comparison-overlap]


def test_model_rejects_non_string_text():
    """The recording model is the upstream guard: non-string ``text`` cannot
    even be modelled, so the numeric-value case is unreachable in production.
    This is the stricter guarantee that supersedes the old numeric fixture."""
    with pytest.raises(RecordingModelError):
        ObservedTarget("OBS-1", "T0001", "sha256:x", control_type="DataItem",
                       text=123, ancestry=tuple(_ANCHOR_CHAIN))


def test_model_rejects_non_string_control_type():
    with pytest.raises(RecordingModelError):
        ObservedTarget("OBS-1", "T0001", "sha256:x", control_type=7, text="D",
                       ancestry=tuple(_ANCHOR_CHAIN))


def test_empty_name_weak_control_is_not_ready():
    """An empty-name weak control yields no selector and compiles to
    ``incomplete`` (compile_selector_ambiguous), never ``ready``."""
    ev = _event(text="")
    assert synthesize_selector(ev) is None
    case = compile_recording(RawRecording(
        name="t", session_id="s1", events=(ev,),
        control_snapshots=(_frame(_node(text="")),))).case
    assert case.status == "incomplete"
    assert case.steps[0].status == "incomplete"
    assert "compile_selector_ambiguous" in case.steps[0].issues
