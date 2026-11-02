"""Deterministic raw-recording compiler (Phase 0, offline)."""

from __future__ import annotations

import dataclasses
import hashlib
import re
from dataclasses import dataclass, replace
from typing import Any, Iterable, Mapping

from target.recording.models import (
    VERIFIER_REQUIRED_EVENT_TYPES,
    RawCaptureEvent,
    RawRecording,
)

from .models import (GOLDEN_TRACE_SCHEMA_V2, EntryWindowIdentity, GoldenTrace,
                     RecordedStep, RecordedTestCase, ReplaySelector,
                     canonical_json)
from .selectors import (
    assertion_match, synthesize_drag_end_selector, synthesize_selector,
    window_for_event,
)

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

_CONTROL_ASSERTION_TYPES = frozenset({
    "text_equals", "text_contains", "value_equals", "visible", "checked",
    "enabled", "text_contains_time", "text_time_range",
})


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


#: Bare cursor-position keys.  They move the caret and never change the edit
#: value, and a recorded ``text_commit`` carries the whole materialized field
#: value that replays through ``set_edit_text`` — a full replacement that does
#: not depend on caret position.  Only this provably value-neutral class may be
#: merged into an immediately following same-Edit commit; every other
#: ``key_command`` (modifiers, ENTER/TAB/arrows/…) stays explicitly unsupported.
_NAVIGATION_MERGE_KEYS = frozenset({"HOME", "END"})


def _plain_navigation_key(event: RawCaptureEvent) -> bool:
    return (
        event.type == "key_command"
        and event.input.get("key") in _NAVIGATION_MERGE_KEYS
        and event.input.get("modifiers", []) == []
    )


def _merge_identity(nav: RawCaptureEvent, commit: RawCaptureEvent) -> bool:
    """One navigation key may merge only if IT matches the commit target.

    Applied to every candidate individually — never just the first key of a
    run.  Both sides must be plain non-protected Edits recorded in the same
    scope AND the same captured window; a missing window handle is not an
    identity (``None == None`` must never pass as "same window").
    """
    nav_target, commit_target = nav.observed_target, commit.observed_target
    if not (nav_target and commit_target):
        return False
    if nav_target.fingerprint != commit_target.fingerprint:
        return False
    for target in (nav_target, commit_target):
        if target.control_type != "Edit" or target.protected is not False:
            return False
    if nav.scope != commit.scope:
        return False
    nav_handle = (nav.evidence.get("window") or {}).get("handle")
    commit_handle = (commit.evidence.get("window") or {}).get("handle")
    if nav_handle is None or commit_handle is None:
        return False
    return nav_handle == commit_handle


def _navigation_merges(
    events: tuple[RawCaptureEvent, ...],
) -> tuple[dict[int, list[dict[str, Any]]], set[int]]:
    """Fold bare HOME/END runs into the text_commit they immediately precede.

    Returns the per-commit merge records (for the compile report) and the set
    of key_command sequences consumed by a merge.  Adjacency is judged on the
    aligned event list: any intervening event breaks the run and the keys stay
    unsupported.  Every key of a run must individually satisfy the merge
    identity against the commit — one stray member blocks the whole run, whose
    earlier keys are no longer adjacent to the commit either.  Raw captures are
    never modified — merging is a compile-time decision that is reported, not a
    silent drop.
    """
    merges: dict[int, list[dict[str, Any]]] = {}
    merged_sequences: set[int] = set()
    index = 0
    while index < len(events):
        if not _plain_navigation_key(events[index]):
            index += 1
            continue
        run_start = index
        while index < len(events) and _plain_navigation_key(events[index]):
            index += 1
        run_end = index
        if run_end >= len(events):
            break
        commit = events[run_end]
        if commit.type != "text_commit" or not all(
            _merge_identity(events[offset], commit)
            for offset in range(run_start, run_end)
        ):
            continue
        merges[commit.sequence] = [
            {
                "sequence": events[offset].sequence,
                "key": events[offset].input.get("key"),
                "mergedIntoSequence": commit.sequence,
            }
            for offset in range(run_start, run_end)
        ]
        merged_sequences.update(
            events[offset].sequence for offset in range(run_start, run_end)
        )
    return merges, merged_sequences


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


def _opposing_window_verifier(
    step: RecordedStep, kind: str, identity: str,
) -> Mapping[str, Any] | None:
    """The verifier this transition cancels out, if the step already has one."""
    wanted = kind != "opened"
    for verifier in step.verifiers:
        if verifier.get("type") != "window_open":
            continue
        expected = verifier.get("expected")
        if not isinstance(expected, Mapping):
            continue
        observed = expected.get("title") or expected.get("titleRegex")
        if observed == identity and bool(expected.get("exists")) is wanted:
            return verifier
    return None


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


