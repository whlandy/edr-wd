"""Full-point step-plan extraction from a raw recording (design 12 §5).

The repair layer (trace_repair.py) rewrites what a failed compilation
already produced; this layer prepares the rerecord: it maps EVERY raw
business input point — not just the subset the compiler accepted — to an
edr-wd executable step plan. Events the compiler folded, bound away, or
refused still appear here, either as their own step or explicitly
classified as capture lifecycle; the coverage triple (raw events,
planned, capture-lifecycle) must add up or construction fails, so a
silent drop is an exception, not a report line.

The plan is a recovery-layer document, not an executor. Action kinds are
constrained by the existing action catalog (`target.action_catalog`):
side_effect=gui_mutation kinds are the ones the rerecord layer must run
behind a real recording; observe.* kinds are read-only. Unsupported
input (compile_action_unsupported) becomes an explicit gap — never a
guessed click. Raw content (titles, control text) is untrusted
observation data: it lands in `observed` fields verbatim and is never
interpreted as instruction.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Mapping

from agent.recording.compiler import CompilationResult
from agent.recording.models import canonical_json
from agent.recording.selectors import synthesize_selector
from target.action_catalog import ACTIONS_V1, get_spec
from target.recording.models import RawCaptureEvent, RawRecording

#: Raw event types that are the capture pipeline's own lifecycle, not the
#: user's business flow. They appear in the coverage ledger but never
#: become execute steps.
_CAPTURE_LIFECYCLE_TYPES = frozenset({"flush", "snapshots"})

#: Raw event types that were recorded as user intent on the target app.
#: key_command is included on purpose: the compiler refuses it today
#: (compile_action_unsupported) — the plan still carries the point, with
#: an explicit gap, because a dropped point is exactly what this layer
#: exists to prevent.
_BUSINESS_EVENT_TYPES = frozenset({
    "pointer_click", "pointer_double_click", "text_commit",
    "selection_change", "toggle_change", "scroll_commit", "drag_commit",
    "key_command",
})

#: Raw event types that observe or assert rather than mutate.
_OBSERVATION_EVENT_TYPES = frozenset({"assertion", "window_transition"})

#: Raw event types the schema can carry. Anything else is a contract
#: violation worth failing on — an unknown event type silently planned as
#: "capture lifecycle" would be a hole in the ledger.
_KNOWN_EVENT_TYPES = (
    _BUSINESS_EVENT_TYPES | _OBSERVATION_EVENT_TYPES | _CAPTURE_LIFECYCLE_TYPES
)

#: Verifier kinds an assertion event can map to in the plan's expectation
#: record. assertion.type drives the read-only catalog action the rerecord
#: layer would use to re-check it (observe.control_text / is_window_open).
#: Keys are the raw schema's assertion vocabulary
#: (target.recording.models._validate_assertion).
_ASSERTION_OBSERVE_ACTION = {
    "window_open": "observe.window_open",
    "window_text_contains": "observe.control_text",
    "window_text_contains_time": "observe.control_text",
    "window_text_time_range": "observe.control_text",
    "text_equals": "observe.control_text",
    "text_contains": "observe.control_text",
    "text_contains_time": "observe.control_text",
    "text_time_range": "observe.control_text",
    "value_equals": "observe.control_text",
    "visible": "observe.control_text",
    "enabled": "observe.control_text",
    "checked": "observe.control_text",
}

_DEFAULT_WAIT_SECONDS = 10.0

_INTENT_EXECUTE = "execute"
_INTENT_VERIFY = "verify"
_INTENT_WINDOW_TRANSITION = "window-transition"
_INTENT_CAPTURE_LIFECYCLE = "capture-lifecycle"


class StepPlanError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class PlanStep:
    """One planned point: the raw fact, the classification, the gap."""
    step_id: str
    order: int
    intent: str
    source_event_refs: tuple[int, ...]
    observed: Mapping[str, Any]
    inferred: Mapping[str, Any]
    window_role: Mapping[str, Any]
    target_resolution: Mapping[str, Any]
    action: Mapping[str, Any]
    preconditions: tuple[Mapping[str, Any], ...]
    expectation: Mapping[str, Any]
    wait_seconds: float
    side_effects: Mapping[str, Any]
    failure_policy: str
    recording_marker: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "stepId": self.step_id,
            "order": self.order,
            "intent": self.intent,
            "sourceEventRefs": list(self.source_event_refs),
            "observed": dict(self.observed),
            "inferred": dict(self.inferred),
            "windowRole": dict(self.window_role),
            "targetResolution": dict(self.target_resolution),
            "action": dict(self.action),
            "preconditions": [dict(p) for p in self.preconditions],
            "expectation": dict(self.expectation),
            "waitSeconds": self.wait_seconds,
            "sideEffects": dict(self.side_effects),
            "failurePolicy": self.failure_policy,
            "recordingMarker": dict(self.recording_marker),
        }


@dataclass(frozen=True)
class StepPlan:
    """The full-point plan plus the coverage ledger that proves it."""
    parent_sha256: str
    golden_status: str
    golden_ref_step_ids: tuple[str, ...]
    steps: tuple[PlanStep, ...]
    coverage: Mapping[str, int]
    gaps: tuple[Mapping[str, Any], ...]
    uncompiled_points: int
    notes: tuple[str, ...] = field(default=())

    def to_dict(self) -> dict[str, Any]:
        return {
            "parentSha256": self.parent_sha256,
            "goldenStatus": self.golden_status,
            "goldenRefStepIds": list(self.golden_ref_step_ids),
            "steps": [s.to_dict() for s in self.steps],
            "coverage": dict(self.coverage),
            "gaps": [dict(g) for g in self.gaps],
            "uncompiledPoints": self.uncompiled_points,
            "notes": list(self.notes),
        }


def recording_sha256(recording: RawRecording) -> str:
    return "sha256:" + hashlib.sha256(
        canonical_json(recording.to_dict()).encode()).hexdigest()


def _scope_window_role(event: RawCaptureEvent) -> dict[str, Any]:
    evidence_window = event.evidence.get("window") or {}
    role: dict[str, Any] = {}
    if isinstance(evidence_window, Mapping):
        for key in ("handle", "instanceId"):
            if evidence_window.get(key) is not None:
                role[key] = evidence_window[key]
    role["processName"] = event.scope.process_name
    role["title"] = event.scope.window_title
    return role


def _observed_target_record(event: RawCaptureEvent) -> dict[str, Any]:
    target = event.observed_target
    if target is None:
        return {}
    out: dict[str, Any] = {
        "snapshotId": target.snapshot_id,
        "fingerprint": target.fingerprint,
        "controlType": target.control_type,
    }
    # Untrusted content fields: verbatim observation references, never
    # instructions (design 12 §5 / R13). They describe what the recorder
    # saw; nothing in this layer acts on their text.
    if target.automation_id is not None:
        out["automationId"] = target.automation_id
    if target.identifier is not None:
        out["identifier"] = target.identifier
    if target.text is not None:
        out["text"] = target.text
    if target.rect is not None:
        out["rect"] = list(target.rect)
    if target.ancestry:
        out["ancestry"] = [dict(a) for a in target.ancestry]
    if target.protected is not None:
        out["protected"] = target.protected
    return out


def _catalog_action(action_id: str) -> dict[str, Any]:
    spec = get_spec(ACTIONS_V1, action_id)
    if spec is None:
        raise StepPlanError(
            "catalog_action_unknown",
            f"plan references {action_id!r} which the catalog does not define",
        )
    return {"actionId": action_id, "tool": spec.tool_name,
            "sideEffect": spec.side_effect}


def _execute_action_for(event: RawCaptureEvent) -> dict[str, Any]:
    """Map one business event to its catalog action + parameter source.

    key_command has NO catalog correspondence (the compiler already
    refuses it) — the plan records the gap explicitly instead of
    substituting a click (design 12 §5: 特殊事件不能硬编成点击).
    """
    etype = event.type
    if etype == "pointer_click":
        button = event.input.get("button", "left")
        mapping = {"left": "gui.click", "right": "pointer.right_click",
                   "middle": "pointer.middle_click"}
        action_id = mapping.get(button)
        if action_id is None:
            return {"actionId": None, "sideEffect": None,
                    "paramSource": {"button": button},
                    "gap": {"code": "pointer_button_unsupported",
                            "detail": f"no catalog action for button {button!r}",
                            "resolvableBy": "agent_adapter"}}
        entry = _catalog_action(action_id)
        entry["paramSource"] = {"rawInput": ["button"]}
        return entry
    if etype == "pointer_double_click":
        entry = _catalog_action("pointer.double_click")
        entry["paramSource"] = {"rawInput": []}
        return entry
    if etype == "text_commit":
        entry = _catalog_action("gui.type_text")
        protected = event.observed_target.protected if event.observed_target else None
        if protected is True:
            source = event.input.get("textSource")
            entry["paramSource"] = {"textSourceRef": source
                                    if isinstance(source, Mapping) else None}
        else:
            entry["paramSource"] = {"rawInput": ["value"]}
        return entry
    if etype == "selection_change":
        entry = _catalog_action("gui.select")
        entry["paramSource"] = {"rawInput": ["value"]}
        return entry
    if etype == "toggle_change":
        entry = _catalog_action("gui.click")
        entry["paramSource"] = {"rawInput": []}
        return entry
    if etype == "scroll_commit":
        entry = _catalog_action("pointer.scroll")
        entry["paramSource"] = {"rawInput": ["delta"]}
        return entry
    if etype == "drag_commit":
        entry = _catalog_action("pointer.drag")
        entry["paramSource"] = {"rawInput": ["durationSeconds", "endTarget"]}
        return entry
    # key_command and any other business-typed event without a catalog
    # action: explicit gap, agent-adapter resolvable only when the event
    # carries an observed target to reason about.
    has_target = event.observed_target is not None
    return {"actionId": None, "sideEffect": None,
            "paramSource": {"rawInput": sorted(event.input.keys())},
            "gap": {"code": "compile_action_unsupported",
                    "detail": f"unsupported event type {etype!r}",
                    "resolvableBy": "agent_adapter" if has_target else "unresolvable"}}


def _expectation_for(event: RawCaptureEvent) -> dict[str, Any]:
    if event.type == "assertion" and event.assertion is not None:
        assertion = dict(event.assertion)
        observe = _ASSERTION_OBSERVE_ACTION.get(str(assertion.get("type")))
        return {"expectationSource": "recorded_assertion",
                "expected": assertion.get("expected"),
                "assertionType": assertion.get("type"),
                "observeActionId": observe}
    if event.type == "toggle_change" and "value" in event.input:
        return {"expectationSource": "recorded_assertion",
                "expected": event.input.get("value"),
                "assertionType": "checked", "observeActionId": None}
    return {"expectationSource": "pending_confirmation", "expected": None,
            "assertionType": None, "observeActionId": None}


def _preconditions_for(event: RawCaptureEvent) -> tuple[dict[str, Any], ...]:
    """Window-presence conditions inferred from the event's own window role."""
    role = event.evidence.get("window") or {}
    if isinstance(role, Mapping) and role.get("instanceId") is not None:
        return ({"kind": "window_present", "instanceId": role["instanceId"]},)
    return ()


