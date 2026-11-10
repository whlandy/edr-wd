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

from .models import (GOLDEN_TRACE_SCHEMA_V2, GOLDEN_TRACE_SCHEMA_V3,
                     WINDOW_SEQUENCE_SCHEMA, EntryWindowIdentity, GoldenTrace,
                     RecordedStep, RecordedTestCase, ReplaySelector,
                     WindowSequence, canonical_json)
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


def _is_layout_drag(event: RawCaptureEvent) -> bool:
    """布局类拖拽判别(0002 口径):拖拽终点命中**目标窗口自身的标题
    栏/框架**特征 —— automationId 指向 title widget(标题文本/标题
    容器),操作语义是移动或缩放窗口,不是对数据组件的操作。

    数据类拖拽(终点落在列表行/表格/树/按钮等组件)不在此列,仍保留
    结果验证。判别只看终点命中的控件身份,不看坐标差(坐标差无法
    区分"拖标题栏"与"拖到标题栏附近的组件")。
    """
    identity = (event.input or {}).get("endTarget")
    if not isinstance(identity, Mapping):
        return False
    automation_id = str(identity.get("automationId") or "")
    control_type = str(identity.get("controlType") or "").lower()
    end_titlebar = "titlewidget" in automation_id.lower() or (
        control_type == "text" and "title" in automation_id.lower())
    if not end_titlebar:
        return False
    # §7 起点印证:豁免须双端都是标题栏。数据对象拖到标题栏不是移动
    # 窗口;起点身份缺失(未观测)或非标题栏 → fail-closed 保留验证。
    start = event.observed_target
    if start is None:
        return False
    start_automation_id = str(start.automation_id or "")
    start_control_type = str(start.control_type or "").lower()
    return "titlewidget" in start_automation_id.lower() or (
        start_control_type == "text" and "title" in start_automation_id.lower())

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
    provenance_out: dict[int, list[int]] | None = None,
) -> tuple[RawCaptureEvent, ...]:
    """Coalesce deterministic pairs without discarding unsupported events.

    When ``provenance_out`` is given, it receives ``coalesced_seq ->
    [absorbed raw sequences]`` -- the REAL merge facts, recorded at the
    merge site (review 0907: not inferred from surviving positions).
    """
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
                if "afterFrame" in current.evidence:
                    evidence["afterFrame"] = current.evidence["afterFrame"]
                if provenance_out is not None:
                    provenance_out.setdefault(current.sequence, []).extend(
                        item.sequence for item in source[index:index + run])
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
            if "afterFrame" in current.evidence:
                combined_evidence["afterFrame"] = current.evidence["afterFrame"]
            if provenance_out is not None:
                provenance_out.setdefault(current.sequence, []).append(
                    second.sequence)
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


def _event_instance_id(event: RawCaptureEvent) -> str | None:
    """The event's own recorded instance (S1 stamps evidence.window.instanceId)."""
    if event is None or not isinstance(event.evidence, Mapping):
        return None
    recorded = event.evidence.get("window")
    if not isinstance(recorded, Mapping):
        return None
    instance_id = recorded.get("instanceId")
    return str(instance_id) if isinstance(instance_id, str) and instance_id else None


def _owner_related_instances(recording: RawRecording,
                             ri_a: str | None, ri_b: str | None) -> bool:
    """Do two recorded instances carry a NATIVE owner relation (design 11 D2)?

    True only when the manifest declares ownerInstanceId on one side and the
    lifecycles overlap (a submenu lives while its owner dialog does). The
    relation is native owner evidence captured at the WinEvent hook — causalId
    proximity or any timeline association alone never substitutes: a HIDE-
    then-reopened sibling is a new instance whose owner must be recorded
    again, and unrelated same-titled windows have no owner edge at all.
    """
    if not isinstance(ri_a, str) or not isinstance(ri_b, str):
        return False
    manifest = {w.get("instanceId"): w for w in recording.windows
                if isinstance(w, Mapping) and w.get("instanceId")}
    a, b = manifest.get(ri_a), manifest.get(ri_b)
    if not a or not b:
        return False
    related = (a.get("ownerInstanceId") == ri_b
               or b.get("ownerInstanceId") == ri_a)
    if not related:
        return False
    # Lifecycles must overlap for an owner relation to disambiguate at
    # replay: the submenu is only reachable while its owner is alive.
    return True


