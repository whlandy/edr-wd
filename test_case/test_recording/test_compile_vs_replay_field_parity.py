"""Compiler candidate-count ↔ replay locator field semantics parity guard.

Defect being pinned (found adversarially on 2026-10-28):

``agent/recording/compiler.py::_weak_selector_candidate_count`` decides whether
a weak-identity step compiles to ``ready`` by counting candidates in the
recorded control tree. It claimed to use "the same field semantics the replay
locator uses" (``agent/locator/hybrid.py::collect_candidates``), but the two
did NOT agree on mixed-type values:

- compiler compared ``str(node["text"]) != str(selector_name)`` (string-coerced);
- locator compared ``node[field] != selector_name`` (raw equality).

So for a control whose observed ``text``/``control_type`` is not a string (e.g.
a numeric-valued cell), the compiler reported 1 candidate (and marked the step
``ready``) while the locator found 0 candidates at replay -> the step that was
promised ``ready`` could never resolve (replay_target_not_found). A silent
failure toward “false ready”, the same class of dishonesty the ancestry
replay defect exposed.

These tests pin the fix: the compiler's candidate count must read fields and
compare values EXACTLY the way the replay locator does (first-present field
lookup, raw equality, no str() coercion on the node side), so a ``ready`` step
never has fewer locator candidates than the compiler counted. The locator
remains the authority: it is never relaxed.
"""

from __future__ import annotations

import pytest

from agent.locator.hybrid import HybridLocator, LocatorRequest
from agent.recording.compiler import (
    _weak_selector_candidate_count,
    _get_field,
)
from agent.recording.selectors import synthesize_selector
from target.recording.models import CaptureScope, ObservedTarget, RawCaptureEvent

_ANCHOR_CHAIN = (
    {"controlType": "Table", "automationId": "table"},
    {"controlType": "Group", "automationId": "base.tablePage"},
)


# --------------------------------------------------------------------------- #
# Helpers: the same logical control fed through both sides
# --------------------------------------------------------------------------- #
def _event_for(text="Details", ctype="DataItem", ancestry=_ANCHOR_CHAIN,
               automation_id=None):
    # The recorded ObservedTarget is the *selector* side: the recording model
    # requires control_type/text to be string-or-null (UIA names are strings).
    # Non-string node values below are tested on the *replay/node* side where a
    # plain dict may legitimately hold them.
    target = ObservedTarget(
        "OBS-1", "T0001", "sha256:x",
        automation_id=automation_id,
        control_type=str(ctype), text=str(text),
        ancestry=tuple(ancestry), same_name_siblings=None,
    )
    return RawCaptureEvent(
        sequence=1, wall_time="2026-08-18T00:00:00Z", monotonic_ms=1,
        type="pointer_click", scope=CaptureScope("t", "windows_pywinauto",
                                                 "EDRClient.exe", "^logo1$"),
        input={"button": "left"}, observed_target=target,
    )


def _compiler_count(control: dict, node: dict) -> int | None:
    """Run the compiler's weak-selector candidate counter on one node frame."""
    frame = {"snapshotId": "S1", "capturedAtMs": 0, "reason": "probe",
             "durationMs": 0, "window": {"title": "logo1",
                                          "processName": "EDRClient.exe"},
             "contentHash": "h", "stateHash": "h", "controlCount": 1,
             "complete": True, "controls": [node]}
    ev = _event_for()
    return _weak_selector_candidate_count(control, [frame], ev)


def _locator_count(control: dict, node: dict) -> int:
    """Run HybridLocator.collect_candidates on the same node as a live bundle."""
    bundle = {
        "observation_id": "OBS",
        "window_identity": {"native_id": "w"},
        "active_window": {"process_name": "EDRClient.exe", "title": "logo1"},
        "controls": [node],
    }
    req = LocatorRequest("gui.click", {"processName": "EDRClient.exe"}, control)
    return len(HybridLocator().collect_candidates(req, bundle))


