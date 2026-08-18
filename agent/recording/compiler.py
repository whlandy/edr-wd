"""Deterministic raw-recording compiler (Phase 0, offline)."""

from __future__ import annotations

import dataclasses
import hashlib
from dataclasses import dataclass, replace
from typing import Any, Iterable, Mapping

from target.recording.models import (
    VERIFIER_REQUIRED_EVENT_TYPES,
    RawCaptureEvent,
    RawRecording,
)

from .models import GoldenTrace, RecordedStep, RecordedTestCase, ReplaySelector, canonical_json
from .selectors import synthesize_drag_end_selector, synthesize_selector

_ACTION_MAP = {
    "pointer_click": "gui.click",
    "pointer_double_click": "pointer.double_click",
    "text_commit": "gui.type_text",
    "selection_change": "gui.select",
    "toggle_change": "gui.click",
    "scroll_commit": "pointer.scroll",
    "drag_commit": "pointer.drag",
}


_VERIFIER_REQUIRED_ISSUE = {
    "scroll_commit": (
        "compile_scroll_verifier_required",
        "recorded scroll requires an explicit content/position verifier before replay",
    ),
    "drag_commit": (
        "compile_drag_verifier_required",
        "recorded drag requires an explicit result verifier before replay",
    ),
}


@dataclass(frozen=True)
class CompileIssue:
    code: str
    sequence: int
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "sequence": self.sequence, "message": self.message}


@dataclass(frozen=True)
class CompilationResult:
    case: RecordedTestCase
    golden: GoldenTrace
    issues: tuple[CompileIssue, ...]
    #: Capture counts that do not by themselves invalidate a recording but
    #: explain a short one.  Out-of-scope input is normal (the user may switch
    #: windows), so it is reported rather than turned into a compile issue.
    diagnostics: Mapping[str, Any] = dataclasses.field(default_factory=dict)

    def case_json(self) -> str:
        return canonical_json(self.case.to_dict())

    def golden_json(self) -> str:
        return canonical_json(self.golden.to_dict())

    def report_dict(self) -> dict[str, Any]:
        return {
            "status": self.golden.status,
            "issues": [x.to_dict() for x in self.issues],
            "diagnostics": dict(self.diagnostics),
        }


def _same_target(left: RawCaptureEvent, right: RawCaptureEvent) -> bool:
    return bool(
        left.observed_target and right.observed_target
        and left.observed_target.fingerprint == right.observed_target.fingerprint
        and left.scope == right.scope
    )


def _scroll_point(event: RawCaptureEvent) -> tuple[int, int] | None:
    point = event.input.get("screenPoint")
    if not isinstance(point, (list, tuple)) or len(point) != 2:
        return None
    try:
        return int(point[0]), int(point[1])
    except (TypeError, ValueError):
        return None


def _same_scroll_origin(
    left: RawCaptureEvent, right: RawCaptureEvent, tolerance: int,
) -> bool:
    """Is this the same gesture, judged by where the pointer is?

    Not by which control is under it: scrolling moves content beneath a
    stationary pointer, so the hit-tested control changes several times inside
    a single flick.  Keying coalescing on control identity therefore shatters
    one gesture into as many steps as rows happened to pass by — in a live
    capture, 67 notches became six steps for that reason alone.  The pointer is
    what actually holds still.
    """
    if left.scope != right.scope:
        return False
    first, second = _scroll_point(left), _scroll_point(right)
    if first is None or second is None:
        return False
    return (
        abs(first[0] - second[0]) <= tolerance
        and abs(first[1] - second[1]) <= tolerance
    )