def _window_transition_step(event: RawCaptureEvent, step_no: int) -> PlanStep:
    kind = event.input.get("kind")
    title = event.input.get("title")
    observe = "observe.window_open"
    action: dict[str, Any] = {"actionId": observe, "tool": "is_window_open",
                              "sideEffect": "none",
                              "paramSource": {"rawInput": ["kind", "title"]}}
    expected = {"exists": kind == "opened"}
    if title:
        expected["title"] = title
    pre: tuple[dict[str, Any], ...] = ()
    if kind == "opened":
        pre = ({"kind": "prior_action_opens_window",
                "causalId": event.causal_id},)
    return PlanStep(
        step_id=f"plan-{step_no:04d}", order=step_no,
        intent=_INTENT_WINDOW_TRANSITION,
        source_event_refs=(event.sequence,),
        observed={"transitionKind": kind, "title": title,
                  "processName": event.input.get("processName")
                  or event.scope.process_name},
        inferred={"classify": "bound or unbound window transition; the "
                             "rerecord layer checks presence, it does not "
                             "click windows into existing"},
        window_role=_scope_window_role(event),
        target_resolution={"strategy": "window_presence_check"},
        action=action,
        preconditions=pre,
        expectation={"expectationSource": "recorded_transition",
                     "expected": expected,
                     "assertionType": "window_open", "observeActionId": observe},
        wait_seconds=float(event.input.get("timeoutSeconds",
                                           _DEFAULT_WAIT_SECONDS)),
        side_effects={"sideEffect": "none", "idempotent": True},
        failure_policy="skip_blocked",
        recording_marker={"causalId": event.causal_id,
                          "rawType": event.type},
    )