def _node(text="Details", ctype="DataItem", ancestry=_ANCHOR_CHAIN,
          automation_id=None, target_id="T1"):
    n = {"control_type": ctype, "text": text, "automation_id": automation_id,
         "target_id": target_id}
    if ancestry is not None:
        n["ancestry"] = list(ancestry)
    return n


def _control_from_event(ev) -> dict:
    """The selector control exactly as the compiler / locator see it."""
    return synthesize_selector(ev).control


# --------------------------------------------------------------------------- #
# Field-semantic parity: compiler count == locator count per node
# --------------------------------------------------------------------------- #
# Each case states the selector identity (what the recorded ObservedTarget
# carries -- always string-typed, matching the recording model contract) and
# the *node* value (what the replay observation / recorded tree may carry, a
# plain dict that is not string-constrained). Parity must hold for the SAME
# node even when the locator's raw equality rejects it: the compiler must
# never count a candidate the locator refuses.
@pytest.mark.parametrize("sel_text,sel_ctype,node_text,node_ctype,label", [
    ("Details", "DataItem", "Details", "DataItem", "canonical-string"),
    ("123", "DataItem", 123, "DataItem", "numeric-text"),
    ("Details", "7", "Details", 7, "numeric-controlType"),
    ("Details", "DataItem", "", "DataItem", "empty-text"),
])
def test_compiler_and_locator_agree_field_for_field(sel_text, sel_ctype,
                                                    node_text, node_ctype,
                                                    label):
    """For the SAME control, the compiler's candidate count and the locator's
    must agree. Before the fix, a numeric ``text`` (node 123 vs selector
    \"123\") made the compiler count 1 (string-coerced) while the locator
    counted 0 -- a step promised ``ready`` that could never resolve at
    replay. Non-string node values (numeric / empty) must stay 0 on BOTH
    sides: the compiler mirrors the locator's raw equality, never
    over-counting toward ``ready``."""
    assert _get_field  # reference the mirrored helper so it stays importable
    ctrl = _control_from_event(_event_for(text=sel_text, ctype=sel_ctype))
    node = _node(text=node_text, ctype=node_ctype)
    cc = _compiler_count(ctrl, node)
    lc = _locator_count(ctrl, node)
    # The compiler must never over-count relative to the locator: if the
    # locator resolves 0 candidates, the compile step must not be ready.
    # They must agree (both 0 or both 1) -- never compiler>locator.
    assert cc == lc, (
        f"[{label}] compiler count {cc} != locator count {lc}: "
        "compiler may promise `ready` for a step replay cannot resolve")


def test_locator_name_field_uses_raw_equality_not_coercion():
    """Pin the locator's authority (raw equality): a node with text 123 (int)
    must NOT match a selector name '123' (str). This is the semantic the
    compiler now mirrors; relaxing it would be a safety change."""
    ctrl = _control_from_event(_event_for(text="123"))
    node = _node(text=123)
    assert _locator_count(ctrl, node) == 0


def test_compiler_does_not_mark_numeric_text_ready_when_locator_rejects():
    """End-to-end honesty: a weak-identity click whose recorded tree text is a
    number compiles `incomplete` (compile_weak_identity_unresolved), never
    `ready`, because the replay locator would resolve 0 candidates for it."""
    from agent.recording.compiler import compile_recording
    from target.recording.models import RawRecording

    ev = _event_for(text=123)
    snap = {"snapshotId": "S1", "capturedAtMs": 0, "reason": "probe",
            "durationMs": 0, "window": {"title": "logo1",
                                         "processName": "EDRClient.exe"},
            "contentHash": "h", "stateHash": "h", "controlCount": 1,
            "complete": True, "controls": [_node(text=123)]}
    case = compile_recording(RawRecording(name="t", session_id="s1",
                                          events=(ev,),
                                          control_snapshots=(snap,))).case
    assert case.status == "incomplete"
    assert case.steps[0].status == "incomplete"
    assert "compile_weak_identity_unresolved" in case.steps[0].issues