def _scroll_run_length(
    source: list[RawCaptureEvent], index: int, coalesce_ms: int,
    point_tolerance: int = 4,
) -> int:
    """How many consecutive notches belong to one scroll gesture.

    A wheel gesture arrives as a burst of notches, and a trackpad forwarded
    over RDP keeps sending a decaying tail after the finger lifts. Compiling
    each notch into its own step multiplies the verifier a user must bind and
    describes one flick as dozens of scrolls. Only a run from the same pointer
    position, in the same direction, within the burst window merges — reversing
    direction is a new gesture, and summing across it would cancel the movement
    out.
    """
    first = source[index]
    delta = first.input.get("delta", 0)
    if not isinstance(delta, (int, float)) or isinstance(delta, bool) or delta == 0:
        return 1
    sign = 1 if delta > 0 else -1
    length = 1
    while index + length < len(source):
        candidate = source[index + length]
        previous = source[index + length - 1]
        candidate_delta = candidate.input.get("delta", 0)
        if (
            candidate.type != "scroll_commit"
            or not isinstance(candidate_delta, (int, float))
            or isinstance(candidate_delta, bool)
            or candidate_delta == 0
            or (1 if candidate_delta > 0 else -1) != sign
            or not _same_scroll_origin(previous, candidate, point_tolerance)
            or candidate.monotonic_ms - previous.monotonic_ms > coalesce_ms
        ):
            break
        length += 1
    return length


def coalesce_events(
    events: Iterable[RawCaptureEvent],
    *,
    double_click_ms: int = 500,
    scroll_coalesce_ms: int = 500,
) -> tuple[RawCaptureEvent, ...]:
    """Coalesce deterministic pairs without discarding unsupported events."""
    source = list(events); result: list[RawCaptureEvent] = []; index = 0
    while index < len(source):
        current = source[index]
        if current.type == "scroll_commit":
            run = _scroll_run_length(source, index, scroll_coalesce_ms)
            if run > 1:
                last = source[index + run - 1]
                total = sum(source[index + offset].input.get("delta", 0) for offset in range(run))
                evidence = dict(last.evidence)
                if "beforeCapture" in current.evidence:
                    evidence["beforeCapture"] = current.evidence["beforeCapture"]
                if "captureError" in current.evidence:
                    evidence["captureError"] = current.evidence["captureError"]
                result.append(RawCaptureEvent(
                    sequence=current.sequence,
                    wall_time=last.wall_time,
                    monotonic_ms=last.monotonic_ms,
                    type="scroll_commit",
                    scope=current.scope,
                    input={**current.input, "delta": total, "notches": run},
                    observed_target=current.observed_target,
                    evidence=evidence,
                    # The gesture ends on the last notch, and a verifier the
                    # user binds afterwards binds to that notch's cause.
                    causal_id=last.causal_id,
                ))
                index += run
                continue
        recorded_double_click_ms = current.evidence.get("doubleClickIntervalMs")
        threshold = (
            recorded_double_click_ms
            if isinstance(recorded_double_click_ms, int)
            and not isinstance(recorded_double_click_ms, bool)
            else double_click_ms
        )
        if (
            current.type == "pointer_click" and index + 1 < len(source)
            and source[index + 1].type == "pointer_click"
            and current.input.get("button", "left") == "left"
            and source[index + 1].input.get("button", "left") == "left"
            and _same_target(current, source[index + 1])
            and source[index + 1].monotonic_ms - current.monotonic_ms <= threshold
            and current.causal_id is not None
            and current.causal_id == source[index + 1].causal_id
        ):
            second = source[index + 1]
            # A coalesced double click spans both click lifecycles: its before
            # frame is the first click's before, while its after frame is the
            # second click's capture.  Keeping only the second event evidence
            # would incorrectly present the first click's after frame as the
            # state before the double click.
            combined_evidence = dict(second.evidence)
            if "beforeCapture" in current.evidence:
                combined_evidence["beforeCapture"] = current.evidence["beforeCapture"]
            if "captureError" in current.evidence:
                combined_evidence["captureError"] = current.evidence["captureError"]
            result.append(RawCaptureEvent(
                sequence=current.sequence, wall_time=second.wall_time,
                monotonic_ms=second.monotonic_ms, type="pointer_double_click",
                scope=current.scope, input={**current.input, "clickCount": 2},
                observed_target=current.observed_target, evidence=combined_evidence,
                causal_id=current.causal_id or second.causal_id,
            ));index += 2;continue
        result.append(current);index += 1
    return tuple(result)