def _region_query_verifier(event: RawCaptureEvent) -> dict[str, Any] | None:
    """Flatten a region_query assertion event into its golden verifier.

    ``match`` is the container's own stable identity (automationId /
    identifier / controlType+name), computed through the same selector
    machinery as every other assertion.  A container without a stable
    identity returns ``None`` — the caller must fail the step instead of
    degrading the region query to a screen coordinate.
    """
    assertion = dict(event.assertion or {})
    draft = dict(assertion.get("draft") or {})
    target = dict(draft.get("target") or {})
    match = assertion_match(event)
    if match is None:
        return None
    return {
        "type": "region_query",
        "match": match,
        "target": {
            "mode": target.get("mode"),
            "components": [
                {key: value for key, value in component.items() if key != "sampleValue"}
                for component in (target.get("components") or ())
                if isinstance(component, Mapping)
            ],
        },
        "quantifier": draft.get("quantifier"),
        "conditions": draft.get("conditions"),
        "contentTime": draft.get("contentTime"),
        "observation": draft.get("observation"),
        "timeoutSeconds": assertion.get("timeoutSeconds", 60.0),
    }


_REGION_CONTAINER_ISSUE = (
    "compile_region_container_unresolved",
    "region assertion container has no stable identity; refusing to degrade to coordinates",
)


def _cross_window_bound_assertion(
    event: RawCaptureEvent, action_event: RawCaptureEvent | None,
) -> bool:
    """A content verifier must observe its own window after the action.

    Window-open assertions use the product inventory and can remain attached
    to the action. Older recordings without an assertion-window stamp retain
    their legacy binding because their destination cannot be determined.
    """
    return bool(
        action_event is not None
        and isinstance(event.evidence.get("window"), Mapping)
        and (event.assertion or {}).get("type") != "window_open"
        and (
            window_for_event(event) != window_for_event(action_event)
            or _different_recorded_handles(event, action_event)
        )
    )


def _different_recorded_handles(event: RawCaptureEvent, action_event: RawCaptureEvent) -> bool:
    """Use HWND only to recognize distinct windows within one recording."""
    current = event.evidence.get("window")
    previous = action_event.evidence.get("window")
    if not isinstance(current, Mapping) or not isinstance(previous, Mapping):
        return False
    current_handle, previous_handle = current.get("handle"), previous.get("handle")
    return bool(
        isinstance(current_handle, int) and not isinstance(current_handle, bool)
        and isinstance(previous_handle, int) and not isinstance(previous_handle, bool)
        and current_handle != previous_handle
    )


def events_for_compiled_steps(events: Iterable[RawCaptureEvent]) -> tuple[RawCaptureEvent, ...]:
    """Return one source event per compiled step, including unbound transitions.

    Applies the same navigation-key merge filter as ``compile_recording`` so
    the returned events stay one-per-step: a merged bare HOME/END produces no
    step, so it must not appear here either — otherwise the artifact projection
    zips evidence frames onto the wrong steps.
    """
    aligned_events = coalesce_events(events)
    _, merged_nav_sequences = _navigation_merges(aligned_events)
    aligned: list[RawCaptureEvent] = []
    last_action_causal_id: str | None = None
    has_last_action = False
    last_action_event: RawCaptureEvent | None = None
    for event in aligned_events:
        if event.sequence in merged_nav_sequences:
            continue
        if _transition_is_bound(event, last_action_causal_id, has_last_action):
            continue
        if (_assertion_is_bound(event, last_action_causal_id, has_last_action)
                and not _cross_window_bound_assertion(event, last_action_event)):
            continue
        aligned.append(event)
        if event.type in _ACTION_MAP:
            last_action_causal_id = event.causal_id
            has_last_action = True
            last_action_event = event
    return tuple(aligned)


def _get_field(node: Mapping, *names: str) -> Any:
    """Mirror of HybridLocator's field lookup (hybrid.py ``_get``).

    Returns the value of the first *present* key (even when it holds ``None``
    or ``""``), not the first truthy one. The compiler's weak-selector candidate
    count must read fields exactly the way the replay locator does, or a step it
    marks ``ready`` can still fail to resolve at replay.
    """
    for name in names:
        if name in node:
            return node[name]
    return None