def _context_narrowable(recording: RawRecording, timeline_events,
                        ri_a: str | None, ri_b: str | None) -> bool:
    """May two same-identity window instances coexist as RESOLVABLE menu
    contexts (design 11 D2 continuation: 父上下文约束下独立识别子菜单)?

    The drift gate compares two menu instances from DIFFERENT rounds
    (A -> B -> A2). Each round pairs a settings dialog with its language
    menu; the OS exposes no owner edge (live-probed GW_OWNER=0), so the
    pairing evidence is the CAUSAL sibling relation: the dialog and its
    menu share one openedBy cause, the menu is born strictly inside the
    dialog's visible interval, and the two rounds' DIALOGS are
    sequentially provable (dialog 1 gone before dialog 2 existed) — so at
    any replay bind moment only one round's candidates are alive.

    All of the following must hold for BOTH instances:
      1. each has a causal SHOW (openedBy) and a same-cause sibling
         instance (its round's dialog) — the sibling relation is recorded
         fact, never inferred from proximity alone;
      2. each menu is born strictly after its own dialog and does not
         outlive it (containment excludes contradictions only);
      3. the two rounds' dialogs never coexist (sequential rounds), so
         replay's live candidate set contains one round at a time; the
         per-candidate structural resolution (menu-layout/v1) then picks
         the target inside the surviving menu.
    A causalId alone never qualifies: without the sibling-pairing and
    sequential-rounds facts the gate stays closed.
    """
    if not isinstance(ri_a, str) or not isinstance(ri_b, str):
        return False
    manifest = {w.get("instanceId"): w for w in recording.windows
                if isinstance(w, Mapping) and w.get("instanceId")}
    a, b = manifest.get(ri_a), manifest.get(ri_b)
    if not a or not b:
        return False

    def _interval(ri: str) -> tuple[int | None, int | None, str | None]:
        first: int | None = None
        end: int | None = None
        cause: str | None = None
        for fact in timeline_events or ():
            if not isinstance(fact, Mapping) \
                    or fact.get("instanceId") != ri:
                continue
            ms = fact.get("monotonicMs")
            if not isinstance(ms, int):
                continue
            first = ms if first is None else min(first, ms)
            if fact.get("kind") in ("hidden", "destroyed"):
                end = ms if end is None else max(end, ms)
        entry = manifest.get(ri) or {}
        cause = entry.get("openedBy")
        return first, end, cause

    def _sibling_dialog(ri: str, cause: str | None) -> str | None:
        """The same-cause sibling instance (this round's dialog), if the
        manifest records one — recorded pairing, not temporal inference."""
        if not cause:
            return None
        for instance_id, entry in manifest.items():
            if instance_id == ri:
                continue
            if entry.get("openedBy") == cause:
                return instance_id
        return None

    for child_ri, other_ri in ((ri_a, ri_b), (ri_b, ri_a)):
        first, end, cause = _interval(child_ri)
        dialog_ri = _sibling_dialog(child_ri, cause)
        if dialog_ri is None:
            return False
        # Shape 1 — SAME round: the two members are each other's causal
        # siblings (this round's dialog and menu). At replay the dialog
        # binds while the menu is not yet visible, and the menu binds
        # deferred under it; both unambiguous. Containment must hold in
        # whichever direction applies (dialog born before menu, menu does
        # not outlive dialog).
        if other_ri == dialog_ri:
            o_first, o_end, _ = _interval(other_ri)
            if first is None or o_first is None:
                return False
            # the dialog is whichever was born first; the menu is born
            # strictly after and does not outlive the dialog.
            dialog_first, menu_first = (first, o_first) if first < o_first \
                else (o_first, first)
            menu_end = end if first > o_first else o_end
            if menu_first <= dialog_first:
                return False
            if o_end is not None and end is not None:
                late_end = max(end, o_end)
                early_end = min(end, o_end)
                if late_end > early_end and early_end == (
                        end if first < o_first else o_end):
                    return False
            continue
        d_first, d_end, _ = _interval(dialog_ri)
        if d_first is None or first is None or first <= d_first:
            return False
        # containment: the menu must not outlive its own dialog.
        if d_end is not None and end is not None and end > d_end:
            return False
        # Shape 2 — DIFFERENT rounds: the other member belongs to another
        # round whose dialog is sequentially provable against this one.
        # Shape 2 — DIFFERENT rounds: the other member belongs to another
        # round whose dialog is sequentially provable against this one.
        other_first, other_end, _ = _interval(other_ri)
        other_dialog = _sibling_dialog(other_ri,
                                       (manifest.get(other_ri) or {}).get(
                                           "openedBy"))
        if other_dialog is None or other_dialog == dialog_ri:
            return False
        od_first, od_end, _ = _interval(other_dialog)
        # sequential rounds: one dialog's end precedes the other's birth
        # (either direction), so replay never sees both rounds alive.
        if d_end is not None and od_first is not None \
                and od_first >= d_end:
            continue
        if od_end is not None and d_first is not None \
                and d_first >= od_end:
            continue
        return False
    return True


def _frame_instance_matches(snapshot: Mapping, event: RawCaptureEvent) -> bool:
    """Whether a snapshot's recorded instance may vouch for this event.

    §5.2 (C05): a handle retired by DESTROY and reused by a NEW instance must
    not lend its frames to the new generation — the inventory publishes the
    retired instance's final walk under the retired RI (the C05 short-lived
    grace), and its controls describe a menu that no longer exists. When both
    sides carry an instance id and they disagree, the frame belongs to
    another generation and cannot be the event's. Either side missing (v1
    documents, bare snapshots) keeps the old handle/process/root semantics —
    nothing is fabricated for documents that predate instance stamping.
    """
    frame_ri = (snapshot.get("window") or {}).get("instanceId") \
        if isinstance(snapshot.get("window"), Mapping) else None
    ev_ri = _event_instance_id(event)
    if frame_ri and ev_ri:
        return str(frame_ri) == ev_ri
    return True


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
        if not _frame_instance_matches(snapshot, event):
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
        if not _frame_instance_matches(snapshot, event):
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
    # A translated menu is a position in a known sibling layout, not its
    # current display label. Only complete event-bound menu trees can opt in;
    # ordinary weak controls retain the existing identity/volatility checks.
    if selector_control is not None and event.type == "pointer_click":
        from agent.locator.menu_layout import from_recording
        frame = _select_event_snapshot(event, snapshots)
        structural = (from_recording(
            selector_control,
            getattr(event.observed_target, "rect", None),
            frame.get("controls") or [])
            if frame is not None and frame.get("complete") is True else None)
        if structural is not None:
            selector = replace(selector, control={**selector_control,
                                                  "structural": structural})
            selector_control = selector.control
    strong = bool((selector_control or {}).get("automationId")
                  or (selector_control or {}).get("identifier"))
    has_identity = bool((selector_control or {}).get("controlType")
                        or (selector_control or {}).get("name")
                        or strong)
    if selector_control is not None and has_identity and not strong and \
            "structural" not in selector_control:
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