def _shares_action_cause(
    event: RawCaptureEvent,
    last_action_causal_id: str | None,
    has_last_action: bool,
) -> bool:
    return bool(
        event.causal_id is not None
        and event.causal_id == last_action_causal_id
        and has_last_action
    )


def _transition_is_bound(
    event: RawCaptureEvent,
    last_action_causal_id: str | None,
    has_last_action: bool,
) -> bool:
    return bool(
        event.type == "window_transition"
        and event.input.get("kind") in {"opened", "closed"}
        and _shares_action_cause(event, last_action_causal_id, has_last_action)
    )


def _assertion_is_bound(
    event: RawCaptureEvent,
    last_action_causal_id: str | None,
    has_last_action: bool,
) -> bool:
    """An assertion recorded with an explicit causal binding verifies that action.

    Unbound assertions keep their own observe-only step, which is what a user
    gets when they simply add a checkpoint without tying it to a preceding
    action.  Binding is what lets scroll and drag prove a result.
    """
    return bool(
        event.type == "assertion"
        and event.assertion is not None
        and _shares_action_cause(event, last_action_causal_id, has_last_action)
    )


def events_for_compiled_steps(events: Iterable[RawCaptureEvent]) -> tuple[RawCaptureEvent, ...]:
    """Return one source event per compiled step, including unbound transitions."""
    aligned: list[RawCaptureEvent] = []
    last_action_causal_id: str | None = None
    has_last_action = False
    for event in coalesce_events(events):
        if _transition_is_bound(event, last_action_causal_id, has_last_action):
            continue
        if _assertion_is_bound(event, last_action_causal_id, has_last_action):
            continue
        aligned.append(event)
        if event.type in _ACTION_MAP:
            last_action_causal_id = event.causal_id
            has_last_action = True
    return tuple(aligned)