def _ancestry_prefix_matches(actual: Any, expected: Any) -> bool:
    """Mirror of HybridLocator's ancestry check (hybrid.py collect_candidates).

    Expected is the selector's recorded ancestor chain (camelCase, nearest
    parent first); actual is one node's observed chain. The recorded chain
    must be a strict prefix of the observed one, field by field, with empty
    selector values ignored -- keeping the same semantics both ends use.
    """
    if not expected:
        return True
    if not isinstance(actual, (list, tuple)) or len(actual) < len(expected):
        return False
    for index, expected_item in enumerate(expected):
        if not isinstance(expected_item, Mapping):
            continue
        actual_item = actual[index]
        if not isinstance(actual_item, Mapping):
            return False
        for key, value in expected_item.items():
            if value in (None, ""):
                continue
            if (str(actual_item.get(key)
                    or actual_item.get(re.sub(r"(?<!^)(?=[A-Z])", "_", key).lower())
                    or "") != str(value)):
                return False
    return True


def _frame_window_matches(frame_window: Mapping, event_window: Mapping) -> bool:
    """Whether a recorded snapshot window could hold the event's window.

    The window ``handle`` is the most precise identity within one recording:
    a process may own several same-titled windows with the same tree root.
    When both the frame and the event carry an integer handle they must agree;
    only when a handle is missing on one side do we fall back to the weaker
    signals (``processName``, then ``rootAutomationId``/``titleRegex``).
    """
    ev_handle = event_window.get("handle")
    fr_handle = frame_window.get("handle")
    if isinstance(ev_handle, int) and not isinstance(ev_handle, bool) \
            and isinstance(fr_handle, int) and not isinstance(fr_handle, bool):
        return ev_handle == fr_handle
    fname = str(frame_window.get("processName") or "")
    ename = str(event_window.get("processName") or "")
    if fname and ename and fname != ename:
        return False
    croot = event_window.get("rootAutomationId")
    froot = frame_window.get("rootAutomationId")
    if croot and froot:
        return str(froot) == str(croot)
    title_regex = event_window.get("titleRegex")
    title = str(frame_window.get("title") or "")
    if title_regex and title:
        try:
            return re.fullmatch(str(title_regex), title) is not None
        except re.error:
            return False
    return True


def _event_handle(event: RawCaptureEvent) -> int | None:
    """The event's window handle, when its recorded evidence names one.

    ``window_for_event`` intentionally drops the handle (the replay selector's
    allowed window fields do not include it), so the frame binder re-reads it
    directly from the event's own captured window evidence.
    """
    if event is None or not isinstance(event.evidence, Mapping):
        return None
    recorded = event.evidence.get("window")
    if not isinstance(recorded, Mapping):
        return None
    handle = recorded.get("handle")
    if isinstance(handle, int) and not isinstance(handle, bool):
        return handle
    return None


def _select_event_snapshot(event: RawCaptureEvent,
                           snapshots: Iterable[Mapping]) -> Mapping | None:
    """The single recorded frame that describes the event's window at the event.

    Uniqueness must be judged against one observation, not a pool. A window is
    walked repeatedly as a flow moves between page states, so the same unique
    control legitimately appears in several snapshots; summing matches across
    all of them counts that one control twice and falsely reports ambiguity.
    Snapshot windows of a *different* window (same-named tree) must also not
    inflate the count. Pick the frame bound to the event's window whose
    ``capturedAtMs`` is the latest at or before the event, else the nearest
    following one.
    """
    if event is None:
        return None
    event_window = window_for_event(event)
    ev_handle = _event_handle(event)
    if ev_handle is not None:
        event_window["handle"] = ev_handle
    candidates: list[tuple[int, Mapping]] = []
    nearest_after: tuple[int, Mapping] | None = None
    event_ms = int(event.monotonic_ms or 0)
    saw_any = False
    for snapshot in snapshots:
        if not isinstance(snapshot, Mapping):
            continue
        window = snapshot.get("window")
        if not isinstance(window, Mapping):
            continue
        saw_any = True
        if not _frame_window_matches(window, event_window):
            continue
        captured = int(snapshot.get("capturedAtMs") or 0)
        if captured <= event_ms:
            candidates.append((captured, snapshot))
        elif nearest_after is None or captured < nearest_after[0]:
            nearest_after = (captured, snapshot)
    if not candidates:
        return nearest_after[1] if nearest_after is not None else None
    candidates.sort(key=lambda pair: pair[0])
    return candidates[-1][1]