def _capture_lifecycle_step(event: RawCaptureEvent, step_no: int) -> PlanStep:
    return PlanStep(
        step_id=f"plan-{step_no:04d}", order=step_no,
        intent=_INTENT_CAPTURE_LIFECYCLE,
        source_event_refs=(event.sequence,),
        observed={"rawType": event.type},
        inferred={"classify": "capture pipeline lifecycle event; recorded "
                             "for ledger completeness, never executed"},
        window_role=_scope_window_role(event),
        target_resolution={"strategy": "none"},
        action={"actionId": None, "tool": None, "sideEffect": None,
                "paramSource": {}},
        preconditions=(),
        expectation={"expectationSource": "none", "expected": None,
                     "assertionType": None, "observeActionId": None},
        wait_seconds=0.0,
        side_effects={"sideEffect": "none", "idempotent": True},
        failure_policy="skip_blocked",
        recording_marker={"causalId": event.causal_id,
                          "rawType": event.type},
    )


def _plan_step_for(event: RawCaptureEvent, step_no: int,
                   golden_ref: str | None) -> PlanStep:
    etype = event.type
    if etype in _CAPTURE_LIFECYCLE_TYPES:
        return _capture_lifecycle_step(event, step_no)
    if etype == "window_transition":
        return _window_transition_step(event, step_no)
    action = _execute_action_for(event)
    gap = action.get("gap")
    if etype == "assertion":
        intent = _INTENT_VERIFY
        action = {"actionId": None, "tool": None, "sideEffect": "none",
                  "paramSource": {"rawInput": sorted(event.input.keys())}}
    else:
        intent = _INTENT_EXECUTE
    selector = synthesize_selector(event)
    needs_fresh = selector is None
    if gap is not None:
        needs_fresh = True
    resolution: dict[str, Any] = {
        "strategy": "recorded_selector" if selector is not None
        else "needs_fresh_observation",
    }
    if selector is not None:
        resolution["selector"] = selector.to_dict()
    if gap is not None:
        resolution["note"] = ("no compiled selector exists for this "
                              "uncompiled point; the rerecord layer's "
                              "constrained agent must resolve a current "
                              "control by observation, never by guessing")
    observed = _observed_target_record(event)
    if etype == "assertion":
        observed["assertion"] = dict(event.assertion or {})
    side_effect = action.get("sideEffect")
    return PlanStep(
        step_id=f"plan-{step_no:04d}", order=step_no, intent=intent,
        source_event_refs=(event.sequence,),
        observed=observed,
        inferred={"classify": f"{etype} on the recorded target",
                  "goldenRefStepId": golden_ref},
        window_role=_scope_window_role(event),
        target_resolution=resolution,
        action=action,
        preconditions=_preconditions_for(event),
        expectation=_expectation_for(event),
        wait_seconds=_DEFAULT_WAIT_SECONDS,
        side_effects={"sideEffect": side_effect,
                      "idempotent": etype == "scroll_commit"},
        failure_policy="plan_blocked" if (gap is not None
                                          and gap.get("resolvableBy") == "unresolvable")
        else "retry_once",
        recording_marker={"causalId": event.causal_id, "rawType": etype},
    )