def _compile_event(event: RawCaptureEvent, step_no: int) -> tuple[RecordedStep, list[CompileIssue]]:
    issues: list[CompileIssue] = []
    selector = synthesize_selector(event)
    end_selector: ReplaySelector | None = None
    action_id = _ACTION_MAP.get(event.type)
    if event.type == "pointer_click":
        action_id = {
            "left": "gui.click",
            "right": "pointer.right_click",
            "middle": "pointer.middle_click",
        }.get(event.input.get("button", "left"))
    elif (
        event.type == "pointer_double_click"
        and event.input.get("button", "left") != "left"
    ):
        action_id = None
    args: dict[str, Any] = {}
    verifiers: list[dict[str, Any]] = []

    if event.type == "assertion":
        action_id = None
        assertion = dict(event.assertion or {})
        if "expected" not in assertion:
            issues.append(CompileIssue("compile_assertion_missing_expected", event.sequence, "assertion requires explicit expected"))
        else:
            verifiers.append(assertion)
    elif action_id is None:
        issues.append(CompileIssue("compile_action_unsupported", event.sequence, f"unsupported event type {event.type!r}"))
    elif event.type == "text_commit":
        protected = event.observed_target and event.observed_target.protected
        if protected is True:
            source = event.input.get("textSource")
            if not isinstance(source, dict) or source.get("kind") != "env" or not source.get("name"):
                issues.append(CompileIssue("recording_secret_review_required", event.sequence, "protected text requires an environment textSource"))
            else:
                args["textSource"] = dict(source)
        elif protected is None:
            issues.append(CompileIssue("recording_secret_review_required", event.sequence, "text control sensitivity is unknown"))
        else:
            args["text"] = event.input.get("value", "")
    elif event.type in {"pointer_click", "pointer_double_click", "toggle_change"}:
        # The replay materializer derives a fresh semantic selector (or a
        # protected coordinate for double-click) from the current observation.
        # Recorded mouse button/coordinates and toggle values are evidence,
        # not backend keyword arguments.
        args = {}
    elif event.type == "selection_change":
        args = {"item": event.input.get("value")}
    elif event.type == "scroll_commit":
        delta = event.input.get("delta", 0)
        delta = delta if isinstance(delta, (int, float)) and not isinstance(delta, bool) else 0
        # WHEEL_DELTA is 120 per notch, but high-resolution sources — an RDP
        # client forwarding trackpad scroll, for one — report far smaller
        # units. Dividing those by 120 truncates a real gesture to "do not
        # scroll", so a gesture that moved always replays as at least one
        # notch in its own direction. The bound result verifier is what proves
        # the movement was enough; it fails loudly if it was not.
        clicks = int(delta / 120)
        if clicks == 0 and delta != 0:
            clicks = 1 if delta > 0 else -1
        args = {"clicks": clicks}
        # A scroll only proves something when an explicit content/position
        # verifier is bound to it.  compile_recording enforces that after
        # causally bound assertions have been folded into the step.
    elif event.type == "drag_commit":
        # Recorded screen coordinates are evidence only.  Both endpoints
        # replay as anchors inside freshly resolved control rectangles, and
        # compile_recording additionally requires a bound result verifier.
        args = {}
        duration = event.input.get("durationSeconds")
        if isinstance(duration, (int, float)) and not isinstance(duration, bool):
            args["duration"] = round(float(duration), 3)
        end_selector = synthesize_drag_end_selector(event)
        if end_selector is None:
            issues.append(CompileIssue(
                "compile_selector_ambiguous",
                event.sequence,
                "no stable semantic selector can be synthesized for the drag release point",
            ))
    else:
        args = {k: v for k, v in event.input.items() if k not in {"screenPoint", "clickCount"}}

    if event.type == "toggle_change" and "value" in event.input:
        verifiers.append({"type": "checked", "expected": event.input["value"]})
    if selector is None and event.type != "window_transition":
        issues.append(CompileIssue("compile_selector_ambiguous", event.sequence, "no stable semantic selector can be synthesized"))

    status = "ready" if not issues else "incomplete"
    return RecordedStep(
        step_id=f"step-{step_no:04d}", action_id=action_id, args=args,
        selector=selector, verifiers=tuple(verifiers), status=status,
        issues=tuple(x.code for x in issues), end_selector=end_selector,
    ), issues