def _weak_label_volatile(control: Mapping,
                         snapshots: Iterable[Mapping],
                         event: RawCaptureEvent) -> bool:
    """Recorded proof that the label at this structural position changes.

    A weak-identity selector resolves at replay BY its name: the replay
    locator only drops the name comparison for strong-ID controls. If any
    COMPLETE frame of the SAME window captured AT OR BEFORE the event shows a
    different text at the same position (same controlType, ancestry-prefix
    matched with the same rule the uniqueness count uses), the label is
    volatile and the step would resolve to nothing at replay. Post-event
    frames are the flow's OWN doing (a Start button legitimately reads "Stop"
    after being clicked) and replay resolves the selector before any action,
    so they are not volatility evidence either. Frames from other windows and
    incomplete walks carry no signal.
    """
    ctype = control.get("controlType")
    name = control.get("name")
    ancestry = control.get("ancestry") or ()
    if not ctype and not name:
        return False
    event_window = window_for_event(event)
    ev_handle = _event_handle(event)
    if ev_handle is not None:
        event_window["handle"] = ev_handle
    event_ms = int(event.monotonic_ms or 0)
    for snapshot in snapshots:
        if not isinstance(snapshot, Mapping):
            continue
        if int(snapshot.get("capturedAtMs") or 0) > event_ms:
            continue
        window = snapshot.get("window")
        if not isinstance(window, Mapping):
            continue
        if not _frame_window_matches(window, event_window):
            continue
        if snapshot.get("complete") is not True:
            continue
        controls = snapshot.get("controls")
        nodes = controls if isinstance(controls, (list, tuple)) else (snapshot,)
        for node in nodes:
            if not isinstance(node, Mapping):
                continue
            if ctype and _get_field(node, "control_type", "controlType", "role") != ctype:
                continue
            if ancestry and not _ancestry_prefix_matches(node.get("ancestry") or (), ancestry):
                continue
            if _get_field(node, "text", "name", "title") != name:
                return True
    return False


def _weak_selector_candidate_count(control: Mapping,
                                   snapshots: Iterable[Mapping],
                                   event: RawCaptureEvent | None = None) -> int | None:
    """Count full-tree controls matching a weak selector within ONE frame.

    This is the authority the compile-time correctness of a weak identity
    depends on. A recorder-side direct-sibling count (`same_name_siblings`)
    proves only that the near parent showed duplicates -- it cannot rule out
    other rows a virtualized table has not loaded, nor a second parent whose
    chain serializes to the same ancestry. Counting against the recorded
    control tree of the event's window, with the same field semantics the
    replay locator uses, tells the compiler whether the selector really
    resolves to exactly one candidate in the frame the event occurred.

    The count is taken within a single event-bound frame: the same unique
    control captured across page-state snapshots must not be counted twice,
    and a same-named tree in another window must not inflate it. Returns
    ``None`` when the selector is not matchable (no usable fields), no
    event-bound frame exists, or the frame carries no tree -- in any of which
    uniqueness cannot be asserted.
    """
    ctype = control.get("controlType")
    name = control.get("name")
    ancestry = control.get("ancestry") or ()
    if not ctype and not name:
        return None
    frame = _select_event_snapshot(event, snapshots) if event is not None else None
    # A weak-identity uniqueness decision may only lean on a tree the producer
    # proved complete (`complete` is True). Absent, non-boolean, depth/child-
    # count-truncated, or a children-enumeration exception all mean a duplicate
    # may live beyond the traversal, so a partial tree can never vouch for
    # `ready`. Old snapshots and field-missing trees default to incomplete.
    # Strong-identity selectors are exempt: a stable id is unique regardless of
    # tree extent, so completeness must not over-block them.
    if not (control.get("automationId") or control.get("identifier")):
        if frame is None or frame.get("complete") is not True:
            return None
    nodes: Iterable[Mapping] = ()
    if frame is not None:
        controls = frame.get("controls")
        nodes = controls if isinstance(controls, (list, tuple)) else (frame,)
    elif event is None:
        # No event context (defensive): fall back to the whole pool, which the
        # reviewer flags as unsafe -- kept only for callers without an event.
        for snapshot in snapshots:
            if not isinstance(snapshot, Mapping):
                continue
            controls = snapshot.get("controls")
            if isinstance(controls, (list, tuple)):
                nodes = (*nodes, *controls)
            else:
                nodes = (*nodes, snapshot)
    if frame is None and event is not None:
        return None
    count = 0
    saw_any = False
    for node in nodes:
        if not isinstance(node, Mapping):
            continue
        saw_any = True
        # Field lookups and comparisons mirror HybridLocator.collect_candidates
        # exactly (raw equality, first-present field, no str() coercion on the
        # node side). Coercing here would count a candidate the replay locator
        # rejects -- a step we mark `ready` that can never resolve at replay.
        if ctype and _get_field(node, "control_type", "controlType", "role") != ctype:
            continue
        if name and (_get_field(node, "text", "name", "title") != name):
            continue
        if ancestry and not _ancestry_prefix_matches(node.get("ancestry") or (), ancestry):
            continue
        count += 1
    if not saw_any:
        return None
    return count