def extract_step_plan(
    recording: RawRecording,
    result: CompilationResult,
) -> StepPlan:
    """Map every raw business input point to a plan step (design 12 §5).

    Coverage is the load-bearing invariant: every raw event sequence lands
    in exactly one step — execute/verify/window-transition steps for
    business flow, capture-lifecycle steps for the recorder's own
    events — and the ledger triple (rawEvents, planned,
    captureLifecycle) must add up, else extraction fails loudly. Events
    the compiler folded or dropped (merged navigation keys, title-less
    bound transitions) still appear here; their fold is recorded via the
    compile diagnostics, never silently inherited.
    """
    events = recording.events
    seen_types = {e.type for e in events}
    unknown_types = seen_types - _KNOWN_EVENT_TYPES
    if unknown_types:
        raise StepPlanError(
            "raw_event_type_unknown",
            f"raw events carry types outside the known vocabulary: "
            f"{sorted(unknown_types)}; refusing to classify them by guess",
        )
    by_step = (result.diagnostics.get("sourceEvents") or {}).get("byStep") or {}
    seq_to_golden: dict[int, str] = {}
    for step_id, seqs in by_step.items():
        for seq in seqs:
            seq_to_golden.setdefault(int(seq), step_id)
    steps: list[PlanStep] = []
    gaps: list[dict[str, Any]] = []
    executed = 0
    lifecycle = 0
    for event in events:
        if event.type in _CAPTURE_LIFECYCLE_TYPES:
            lifecycle += 1
        else:
            executed += 1
        step = _plan_step_for(event, len(steps) + 1,
                              seq_to_golden.get(event.sequence))
        steps.append(step)
        gap = step.action.get("gap")
        if gap is not None:
            gaps.append({"stepId": step.step_id,
                         "sequence": event.sequence, **gap})
    raw_total = len(events)
    planned_total = len(steps)
    if planned_total != executed + lifecycle or planned_total != raw_total:
        raise StepPlanError(
            "coverage_ledger_broken",
            f"plan dropped points: raw={raw_total} planned={planned_total} "
            f"execute_or_observe={executed} lifecycle={lifecycle}",
        )
    for seq in seq_to_golden:
        if not any(seq in s.source_event_refs for s in steps):
            raise StepPlanError(
                "coverage_ledger_broken",
                f"compile-mapped sequence {seq} is missing from the plan; "
                "the compile/source mapping and the raw ledger disagree",
            )
    uncompiled = sum(
        1 for s in steps
        if s.action.get("actionId") is None
        and s.intent not in (_INTENT_CAPTURE_LIFECYCLE, _INTENT_WINDOW_TRANSITION)
    )
    folded = []
    if result.diagnostics.get("coalescedSources"):
        folded.append("coalescedSources")
    if result.diagnostics.get("mergedNavigationKeyEvents"):
        folded.append("mergedNavigationKeyEvents")
    notes = (
        ["plan references golden steps via inferred.goldenRefStepId; the "
         "golden subset is NOT the plan — every raw point appears even "
         "when the compiler dropped it"]
        + [f"compile fold records consulted: {name}" for name in folded]
    )
    return StepPlan(
        parent_sha256=recording_sha256(recording),
        golden_status=result.golden.status,
        golden_ref_step_ids=tuple(
            s.step_id for s in result.golden.steps),
        steps=tuple(steps),
        coverage={"rawEvents": raw_total, "planned": planned_total,
                  "businessOrObserve": executed,
                  "captureLifecycle": lifecycle},
        gaps=tuple(gaps),
        uncompiled_points=uncompiled,
        notes=tuple(notes),
    )


__all__ = [
    "PlanStep",
    "StepPlan",
    "StepPlanError",
    "extract_step_plan",
    "recording_sha256",
]