def compile_recording(
    recording: RawRecording,
    *,
    catalog_version: str | None = None,
    catalog_digest: str | None = None,
    profile: str | None = None,
) -> CompilationResult:
    if profile is not None and (not isinstance(profile, str) or not profile):
        raise ValueError("profile must be a non-empty string or None")
    if catalog_version is None or catalog_digest is None:
        from target.action_catalog import CATALOG_VERSION, catalog_digest as current_digest

        catalog_version = catalog_version or CATALOG_VERSION
        catalog_digest = catalog_digest or current_digest()
    events = coalesce_events(recording.events)
    steps: list[RecordedStep] = []; issues: list[CompileIssue] = []
    if not events:
        issues.append(CompileIssue(
            "compile_recording_empty", 0,
            "recording contains no accepted lifecycle events",
        ))
    dropped_packets = recording.capture_diagnostics.get("droppedPackets", 0)
    correlation_errors = recording.capture_diagnostics.get("correlationErrorCount", 0)
    if dropped_packets:
        issues.append(CompileIssue(
            "recording_packets_dropped", 0,
            f"capture queue dropped {dropped_packets} native input packet(s)",
        ))
    if correlation_errors:
        issues.append(CompileIssue(
            "recording_correlation_errors", 0,
            f"capture correlation reported {correlation_errors} error(s)",
        ))
    last_action_index: int | None = None
    last_action_causal_id: str | None = None
    verifier_required: dict[int, tuple[str, int]] = {}

    def bind_verifier(index: int, verifier: dict[str, Any], event: RawCaptureEvent) -> None:
        """Fold a causally bound verifier into the action step it proves."""
        previous = steps[index]
        step = replace(previous, verifiers=(*previous.verifiers, verifier))
        if event.evidence.get("captureError"):
            capture_issue = CompileIssue(
                "recording_capture_evidence_unavailable",
                event.sequence,
                "a required lifecycle screenshot could not be captured",
            )
            issues.append(capture_issue)
            step = replace(
                step, status="incomplete", issues=(*step.issues, capture_issue.code),
            )
        steps[index] = step

    for event in events:
        if _assertion_is_bound(event, last_action_causal_id, last_action_index is not None):
            assert last_action_index is not None
            bind_verifier(last_action_index, dict(event.assertion or {}), event)
            continue
        if event.type == "window_transition":
            transition_kind = event.input.get("kind")
            bound = _transition_is_bound(
                event, last_action_causal_id, last_action_index is not None,
            )
            if bound:
                expected = {
                    "exists": transition_kind == "opened",
                    "processName": event.input.get("processName") or event.scope.process_name,
                }
                if event.input.get("title"):
                    expected["title"] = event.input["title"]
                if event.input.get("titleRegex"):
                    expected["titleRegex"] = event.input["titleRegex"]
                verifier = {
                    "type": "window_open",
                    "expected": expected,
                    "timeoutSeconds": event.input.get("timeoutSeconds", 10.0),
                }
                bind_verifier(last_action_index, verifier, event)
                continue
            issue = CompileIssue(
                "compile_transition_unbound",
                event.sequence,
                "window transition must share a causal ID with the preceding action",
            )
            issues.append(issue)
            selector = ReplaySelector(
                window={
                    "processName": event.scope.process_name,
                    "titleRegex": event.scope.window_title,
                },
                control={},
            )
            steps.append(RecordedStep(
                step_id=f"step-{len(steps) + 1:04d}",
                action_id=None,
                args={},
                selector=selector,
                status="incomplete",
                issues=(issue.code,),
            ))
            continue
        step, event_issues = _compile_event(event, len(steps) + 1)
        if event.evidence.get("captureError"):
            capture_issue = CompileIssue(
                "recording_capture_evidence_unavailable",
                event.sequence,
                "a required lifecycle screenshot could not be captured",
            )
            step = replace(
                step,
                status="incomplete",
                issues=(*step.issues, capture_issue.code),
            )
            event_issues.append(capture_issue)
        steps.append(step);issues.extend(event_issues)
        if step.action_id is not None:
            last_action_index = len(steps) - 1
            last_action_causal_id = event.causal_id
            if event.type in VERIFIER_REQUIRED_EVENT_TYPES:
                verifier_required[last_action_index] = (event.type, event.sequence)

    for index, (event_type, sequence) in verifier_required.items():
        if steps[index].verifiers:
            continue
        code, message = _VERIFIER_REQUIRED_ISSUE[event_type]
        issue = CompileIssue(code, sequence, message)
        issues.append(issue)
        steps[index] = replace(
            steps[index], status="incomplete", issues=(*steps[index].issues, code),
        )
    status = "ready" if not issues else "incomplete"
    case = RecordedTestCase(recording.name, recording.session_id, tuple(steps), status)
    recording_digest = "sha256:" + hashlib.sha256(canonical_json(recording.to_dict()).encode()).hexdigest()
    first_scope = events[0].scope if events else None
    environment = {
        "backend": first_scope.backend if first_scope else "",
        "application": first_scope.process_name if first_scope else "",
        "target": first_scope.target if first_scope else "",
    }
    if profile is not None:
        environment["profile"] = profile
    golden = GoldenTrace(
        name=recording.name,
        source_recording={"schema": recording.schema, "sha256": recording_digest},
        catalog={"version": catalog_version, "digest": catalog_digest},
        environment=environment,
        steps=tuple(steps), status=status,
    )
    out_of_scope = recording.capture_diagnostics.get("outOfScopeEvents", 0)
    diagnostics = {"outOfScopeEvents": int(out_of_scope or 0)}
    return CompilationResult(case, golden, tuple(issues), diagnostics)