def _compile_event(event: RawCaptureEvent, step_no: int,
                   snapshots: Iterable[Mapping] = ()) -> tuple[RecordedStep, list[CompileIssue]]:
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
        if assertion.get("type") == "region_query":
            verifier = _region_query_verifier(event)
            if verifier is None:
                issues.append(CompileIssue(
                    _REGION_CONTAINER_ISSUE[0], event.sequence,
                    _REGION_CONTAINER_ISSUE[1],
                ))
            else:
                verifiers.append(verifier)
        elif "expected" not in assertion:
            issues.append(CompileIssue("compile_assertion_missing_expected", event.sequence, "assertion requires explicit expected"))
        else:
            if assertion.get("type") in _CONTROL_ASSERTION_TYPES:
                match = assertion_match(event)
                if match:
                    assertion["match"] = match
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

    # Honest ready/incomplete: a step is only `ready` when the identity that
    # will actually be resolved at replay can be proven unique. Judge this on
    # the *final synthesized selector* -- not the hit-tested row. A strong
    # automationId/identifier (e.g. a scroll_commit elevated to its stable
    # Table container) owns its identity and is never ambiguous here. A weak
    # selector (controlType+name+ancestry only) must not be promised `ready`
    # merely because the recorder saw one direct sibling: a virtualized table
    # may not have loaded every row, and two different parents can serialize
    # to the same ancestry. Count candidates across the full recorded control
    # tree with the same field semantics the replay locator uses; only a
    # proven-unique weak selector stays `ready`.
    selector_control = selector.control if selector is not None else None
    strong = bool((selector_control or {}).get("automationId")
                  or (selector_control or {}).get("identifier"))
    has_identity = bool((selector_control or {}).get("controlType")
                        or (selector_control or {}).get("name")
                        or strong)
    if selector_control is not None and has_identity and not strong:
        candidate_count = _weak_selector_candidate_count(selector_control, snapshots, event)
        if candidate_count is None or candidate_count == 0:
            issues.append(CompileIssue(
                "compile_weak_identity_unresolved", event.sequence,
                "weak-identity target has no recorded control tree to prove "
                "uniqueness against; capture with a stable automation id or "
                "confirm a semantic identity, or replay cannot be guaranteed",
            ))
        elif candidate_count > 1:
            issues.append(CompileIssue(
                "compile_weak_or_ambiguous_identity", event.sequence,
                f"weak-identity selector matches {candidate_count} controls in "
                "the recorded window; capture a stable row key/content or "
                "confirm a semantic identity, or this step cannot resolve "
                "uniquely at replay",
            ))
        else:
            # candidate_count == 1: the captured tree looked unique, but the
            # recorder's own direct sibling observation may contradict it. The
            # click-time count (`same_name_siblings`) is a lower bound the
            # debounced background snapshot can undercount when a virtualized
            # table has not loaded every row. If the recorder directly saw more
            # than one same-name control than the tree accounts for, the tree
            # is inconsistent and "unique" is not proven -- promising `ready`
            # would fail exactly as the 11.26 live replay did (compile `ready`,
            # replay ambiguous on six identical rows). Refuse rather than guess.
            siblings = event.observed_target.same_name_siblings
            if isinstance(siblings, int) and not isinstance(siblings, bool) and siblings > 1:
                issues.append(CompileIssue(
                    "compile_weak_identity_tree_undercount", event.sequence,
                    f"recorder observed {siblings} same-name siblings but the "
                    "recorded control tree shows only one candidate; the tree "
                    "undercounts (a virtualized table may not have loaded every "
                    "row), so uniqueness cannot be proven. Re-capture with the "
                    "table fully loaded, capture a stable row key/content, or "
                    "confirm a semantic identity before this step can resolve "
                    "uniquely at replay",
                ))
            # The replay locator resolves a weak-identity control BY its name
            # ("Recorded labels (notably clocks, log rows and status text) may
            # legitimately change by replay" -- agent/locator/hybrid.py only
            # drops the name comparison for strong-ID controls). If the same
            # structural position (controlType + ancestry) carried a different
            # label in another COMPLETE frame of the same window, the label at
            # this position is recorded-proven volatile and replay would see a
            # different name: candidate_count=0, replay_target_not_found.
            # Promising `ready` would blindside exactly as the dynamic-label
            # replay failures did. Complete frames only: an incomplete walk
            # that misses the alternate label is no volatility signal, and a
            # different window's same-shaped tree is not evidence about this
            # one (the same window-match rule as the uniqueness count).
            elif _weak_label_volatile(selector_control, snapshots, event):
                issues.append(CompileIssue(
                    "compile_weak_identity_volatile_label", event.sequence,
                    "the same control position (controlType + ancestry) showed a "
                    "different label in another recorded frame of this window, so "
                    "the label this selector resolves by is volatile and replay "
                    "would not find it; capture a stable identity (automationId) "
                    "or anchor to content that does not change between capture "
                    "and replay",
                ))

    status = "ready" if not issues else "incomplete"
    return RecordedStep(
        step_id=f"step-{step_no:04d}", action_id=action_id, args=args,
        selector=selector, verifiers=tuple(verifiers), status=status,
        issues=tuple(x.code for x in issues), end_selector=end_selector,
    ), issues