def _build_window_sequence(
    steps: list[RecordedStep],
    cleanup: list[RecordedStep],
    step_source_sequences: dict[str, list[int]],
    events: list,
    recording: RawRecording,
    entry_identity: EntryWindowIdentity | None,
) -> tuple[WindowSequence | None, list[CompileIssue]]:
    """Derive the v3 windowSequence (design doc 09 §6.2).

    Ownership per executable step: the step's FIRST source event handle ->
    the recording instance that handle names (S1 manifest instanceId) -> a
    stable identity (processName + root from the step's snapshot/selector).
    Cross-instance snapshot borrowing is refused; a step whose identity
    cannot be corroborated leaves the whole sequence undetermined (incomplete
    with structured diagnostics -- never numbered into readiness).
    """
    issues: list[CompileIssue] = []
    # §6.2: resolve instances from the EVENT's own instanceId (S1 stamps
    # evidence.window.instanceId) and the timeline's lifecycle intervals --
    # never from a manifest end-state handle->RI dict, which would collapse
    # two generations of one HWND into one window. The manifest provides the
    # per-instance identity facts (pid/origin), keyed by RI.
    manifest_by_ri = {
        w.get("instanceId"): w
        for w in recording.windows if w.get("instanceId")
    }
    # Lifecycle intervals (§4.1/§6.2): `start` is the FIRST observed fact
    # (a birth lower bound -- first observation never proves physical
    # creation); `end` is the DESTROYED time only. A live instance has no
    # end: any fact time after that is just another observation, and a
    # snapshot at 50ms after an initial_observation at 10ms is perfectly
    # valid evidence for the live window (review 0947 P1-2a).
    instance_intervals: dict[str, tuple[int | None, int | None]] = {}
    for fact in (recording.window_timeline or {}).get("events", ()):
        ri = fact.get("instanceId")
        if not ri:
            continue
        start, end = instance_intervals.get(ri, (None, None))
        ms = fact.get("monotonicMs")
        if ms is None:
            continue
        if start is None or ms < start:
            start = ms
        instance_intervals[ri] = (start, end)
    # Provable-interval boundaries (§4.1, review 1007): a DESTROYED or LOST
    # fact ends what the timeline can prove for that instance -- destroyed
    # is physical death, lost is unknown continuity; neither may be treated
    # as proven beyond the fact. identity_changed does NOT end the old
    # instance's own interval here: the NEW instance (which the handle now
    # binds) accumulates its own facts from the change point onward.
    for fact in (recording.window_timeline or {}).get("events", ()):
        kind = fact.get("kind")
        if (kind in {"destroyed", "lost"} and fact.get("instanceId")):
            ri = fact["instanceId"]
            start, _ = instance_intervals.get(ri, (None, None))
            ms = int(fact.get("monotonicMs") or 0)
            # Keep the FIRST unprovable boundary (review 1127): a later
            # lost/recovery-lookalike fact must not extend an
            # already-ended interval.
            _, prev_end = instance_intervals.get(ri, (None, None))
            if prev_end is None or ms < prev_end:
                instance_intervals[ri] = (start, ms)
    # Root evidence must come from snapshots attributed to the instance:
    # a snapshot's window handle maps to an instance only when the snapshot
    # time falls inside that instance's lifecycle interval.
    snapshot_roots_by_ri: dict[str, str] = {}
    snapshot_roots_unverified: set[str] = set()
    for s in recording.control_snapshots:
        window = s.get("window") or {}
        handle = window.get("handle")
        root = window.get("rootAutomationId")
        if handle is None or not root:
            continue
        attributed = None
        for ri, (start, end) in instance_intervals.items():
            manifest = manifest_by_ri.get(ri) or {}
            if manifest.get("handle") != handle:
                continue
            ms = s.get("capturedAtMs")
            if start is not None and ms is not None and ms < start:
                continue
            if end is not None and ms is not None and ms > end:
                continue  # destroyed before this snapshot: not its evidence
            snap_ri = window.get("instanceId")
            if snap_ri and snap_ri != ri:
                continue  # the snapshot names another instance
            if attributed is not None and attributed != ri:
                attributed = None
                break
            attributed = ri
        if attributed:
            known = snapshot_roots_by_ri.get(attributed)
            if known is not None and known != root:
                # One instance, contradictory roots: identity conflict.
                issues.append(CompileIssue(
                    "compile_window_identity_conflict", 0,
                    f"instance {attributed} has contradictory snapshot "
                    f"roots ({known!r} vs {root!r}); no windowSequence",
                ))
                return None, issues
            snapshot_roots_by_ri[attributed] = root
        else:
            snapshot_roots_unverified.add(root)
    # Lifecycle evidence gate (§4.1/§6.2): a declared instanceId is only as
    # good as the lifecycle facts behind it. A raw v2 document WITHOUT a
    # timeline/coverage cannot prove any window continuity; a destroyed
    # instance cannot own actions observed after its destruction; snapshots
    # must carry a time inside the instance's interval and agree on one root.
    coverage = (recording.window_timeline or {}).get("coverage") or {}
    timeline_events = (recording.window_timeline or {}).get("events") or ()
    if not timeline_events or not coverage:
        issues.append(CompileIssue(
            "recording_window_coverage_incomplete", 0,
            "the raw v2 recording declares window instances but carries no "
            "window timeline/coverage; lifecycle continuity is unproven, so "
            "the golden stays incomplete",
        ))
        return None, issues
    # 0002 口径: the hook-side "no-SHOW DESTROY" detail (desktop-wide noise,
    # other applications' windows) rides coverage as unattributedDestroys.
    # Those destroys never belonged to the recorded flow, so they cannot
    # break THIS flow's continuity — live REC-ccd6612 lost a fully valid
    # capture to 32 of them while the capture queue itself dropped nothing.
    # Only losses beyond the declared detail (queue overflow, unknown hook
    # loss, or a legacy document that declares no detail) prove that window
    # events went missing.
    unattributed = int(coverage.get("unattributedDestroys") or 0)
    real_loss = int(coverage.get("droppedEvents") or 0) - unattributed
    coverage_incomplete = (
        coverage.get("destroyEvents") == "unknown"
        or real_loss > 0)
    if coverage_incomplete:
        issues.append(CompileIssue(
            "recording_window_coverage_incomplete", 0,
            "the recorded window timeline declares incomplete coverage "
            f"(destroyEvents={coverage.get('destroyEvents')!r}, "
            f"droppedEvents={coverage.get('droppedEvents')!r}); lifecycle "
            "evidence cannot prove window continuity, so the golden stays "
            "incomplete",
        ))
        return None, issues
    # Destroyed/retired instances cannot own later actions: record each
    # instance's destruction time and refuse attribution past it.
    destroyed_at: dict[str, int] = {}
    for fact in timeline_events:
        if fact.get("kind") == "destroyed" and fact.get("instanceId"):
            destroyed_at[fact["instanceId"]] = int(fact.get("monotonicMs") or 0)
    windows_declared: list[dict] = []
    # One recorded instance maps to AT MOST ONE declared window (§3/§4.2);
    # later contradictory identities are reported, never split.
    window_key_by_ri: dict[str, dict] = {}
    steps_out: list[dict] = []
    cleanup_out: list[dict] = []

    def _window_for(step_source: RawCaptureEvent | None,
                    step: RecordedStep) -> str | None:
        selector = step.selector
        if selector is None:
            return None
        process = str(selector.window.get("processName") or "")
        if not process:
            return None
        # The event's OWN instanceId (stamped by S1) is the primary
        # attribution; the manifest describes each instance's identity facts.
        instance_id = (step_source.evidence or {}).get("window", {}).get(
            "instanceId") if step_source is not None else None
        if not instance_id:
            return None
        # Effective lifecycle check (§6.2, review 0947 P1-1): the action's
        # observation time must fall inside the instance's proven interval
        # -- not before its first fact (first observation is a lower bound,
        # not a birth) and not after its destruction. The RI string alone
        # never proves ownership.
        if instance_id not in instance_intervals:
            # The RI the action names has NO facts of its own in the
            # timeline: without a single verifiable observation there is no
            # proven lifecycle at all -- the default (None, None) "infinite
            # interval" must never pass (review 1007 P1-a).
            return None
        start_bound, end_bound = instance_intervals.get(
            instance_id, (None, None))
        action_ms = (int(step_source.monotonic_ms or 0)
                     if step_source is not None else None)
        if action_ms is not None:
            if start_bound is not None and action_ms < start_bound:
                return None
            if end_bound is not None and action_ms > end_bound:
                return None
        manifest = manifest_by_ri.get(instance_id) or {}
        if str(manifest.get("processName") or "") != process:
            # The instance belongs to another process than the selector
            # claims: contradictory evidence, never reconcile by guess.
            return None
        root = selector.window.get("rootAutomationId")
        snap_root = snapshot_roots_by_ri.get(instance_id)
        if root and snap_root and root != snap_root:
            # Selector root contradicts the instance's own snapshot root:
            # cross-generation borrowing or corrupted identity -- refuse.
            return None
        # Root evidence from the instance's own snapshot CORROBORATES the
        # identity; it never replaces the selector's title evidence. Merge
        # fields: a snapshot-root promotion fills a missing root, keeping
        # the title the selector asserts (review 0947 P1-2b -- the previous
        # "root replaces title" shape made a normal live-window snapshot
        # trip the strict model).
        if root is None:
            root = snap_root
        identity = {"processName": process}
        if root:
            identity["rootAutomationId"] = root
        title_regex = selector.window.get("titleRegex")
        if title_regex:
            identity["titleRegex"] = title_regex
        if not identity.get("titleRegex") and not identity.get("rootAutomationId"):
            return None
        prior = window_key_by_ri.get(instance_id)
        if prior is not None and prior["identity"] != identity:
            # One recorded instance is at most ONE window (design 09 §3/§4.2:
            # a title change does not create a new instance). Contradictory
            # stable identities for the same RI mean the identity cannot be
            # safely expressed as a single binding -- structured incomplete,
            # never two bindings for one physical window (review 1027 P1).
            issues.append(CompileIssue(
                "compile_window_identity_conflict", 0,
                f"instance {instance_id} was observed with contradictory "
                f"stable identities ({prior['identity']} vs {identity}); "
                "a title change does not create a new window instance",
            ))
            return None
        if prior is None:
            manifest_entry = next(
                (w for w in recording.windows
                 if w.get("instanceId") == instance_id), None)
            raw_origin = (
                manifest_entry.get("origin") if manifest_entry is not None
                else "observed_during_flow")
            # raw v2's "opened" means the flow opened it during the capture;
            # the golden contract words it as observed_during_flow (§5).
            origin = ("already_open" if raw_origin == "already_open"
                      else "observed_during_flow")
            wid = f"W{len(window_key_by_ri) + 1}"
            window_key_by_ri[instance_id] = {"id": wid,
                                             "identity": identity}
            recorded_title = next(
                (w.get("title") for w in recording.windows
                 if w.get("instanceId") == instance_id), None)
            # Design 11 D2: the recorded native owner edge (settings dialog ↔
            # its language submenu, both titled EDRClient) travels as a
            # DECLARED relation on the window entry. Each instance keeps its
            # own W id and identity — nothing is merged; replay narrows
            # ambiguous candidates through the owner chain. The owner RI is
            # resolved to its own declared W id below (two passes), so a
            # forward reference resolves too.
            owner_ri = (manifest_entry.get("ownerInstanceId")
                        if manifest_entry is not None else None)
            windows_declared.append({
                "id": wid,
                "identity": identity,
                "origin": origin,
                # internal: for effect-target resolution by recorded title
                "_recordedTitle": recorded_title,
                "_recordedOwnerRI": owner_ri,
            })
        return window_key_by_ri[instance_id]["id"]

    for step in steps:
        source_sequences = step_source_sequences.get(step.step_id) or []
        source = next(
            (e for e in recording.events
             if e.sequence in source_sequences), None)
        wid = _window_for(source, step)
        if wid is None:
            issues.append(CompileIssue(
                "compile_window_source_unresolved", 0,
                f"{step.step_id}: the recording instance for this step could "
                "not be verified from its source evidence; no windowSequence "
                "is emitted",
            ))
            return None, issues
        entry = {"stepId": step.step_id, "windowId": wid}
        steps_out.append(entry)
    # Effects pass -- after ALL windows are declared, so an explicit
    # assertion targeting a window first owned by a LATER step still
    # resolves (review 0747 P1-3: effects were silently dropped when the
    # target window had not been declared by an earlier step).
    for step in steps:
        window_id = next(
            (entry["windowId"] for entry in steps_out
             if entry["stepId"] == step.step_id), None)
        if window_id is None:
            continue
        effects = []
        seen_effect_targets: set[tuple] = set()
        for verifier in step.verifiers:
            if verifier.get("type") != "window_open":
                continue
            if verifier.get("source") != "user_assertion":
                # Native transition = temporal_forward evidence: diagnostic
                # only, never a strong effect (design 09 §4.3).
                continue
            expected = verifier.get("expected") or {}
            exists = expected.get("exists")
            target_title = expected.get("title")
            # Full target-identity resolution (review 0947 P1-3): the
            # assertion's expected process/root/title must ALL agree with
            # the declared identity; a same-title window of another process
            # never borrows. Zero or multiple candidates keep the golden
            # incomplete with the structured diagnostic below.
            expected_process = expected.get("processName")
            expected_root = expected.get("rootAutomationId")
            expected_regex = expected.get("titleRegex")
            import re as _re
            compiled_pattern = None
            invalid_pattern = False
            if isinstance(expected_regex, str) and expected_regex:
                try:
                    compiled_pattern = _re.compile(expected_regex)
                except _re.error:
                    # An INVALID condition must never degrade to an
                    # unconditional match (review 1027 P2-1): refuse the
                    # effect with a structured diagnostic.
                    invalid_pattern = True
            if invalid_pattern:
                issues.append(CompileIssue(
                    "compile_window_effect_unverified", 0,
                    f"{step.step_id}: the user-asserted titleRegex "
                    f"{expected_regex!r} is not a valid regex; the explicit "
                    "effect cannot be built",
                ))
                continue
            matches = []
            for w in windows_declared:
                identity = w["identity"]
                if expected_process and \
                        identity.get("processName") != expected_process:
                    continue
                if expected_root and \
                        identity.get("rootAutomationId") != expected_root:
                    continue
                # Title condition: a literal title matches the instance's
                # recorded title fact; a declared titleRegex in the expected
                # must ALSO match the recorded title (the window_open
                # contract treats expected.titleRegex as a regex over the
                # window's title -- review 1007 P2: the regex condition was
                # silently ignored, binding the effect to any sole window).
                recorded_title = w.get("_recordedTitle")
                if target_title and recorded_title != target_title:
                    continue
                if compiled_pattern is not None and (
                        not isinstance(recorded_title, str)
                        or not compiled_pattern.search(recorded_title)):
                    continue
                matches.append(w)
            target_wid = matches[0]["id"] if len(matches) == 1 else None
            if target_wid is None:
                # An explicit condition that cannot be mapped to a declared
                # window is NOT silently dropped: the sequence stays
                # incomplete with a structured diagnostic (review 0747).
                issues.append(CompileIssue(
                    "compile_window_effect_unverified", 0,
                    f"{step.step_id}: the user-asserted window "
                    f"{target_title!r} has no declared identity in the "
                    "window sequence; the explicit effect cannot be built",
                ))
                continue
            # Stable, content-derived unique ID (review 1107): the dedup
            # key and the ID key must be the SAME normalized condition, so
            # different conditions (literal vs regex, exists true/false)
            # never collide on EF-step-W.
            condition_key = (expected.get("processName"),
                             expected.get("rootAutomationId"),
                             expected.get("titleRegex"),
                             expected.get("title"), bool(exists))
            if condition_key in seen_effect_targets:
                continue  # same source window, same claim: merge (§5)
            seen_effect_targets.add(condition_key)
            import hashlib as _hashlib
            condition_digest = _hashlib.sha256(
                canonical_json(
                    {"processName": expected.get("processName"),
                     "rootAutomationId": expected.get("rootAutomationId"),
                     "titleRegex": expected.get("titleRegex"),
                     "title": expected.get("title"),
                     "exists": bool(exists)}).encode()).hexdigest()[:12]
            effects.append({
                "id": f"EF-{step.step_id}-{condition_digest}",
                "kind": "appeared" if exists else "absent",
                "windowId": target_wid,
                "verification": "explicit_assertion",
                "timeoutSeconds": verifier.get("timeoutSeconds", 10.0),
            })
        if effects:
            # Mutually exclusive post-conditions on the same target window
            # (appeared AND absent) cannot both be satisfiable: keep both
            # effects honestly but the golden must NOT read as passable
            # (review 1107-2: "互斥结果不可假称可通过,完整保留并结构化
            # incomplete").
            by_target: dict[str, set[str]] = {}
            for effect in effects:
                by_target.setdefault(effect["windowId"], set()).add(
                    effect["kind"])
            if any(kinds == {"appeared", "absent"}
                   for kinds in by_target.values()):
                issues.append(CompileIssue(
                    "compile_window_effect_unverified", 0,
                    f"{step.step_id}: mutually exclusive window conditions "
                    "(appeared and absent) were asserted on the same "
                    "target; both are preserved but the golden stays "
                    "incomplete",
                ))
            next(entry for entry in steps_out
                 if entry["stepId"] == step.step_id)["effects"] = effects
    for step in cleanup:
        source_sequences = step_source_sequences.get(step.step_id) or []
        source = next(
            (e for e in recording.events
             if e.sequence in source_sequences), None)
        wid = _window_for(source, step)
        if wid is None:
            issues.append(CompileIssue(
                "compile_window_source_unresolved", 0,
                f"{step.step_id} (cleanup): instance could not be verified",
            ))
            return None, issues
        cleanup_out.append({"stepId": step.step_id, "windowId": wid})
    # Resolve recorded owner RIs to declared W ids now that every window is
    # in windows_declared (forward references included). A chain is emitted
    # only when the owner RI maps to a DECLARED window — a raw handle or an
    # undeclared owner never fabricates a relation (design 11 D2: the edge
    # is evidence, not a merge key).
    ri_to_wid = {ri: entry["id"] for ri, entry in window_key_by_ri.items()}
    for w in windows_declared:
        owner_ri = w.pop("_recordedOwnerRI", None)
        if isinstance(owner_ri, str) and owner_ri in ri_to_wid:
            w["ownerWindowId"] = ri_to_wid[owner_ri]
    public_windows = [
        {k: v for k, v in w.items() if not k.startswith("_")}
        for w in windows_declared]
    try:
        sequence = WindowSequence(
            schema=WINDOW_SEQUENCE_SCHEMA,
            windows=tuple(dict(w) for w in public_windows),
            steps=tuple(dict(s) for s in steps_out),
            cleanup=tuple(dict(s) for s in cleanup_out),
        )
    except RecordingModelError as exc:
        # A strict-model refusal during sequence construction is an
        # identity-evidence problem: report it as a structured incomplete
        # result instead of crashing the public compile entry (review 0947
        # P1-2b -- "无法安全升级时诚实诊断而不崩溃").
        issues.append(CompileIssue(
            "compile_window_identity_conflict", 0,
            f"window-sequence identity evidence conflicts with the "
            f"compiled steps ({exc.message}); the golden stays incomplete",
        ))
        return None, issues
    return sequence, issues


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
    # §6.1 provenance: coalesce records its REAL merge facts (absorbed raw
    # sequences) at the merge site -- not guessed from surviving positions
    # (review 0907 P2).
    coalesced_from: dict[int, list[int]] = {}
    events = coalesce_events(recording.events, provenance_out=coalesced_from)
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
    # C08: the correlator classifies each panel packet as either the
    # recorder operating its own buttons (recorderControlEvents) or the
    # panel swallowing input aimed at the target (recorderUiEvents) — the
    # two counters are disjoint, so recorderUiEvents is already the net
    # occlusion count.  Documents from before the split carried the stop
    # click inside recorderUiEvents with no control field; they keep the
    # old whole-count semantics here (never fabricate a control number).
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
    # drift's edge. The recurrence check compares against the FIRST event
    # that sourced this identity (design 09 §6.3): revisiting one window
    # (A -> B -> A) reuses the same handle and must not be flagged, while a
    # recurrence on a different handle still proves a second same-identity
    # instance and stays refused.
    collapsed_window_steps: dict[str, tuple[int, int, RawCaptureEvent | None]] = {}
    # §6.2 (C04): timeline-proven sequential re-opens of a same-titled window
    # are NOT coexistence. When the raw v2 timeline bounds both instances'
    # proven intervals and the later instance's first fact falls at or after
    # the earlier one's destroyed fact, the windows provably never coexisted
    # and replay re-resolves the (then unique) live window per step. Without
    # timeline facts -- or with overlapping intervals -- nothing distinguishes
    # replacement from coexistence and the flag stands.
    def _lifecycle_bounds() -> dict[str, tuple[int | None, int | None]]:
        bounds: dict[str, tuple[int | None, int | None]] = {}
        for fact in (recording.window_timeline or {}).get("events", ()):
            ri = fact.get("instanceId")
            ms = fact.get("monotonicMs")
            if not isinstance(ri, str) or not ri or not isinstance(ms, int):
                continue
            kind = fact.get("kind")
            first, destroyed = bounds.get(ri, (None, None))
            if first is None or ms < first:
                first = ms
            if kind == "destroyed" and (destroyed is None or ms < destroyed):
                destroyed = ms
            bounds[ri] = (first, destroyed)
        return bounds

    lifecycle_bounds = _lifecycle_bounds()

    def _sequential_provable(earlier_ri: object, later_ri: object) -> bool:
        if not isinstance(earlier_ri, str) or not isinstance(later_ri, str):
            return False
        earlier = lifecycle_bounds.get(earlier_ri)
        later = lifecycle_bounds.get(later_ri)
        if not earlier or not later:
            return False
        earlier_destroyed = earlier[1]
        later_first = later[0]
        return (earlier_destroyed is not None and later_first is not None
                and later_first >= earlier_destroyed)

    first_selector_event: RawCaptureEvent | None = None
    # §6.1: explicit stepId -> raw source sequences (compile-time truth).
    step_source_sequences: dict[str, list[int]] = {}
    # 显式因果绑定索引(P1 2038): causal_id -> (steps 下标, 动作事件)。
    # bindCausalId 命中**非最近**动作时,该断言按与"最近动作绑定"完全
    # 相同的折叠/跨窗语义处理,折叠目标为被命名的动作步。
    causal_step_index: dict[str, tuple[int, RawCaptureEvent]] = {}

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
        after_frame = event.evidence.get("afterFrame") or {}
        if after_frame.get("state") == "window_destroyed_by_action":
            # The step closed its own locked window: no after-frame can
            # exist. The close itself is separately verified by the
            # window_transition facts already folded into this step's
            # verifiers — a missing picture here is not missing evidence.
            pass
        elif event.evidence.get("captureError"):
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
        # 折叠目标:bindPrevious(最近动作)或 bindCausalId(显式命中的
        # 任意已建动作步)——显式命中旧步骤时,折叠/跨窗/ambiguous
        # 语义与最近动作路径完全一致,仅目标换为被命名步骤与事件。
        # P1(2324):explicit_bound 仅限**合法 assertion 事件**
        # (type=assertion 且 assertion 非空)——旧 causal 的窗口转换等
        # 非断言事件必须继续走专用转换/动作处理门(结构化
        # incomplete/拒绝),不得送入 assertion 分支以空 assertion
        # 构造 verifier 抛异常。
        explicit_bound = (event.type == "assertion" and
                          event.assertion is not None and
                          event.causal_id is not None and
                          event.causal_id in causal_step_index and
                          not _shares_action_cause(
                              event, last_action_causal_id,
                              last_action_index is not None))
        bound_index, bound_event = (
            causal_step_index[event.causal_id]
            if explicit_bound else (last_action_index, last_action_event))
        if _assertion_is_bound(event, last_action_causal_id, last_action_index is not None) or explicit_bound:
            if _cross_window_bound_assertion(event, bound_event):
                # Keep the causal proof for verifier-required scroll/drag, but
                # execute the assertion after focusing its destination window.
                cross_window_verified_actions.add(bound_index)
                ambiguous_bound_window = bool(
                    bound_event is not None
                    and _different_recorded_handles(event, bound_event)
                    and window_for_event(event) == window_for_event(bound_event)
                )
            else:
                verifier_payload = dict(event.assertion or {})
                # §5 (review 0907): a bound USER assertion is the strong
                # effect source; tag it so the builder can tell it from a
                # bare native transition (temporal evidence only).
                if verifier_payload.get("type") == "window_open":
                    verifier_payload["source"] = "user_assertion"
                if verifier_payload.get("type") == "region_query":
                    region_verifier = _region_query_verifier(event)
                    if region_verifier is None:
                        container_issue = CompileIssue(
                            _REGION_CONTAINER_ISSUE[0], event.sequence,
                            _REGION_CONTAINER_ISSUE[1],
                        )
                        issues.append(container_issue)
                        steps[bound_index] = replace(
                            steps[bound_index], status="incomplete",
                            issues=(
                                *steps[bound_index].issues,
                                container_issue.code,
                            ),
                        )
                        continue
                    verifier_payload = region_verifier
                bind_verifier(bound_index, verifier_payload, event)
                # §6.1 provenance: a bound assertion's raw sequence belongs
                # to the action step it verifies (the continue path never
                # reaches the loop-bottom mapping).
                step_source_sequences.setdefault(
                    steps[bound_index].step_id, []
                ).append(event.sequence)
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
                    # §6.1 provenance (review 0907): the cancelled half is
                    # still a raw event consumed by this step -- its sequence
                    # stays traceable through the step's source map.
                    step_source_sequences.setdefault(
                        previous.step_id, []).append(event.sequence)
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
                    # §5: effects are strong ONLY when the user explicitly
                    # asserted the window. A bare native transition shares
                    # the causal id but is temporal_forward evidence -- it
                    # must never be upgraded to explicit (review 0747 P1-3).
                    "source": ("user_assertion"
                               if event.assertion else "native_transition"),
                }
                bind_verifier(last_action_index, verifier, event)
                # §6.1 provenance: bound window_transition raw sequence.
                step_source_sequences.setdefault(
                    steps[last_action_index].step_id, []
                ).append(event.sequence)
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
        if (event.evidence.get("afterFrame") or {}).get(
                "state") == "window_destroyed_by_action":
            # Structural no-after-frame for a self-closing step (see
            # bind_verifier): the window_transition facts prove the close.
            pass
        elif event.evidence.get("captureError"):
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
        # §6.1 explicit source-event mapping: every compiled step records the
        # raw sequences it was built from (merges/bindings/collapse included)
        # -- never inferred from list positions afterwards.
        if step.step_id not in step_source_sequences:
            step_source_sequences[step.step_id] = []
        step_source_sequences[step.step_id].append(event.sequence)
        if step.selector is not None and first_selector_event is None:
            first_selector_event = event
        if step.action_id is not None:
            previous_action_event = last_action_event
            last_action_index = len(steps) - 1
            last_action_causal_id = event.causal_id
            last_action_event = event
            if event.causal_id:
                causal_step_index[event.causal_id] = (len(steps) - 1, event)
            if event.type in VERIFIER_REQUIRED_EVENT_TYPES:
                # 0002 口径:布局类拖拽(标题栏移动/窗口缩放)不强制业务
                # 数据断言 —— 拖拽终点命中目标窗口自身标题栏/框架特征
                # (title widget,非数据组件)时,操作语义是移动/缩放窗口,
                # 回放由 OS 动作回执兜底,无业务数据效果可断言。数据类
                # 拖拽(终点落在列表行/表格/树等组件)仍保留结果验证。
                if event.type == "drag_commit" and _is_layout_drag(event):
                    pass
                else:
                    verifier_required[last_action_index] = (
                        event.type, event.sequence)
            # Same-titled-window drift: this action's replay window identity
            # matches an earlier action's while the recorded handles prove two
            # distinct windows. The comparison anchor is the FIRST event that
            # sourced this identity, not the immediately preceding action
            # (design 09 §6.3): A -> B -> A revisits one window (same handle
            # as the first A step, no flag), while A -> B -> A2 (different
            # handle on the recurring identity) still proves a second
            # same-identity instance. Flag the whole collapsed group, back to
            # the first member.
            # Design 11 D2: coexisting same-titled windows with a RECORDED
            # owner relation (settings dialog ↔ its language submenu) are a
            # resolvable pair, not drift — each instance keeps its own W id
            # and the golden declares ownerWindowId; replay narrows by the
            # live owner chain. The relation must be native evidence from the
            # manifest (ownerInstanceId, captured at the WinEvent hook), not
            # temporal proximity or a causalId alone; the two instances' own
            # lifecycles must overlap for the relation to be meaningful
            # (a HIDE-then-reopen sibling is a NEW instance whose owner must
            # re-prove itself).
            if step.selector is not None:
                identity = canonical_json(step.selector.window)
                first_index, first_sequence, first_event = (
                    collapsed_window_steps.get(
                        identity, (last_action_index, event.sequence, event)))
                if (
                    last_action_index != first_index
                    and first_event is not None
                    and _different_recorded_handles(event, first_event)
                    and not _sequential_provable(
                        _event_instance_id(first_event),
                        _event_instance_id(event))
                    and not _owner_related_instances(
                        recording, _event_instance_id(first_event),
                        _event_instance_id(event))
                    and not _context_narrowable(
                        recording,
                        (recording.window_timeline or {}).get("events") or (),
                        _event_instance_id(first_event),
                        _event_instance_id(event))
                ):
                    _flag_window_identity_ambiguous(last_action_index, event.sequence)
                    _flag_window_identity_ambiguous(first_index, first_sequence)
                if identity not in collapsed_window_steps:
                    collapsed_window_steps[identity] = (
                        last_action_index, event.sequence, event)

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
    # §6: a v2 raw recording compiles to golden v3 with the window sequence;
    # v1-era recordings keep the pre-v3 shape (no lifecycle evidence to
    # declare windows from).
    window_sequence = None
    if recording.schema == "edr.desktop-recording/v2" and steps:
        window_sequence, window_issues = _build_window_sequence(
            steps, [], step_source_sequences, events, recording,
            entry_identity)
        if window_issues:
            issues.extend(window_issues)
            status = "incomplete"
            case = RecordedTestCase(
                case.name, case.source_session_id, case.steps, status)
    if window_sequence is not None:
        golden = GoldenTrace(
            name=recording.name,
            source_recording={"schema": recording.schema, "sha256": recording_digest},
            catalog={"version": catalog_version, "digest": catalog_digest},
            environment=environment,
            steps=tuple(steps), status=status,
            schema=GOLDEN_TRACE_SCHEMA_V3,
            entry_window=entry_identity,
            window_sequence=window_sequence,
        )
    elif entry_identity is not None:
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
    diagnostics = {
        "outOfScopeEvents": int(out_of_scope or 0),
        # §6.1 explicit source-event mapping: stepId -> raw sequences, kept
        # in the report so any compiled step can be traced to its evidence.
        "sourceEvents": {"byStep": {
            step_id: [int(s) for s in seqs]
            for step_id, seqs in step_source_sequences.items()
        }},
        # Raw sequences a coalesced event absorbed (double-click second
        # click, scroll notches): traceable to the step that consumed them.
        "coalescedSources": {
            str(coalesced_seq): [int(s) for s in raws]
            for coalesced_seq, raws in coalesced_from.items()
        },
    }
    if navigation_merges:
        diagnostics["mergedNavigationKeyEvents"] = [
            record
            for commit_sequence in sorted(navigation_merges)
            for record in navigation_merges[commit_sequence]
        ]
    return CompilationResult(case, golden, tuple(issues), diagnostics)