def _entry_window_identity(
    first_step: RecordedStep | None,
    first_source_event: RawCaptureEvent | None,
    recording: RawRecording,
) -> tuple[EntryWindowIdentity | None, list[CompileIssue]]:
    """Derive the optional entry-window identity record (design doc 08, R2 scope).

    One record from the first executable step (the same selector the replay's
    golden_entry_scope would take — assertion selectors included, matching the
    implemented contract). Selector, manifest entry and control snapshot must
    all describe the SAME recorded window, correlated by that event's handle:
    a different window of the same process must never vouch for the entry
    identity. Any contradiction or missing evidence means no record is
    emitted and the golden stays incomplete — replay never gets a weakened
    title-only match.
    """
    issues: list[CompileIssue] = []
    selector = first_step.selector if first_step is not None else None
    if selector is None:
        return None, issues
    window = selector.window
    process_name = str(window.get("processName") or "")
    if not process_name:
        return None, issues
    root = window.get("rootAutomationId")
    title_regex = window.get("titleRegex")
    if not root and not title_regex:
        # window_for_event guarantees one of the two; a selector without both
        # cannot be verified against the manifest.
        return None, issues
    # Correlate by the source event's recorded handle. An empty manifest
    # means a v1-era recording without window tracking: no evidence either
    # way, so the v1 semantics apply and no diagnostic is raised (compat
    # matrix, design 08 §C6) -- the handle is simply not demanded there.
    if not recording.windows:
        return None, issues  # v1-era recording: legacy semantics, no record
    entry_handle = _event_handle(first_source_event)
    if entry_handle is None:
        issues.append(CompileIssue(
            "entry_identity_unverifiable", 0,
            "the first executable step has no recorded window handle, so the "
            "selector cannot be tied to one recorded window; no entryWindow "
            "record is emitted and entry-window recovery will be refused",
        ))
        return None, issues
    # A manifest that never recorded THIS window as already_open is positive
    # contradicting evidence: the entry window did not exist before the flow.
    manifest_entry = next(
        (w for w in recording.windows
         if w.get("handle") == entry_handle
         and str(w.get("processName") or "") == process_name),
        None,
    )
    if manifest_entry is None:
        issues.append(CompileIssue(
            "entry_identity_no_manifest_match", 0,
            "the first executable step's window (by recorded handle) has no "
            "manifest entry, or its process differs; the entry identity "
            "cannot be verified against the recording, no entryWindow record "
            "is emitted and entry-window recovery will be refused",
        ))
        return None, issues
    if manifest_entry.get("origin") != "already_open":
        issues.append(CompileIssue(
            "entry_identity_no_manifest_match", 0,
            "the entry window was opened BY the recorded flow (origin="
            f"{manifest_entry.get('origin')!r}), not present before it; a "
            "declared identity would claim a precondition the recording "
            "contradicts. No entryWindow record is emitted and entry-window "
            "recovery will be refused",
        ))
        return None, issues
    # The corroborating snapshot must describe the SAME handle: roots are
    # aggregated per window, never per process.
    snapshot_roots = {
        str((s.get("window") or {}).get("rootAutomationId") or "")
        for s in recording.control_snapshots
        if isinstance(s.get("window"), Mapping)
        and s.get("window").get("handle") == entry_handle
    }
    snapshot_roots.discard("")
    if root and snapshot_roots and root not in snapshot_roots:
        issues.append(CompileIssue(
            "entry_identity_conflict", 0,
            "the first step's window root identity does not match the root "
            "recorded in the control snapshots of the same recorded window; "
            "contradictory recording evidence, no entryWindow record emitted",
        ))
        return None, issues
    return EntryWindowIdentity(
        process_name=process_name,
        title_regex=str(title_regex) if title_regex else None,
        root_automation_id=str(root) if root else None,
        origin="already_open",
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
    navigation_merges, merged_nav_sequences = _navigation_merges(events)
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
    recorder_ui_events = recording.capture_diagnostics.get("recorderUiEvents", 0)
    if recorder_ui_events:
        issues.append(CompileIssue(
            "recording_recorder_ui_events", 0,
            f"the recorder panel swallowed {recorder_ui_events} input event(s) "
            "aimed at the target",
        ))
    unidentified_events = recording.capture_diagnostics.get("unidentifiedTargetEvents", 0)
    if unidentified_events:
        issues.append(CompileIssue(
            "recording_unidentified_target_events", 0,
            f"{unidentified_events} input event(s) landed on nothing the "
            "application can name",
        ))
    last_action_index: int | None = None
    last_action_causal_id: str | None = None
    last_action_event: RawCaptureEvent | None = None
    verifier_required: dict[int, tuple[str, int]] = {}
    cross_window_verified_actions: set[int] = set()
    # Replay locks ONE window per collapsed identity; two action steps whose
    # recorded handles differ but whose replay window identity is identical
    # would both run inside whichever same-titled window replay locked — the
    # recorded flow spanned two coexisting windows that replay cannot tell
    # apart. Handles are within-recording proof of distinct windows only, so
    # every step that shares the collapsed identity is flagged, not just the
    # drift's edge.
    collapsed_window_steps: dict[str, tuple[int, int]] = {}
    first_selector_event: RawCaptureEvent | None = None

    def _flag_window_identity_ambiguous(step_index: int, sequence: int) -> None:
        step = steps[step_index]
        if "compile_window_identity_ambiguous" in step.issues:
            return
        steps[step_index] = replace(
            step, status="incomplete",
            issues=(*step.issues, "compile_window_identity_ambiguous"),
        )
        issues.append(CompileIssue(
            "compile_window_identity_ambiguous",
            sequence,
            "distinct recorded windows share one replay selector; capture a stable window root identity",
        ))

    def bind_verifier(index: int, verifier: dict[str, Any], event: RawCaptureEvent) -> None:
        """Fold a causally bound verifier into the action step it proves."""
        previous = steps[index]
        if event.type == "assertion" and verifier.get("type") in _CONTROL_ASSERTION_TYPES:
            match = assertion_match(event)
            if match is None:
                selector_issue = CompileIssue(
                    "compile_assertion_selector_ambiguous", event.sequence,
                    "bound control assertion has no stable selector of its own",
                )
                issues.append(selector_issue)
                previous = replace(
                    previous, status="incomplete",
                    issues=(*previous.issues, selector_issue.code),
                )
            else:
                verifier = {**verifier, "match": match}
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
        if event.sequence in merged_nav_sequences:
            # Merged into the immediately following same-Edit text_commit;
            # reported in diagnostics, never silently dropped from the capture.
            continue
        ambiguous_bound_window = False
        if _assertion_is_bound(event, last_action_causal_id, last_action_index is not None):
            assert last_action_index is not None
            if _cross_window_bound_assertion(event, last_action_event):
                # Keep the causal proof for verifier-required scroll/drag, but
                # execute the assertion after focusing its destination window.
                cross_window_verified_actions.add(last_action_index)
                ambiguous_bound_window = bool(
                    last_action_event is not None
                    and _different_recorded_handles(event, last_action_event)
                    and window_for_event(event) == window_for_event(last_action_event)
                )
            else:
                verifier_payload = dict(event.assertion or {})
                if verifier_payload.get("type") == "region_query":
                    region_verifier = _region_query_verifier(event)
                    if region_verifier is None:
                        container_issue = CompileIssue(
                            _REGION_CONTAINER_ISSUE[0], event.sequence,
                            _REGION_CONTAINER_ISSUE[1],
                        )
                        issues.append(container_issue)
                        steps[last_action_index] = replace(
                            steps[last_action_index], status="incomplete",
                            issues=(
                                *steps[last_action_index].issues,
                                container_issue.code,
                            ),
                        )
                        continue
                    verifier_payload = region_verifier
                bind_verifier(last_action_index, verifier_payload, event)
                continue
        if event.type == "window_transition":
            transition_kind = event.input.get("kind")
            bound = _transition_is_bound(
                event, last_action_causal_id, last_action_index is not None,
            )
            if bound:
                identity = event.input.get("title") or event.input.get("titleRegex")
                if not identity:
                    # A transient window with no title cannot be identified,
                    # so asserting on it would assert on any window at all.
                    continue
                previous = steps[last_action_index]
                opposite = _opposing_window_verifier(previous, transition_kind, identity)
                if opposite is not None:
                    # It opened and closed again inside the same action. The
                    # end state is unchanged, and keeping either half asserts
                    # something the step did not leave behind — keeping both
                    # asserts a window is present and absent at once.
                    steps[last_action_index] = replace(
                        previous,
                        verifiers=tuple(
                            v for v in previous.verifiers if v is not opposite
                        ),
                    )
                    continue
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
        step, event_issues = _compile_event(event, len(steps) + 1,
                                            recording.control_snapshots)
        if ambiguous_bound_window:
            identity_issue = CompileIssue(
                "compile_window_identity_ambiguous", event.sequence,
                "distinct recorded windows share one replay selector; capture a stable window root identity",
            )
            step = replace(step, status="incomplete", issues=(*step.issues, identity_issue.code))
            event_issues.append(identity_issue)
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
        if step.selector is not None and first_selector_event is None:
            first_selector_event = event
        if step.action_id is not None:
            previous_action_event = last_action_event
            last_action_index = len(steps) - 1
            last_action_causal_id = event.causal_id
            last_action_event = event
            if event.type in VERIFIER_REQUIRED_EVENT_TYPES:
                verifier_required[last_action_index] = (event.type, event.sequence)
            # Same-titled-window drift: this action's replay window identity
            # matches an earlier action's while the recorded handles prove two
            # distinct windows. Flag the whole collapsed group, back to the
            # first member.
            if step.selector is not None:
                identity = canonical_json(step.selector.window)
                first_index, first_sequence = collapsed_window_steps.get(
                    identity, (last_action_index, event.sequence),
                )
                if (
                    last_action_index != first_index
                    and previous_action_event is not None
                    and _different_recorded_handles(event, previous_action_event)
                ):
                    _flag_window_identity_ambiguous(last_action_index, event.sequence)
                    _flag_window_identity_ambiguous(first_index, first_sequence)
                if identity not in collapsed_window_steps:
                    collapsed_window_steps[identity] = (last_action_index, event.sequence)

    def _run_is_verified(index: int) -> bool:
        """Is this step covered by a verifier bound later in the same run?

        Scrolling twice and asserting once is one claim about where the list
        ended up, not two. Demanding separate proof for the intermediate
        gesture asks the user to verify a state they never asserted, so a
        verifier bound to a later step of an uninterrupted run on the same
        target satisfies the earlier ones too.
        """
        step = steps[index]
        for later in steps[index + 1:]:
            if later.action_id != step.action_id or later.selector != step.selector:
                return False
            if later.verifiers:
                return True
        return False

    for index, (event_type, sequence) in verifier_required.items():
        if index in cross_window_verified_actions or steps[index].verifiers or _run_is_verified(index):
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
    first_selector_step = next(
        (step for step in steps if step.selector is not None), None)
    # first_selector_event was captured in the compile loop above: the source
    # event of the first step that carries a selector. Its recorded handle
    # ties the selector, the manifest entry and the control snapshots to ONE
    # window (review P1-1: a same-process sibling window must never vouch).
    entry_identity, entry_issues = _entry_window_identity(
        first_selector_step, first_selector_event, recording)
    # Entry-identity evidence problems close the ready gate: a golden whose
    # entry identity could not be verified must not pass the replay threshold
    # as "ready" (review P1-2). The v1-era compat path emits no issues, so
    # old recordings keep compiling exactly as before.
    if entry_issues:
        status = "incomplete"
        case = RecordedTestCase(
            case.name, case.source_session_id, case.steps, status)
    issues.extend(entry_issues)
    if entry_identity is not None:
        golden = GoldenTrace(
            name=recording.name,
            source_recording={"schema": recording.schema, "sha256": recording_digest},
            catalog={"version": catalog_version, "digest": catalog_digest},
            environment=environment,
            steps=tuple(steps), status=status,
            schema=GOLDEN_TRACE_SCHEMA_V2,
            entry_window=entry_identity,
        )
    else:
        golden = GoldenTrace(
            name=recording.name,
            source_recording={"schema": recording.schema, "sha256": recording_digest},
            catalog={"version": catalog_version, "digest": catalog_digest},
            environment=environment,
            steps=tuple(steps), status=status,
        )
    out_of_scope = recording.capture_diagnostics.get("outOfScopeEvents", 0)
    diagnostics = {"outOfScopeEvents": int(out_of_scope or 0)}
    if navigation_merges:
        diagnostics["mergedNavigationKeyEvents"] = [
            record
            for commit_sequence in sorted(navigation_merges)
            for record in navigation_merges[commit_sequence]
        ]
    return CompilationResult(case, golden, tuple(issues), diagnostics)
