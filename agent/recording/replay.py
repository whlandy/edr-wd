"""Golden-trace loading, fresh selector materialization, and atomic replay."""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Mapping

from agent.execution import AtomicExecutor, CaseRunResult, StepMaterializationError, StepStatus
from agent.locator import HybridLocator, LocatorError, LocatorRequest, LocatorPolicy
from agent.execution.confirmation import ExecutionContext
from agent.trace.events import EventType
from agent.trace.evidence import EvidenceRecord, persist_screenshot
from agent.trace.store import TraceStore
from target.protocol_models import AtomicTestStep, Expectation, TargetRef, TestCase
from target.recording.models import RecordingModelError

from .models import GoldenTrace, RecordedStep, ReplayEvaluation, ReplaySelector, canonical_json
from .selectors import expectation_match
from .visual import SafeVisualResolver


def load_golden_trace(path: str | Path) -> GoldenTrace:
    return GoldenTrace.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


def _get(value: Any, *names: str) -> Any:
    for name in names:
        if isinstance(value, Mapping) and name in value:
            return value[name]
        if hasattr(value, name):
            return getattr(value, name)
    return None


def _snapshot_id(observation: Any) -> str:
    value = _get(observation, "snapshot_id", "snapshotId", "observation_id")
    if not isinstance(value, str) or not value:
        raise StepMaterializationError(
            "replay_observation_invalid",
            "fresh observation has no snapshot id",
        )
    return value


def _targets(observation: Any) -> list[Any]:
    values = _get(observation, "targets")
    if values is None:
        values = _get(observation, "controls")
    return list(values or [])


def _matches_process(actual: Any, expected: str) -> bool:
    return str(actual or "").lower().removesuffix(".exe") == expected.lower().removesuffix(".exe")


def _control_match(candidate: Any, control: Mapping[str, Any]) -> bool:
    comparisons = {
        "automationId": ("automation_id", "automationId"),
        "identifier": ("identifier",),
        "controlType": ("control_type", "controlType", "role"),
        "name": ("text", "name", "title"),
    }
    for selector_field, candidate_fields in comparisons.items():
        expected = control.get(selector_field)
        if expected not in (None, "") and _get(candidate, *candidate_fields) != expected:
            return False
    expected_ancestry = control.get("ancestry") or []
    if expected_ancestry:
        actual_ancestry = list(_get(candidate, "ancestry") or [])
        # The recorder walks the UIA tree; the replay observation reports
        # whatever depth its own dump provides, often none. Demanding equality
        # makes a selector that can never match, so the recorded chain need
        # only be contained in the observed one, nearest ancestor first.
        if not _ancestry_contains(actual_ancestry, list(expected_ancestry)):
            return False
    return True


def _ancestry_contains(actual: list, expected: list) -> bool:
    if not expected:
        return True
    if len(actual) < len(expected):
        return False
    return all(
        _ancestor_matches(actual[index], expected[index])
        for index in range(len(expected))
    )


def _ancestor_matches(actual: Any, expected: Any) -> bool:
    if not isinstance(expected, Mapping):
        return actual == expected
    if not isinstance(actual, Mapping):
        return False
    return all(
        str(actual.get(key) or actual.get(_snake(key)) or "") == str(value)
        for key, value in expected.items()
        if value not in (None, "")
    )


def _snake(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def resolve_replay_selector(selector: ReplaySelector, observation: Any) -> TargetRef:
    """Resolve exactly one current target; never reuse a recorded target ID."""
    snapshot_id = _snapshot_id(observation)
    items = [item if isinstance(item, Mapping) else vars(item)
             for item in _targets(observation)]
    bundle = {
        "snapshot_id": snapshot_id, "observation_id": snapshot_id,
        "controls": items,
        "active_window": _get(observation, "active_window") or {},
        "window_identity": _get(observation, "window_identity") or {},
    }
    try:
        resolution = HybridLocator().resolve(
            LocatorRequest("gui.click", selector.window, selector.control),
            bundle, LocatorPolicy(mode="semantic_only"),
        )
    except LocatorError as exc:
        code = "replay_target_ambiguous" if exc.code == "locator_ambiguous" else exc.code
        raise StepMaterializationError(
            code, "semantic replay selector could not resolve one current target",
            details={"candidate_count": exc.candidate_count, "strategy": "semantic"},
        ) from exc
    candidate = resolution.candidate
    if candidate is None:
        raise StepMaterializationError("replay_target_not_found", "no semantic target")
    return TargetRef(
        snapshot_id=snapshot_id,
        target_id=candidate.target_id,
        expected_process_name=str(selector.window.get("processName") or ""),
        fingerprint=_get(candidate.source, "fingerprint"),
        selector_hint=dict(selector.control),
    )


class ReplayEvidenceRecorder:
    """Persist source-redacted replay frames into the execution trace.

    A frame reaches disk only when the target marked it redacted and
    window-scoped; anything else is refused rather than written, so an
    execution report can never carry an unredacted runtime screenshot.
    """

    def __init__(self, trace_store: TraceStore, *, role: str = "before") -> None:
        self._trace_store = trace_store
        self._role = role
        self.persisted: list[EvidenceRecord] = []

    @property
    def root(self) -> Path:
        return self._trace_store.root

    @property
    def evidence_index(self) -> dict[str, EvidenceRecord]:
        return {record.evidence_id: record for record in self.persisted}

    def record(self, step: AtomicTestStep, observation: Any) -> EvidenceRecord | None:
        png_bytes = _get(observation, "screenshot_bytes")
        if not png_bytes:
            return None
        if _get(observation, "screenshot_scope") != "window":
            raise StepMaterializationError(
                "replay_capture_not_window_scoped",
                "a replay frame may only be persisted when it is window-scoped",
            )
        if _get(observation, "screenshot_redacted") is not True:
            raise StepMaterializationError(
                "replay_capture_not_redacted",
                "a replay frame may only be persisted after target-side source redaction",
            )
        window = _get(observation, "active_window") or {}
        event = self._trace_store.append_dict(
            EventType.SCREENSHOT_CAPTURED,
            payload={
                "snapshot_id": _snapshot_id(observation),
                "scope": "window",
                "redacted": True,
                "sha256": _get(observation, "screenshot_sha256"),
            },
            step_id=step.step_id,
        )
        record = persist_screenshot(
            bytes(png_bytes),
            self._role,
            step_no=step.step_no,
            step_id=step.step_id,
            snapshot_id=_snapshot_id(observation),
            event_id=event.event_id,
            process_name=str(_get(window, "process_name", "processName") or ""),
            pid=int(_get(window, "pid") or 0),
            window_title=str(_get(window, "title", "window_title") or ""),
            trace_dir=self._trace_store.root,
        )
        self._trace_store.append_dict(
            EventType.SCREENSHOT_PERSISTED,
            payload=record.to_dict(),
            step_id=step.step_id,
        )
        self.persisted.append(record)
        return record


def _rect(candidate: Any) -> tuple[int, int, int, int] | None:
    rect = _get(candidate, "rect", "rectangle")
    if not isinstance(rect, (list, tuple)) or len(rect) != 4:
        return None
    return tuple(int(value) for value in rect)


def _anchor_point(candidate: Any, control: Mapping[str, Any]) -> tuple[int, int]:
    """Project a recorded anchor onto the control's current rectangle."""
    rect = _rect(candidate)
    if rect is None:
        raise StepMaterializationError(
            "replay_observation_invalid",
            "resolved current target has no rectangle to anchor a pointer path in",
        )
    left, top, right, bottom = rect
    if right <= left or bottom <= top:
        raise StepMaterializationError(
            "replay_observation_invalid",
            "resolved current target has an empty or inverted rectangle",
        )
    relative = (control.get("anchor") or {}).get("relativePoint", [0.5, 0.5])
    if (
        not isinstance(relative, (list, tuple)) or len(relative) != 2
        or not all(
            isinstance(value, (int, float)) and not isinstance(value, bool)
            and 0 <= value <= 1 for value in relative
        )
    ):
        raise StepMaterializationError(
            "replay_anchor_invalid",
            "pointer anchor must contain two relative values in [0, 1]",
            details={"relativePoint": relative},
        )
    return (
        min(right - 1, max(left, left + round((right - left) * float(relative[0])))),
        min(bottom - 1, max(top, top + round((bottom - top) * float(relative[1])))),
    )


def _expectation_match(selector: ReplaySelector | None) -> dict[str, Any]:
    if selector is None:
        return {}
    return expectation_match(selector.control)


def _verifier_expectation(step: RecordedStep, verifier: Mapping[str, Any]) -> Expectation:
    kind = verifier.get("type") or verifier.get("assertion")
    if "expected" not in verifier:
        raise RecordingModelError(
            "compile_assertion_missing_expected",
            "golden verifier has no explicit expected value",
            path=f"steps.{step.step_id}.verifiers",
        )
    expected = verifier["expected"]
    match = verifier.get("match", _expectation_match(step.selector))
    control_assertions = {
        "visible", "text_contains_time", "text_time_range",
        "text_equals", "text_contains", "value_equals", "checked", "enabled",
    }
    if kind in control_assertions and not match:
        raise RecordingModelError(
            "golden_verifier_selector_missing",
            "control assertion has no stable control identity",
            path=f"steps.{step.step_id}.verifiers.match",
        )
    timeout = float(verifier.get("timeoutSeconds", 10.0))
    if kind == "visible":
        return Expectation(
            type="control_exists" if expected else "control_absent",
            value=match,
            timeout_seconds=timeout,
        )
    if kind in {"window_text_contains", "window_text_contains_time", "window_text_time_range"}:
        # No control identity: the pattern or literal need only appear
        # somewhere in the window.
        return Expectation(
            type=(
                "window_text_time_range" if kind == "window_text_time_range" else
                "window_text_contains_time" if kind == "window_text_contains_time" else
                "window_text_contains"
            ),
            value=({"prefix": expected, "durationSeconds": verifier.get("durationSeconds")}
                   if kind == "window_text_time_range" else expected),
            timeout_seconds=timeout,
        )
    if kind == "text_contains_time":
        # The golden trace stores the pattern, never the timestamp the recorder
        # observed; the evaluator renders it against the replay's own clock.
        return Expectation(
            type="control_text_contains_time",
            value={"match": match, "pattern": expected},
            timeout_seconds=timeout,
        )
    if kind == "text_time_range":
        return Expectation(
            type="control_text_time_range",
            value={"match": match, "prefix": expected,
                   "durationSeconds": verifier.get("durationSeconds")},
            timeout_seconds=timeout,
        )
    if kind in {"text_equals", "text_contains"}:
        return Expectation(
            type="control_text_equals" if kind == "text_equals" else "control_text_contains",
            value={"match": match, "text": expected},
            timeout_seconds=timeout,
        )
    if kind in {"value_equals", "checked", "enabled"}:
        expectation_type = {
            "value_equals": "control_value_equals",
            "checked": "control_checked_equals",
            "enabled": "control_enabled_equals",
        }[kind]
        return Expectation(
            type=expectation_type,
            value={"match": match, "expected": expected},
            timeout_seconds=timeout,
        )
    if kind == "window_open":
        exists = expected
        window_match = {
            "process_name": (step.selector.window.get("processName") if step.selector else None),
            "title_regex": (step.selector.window.get("titleRegex") if step.selector else None),
        }
        if isinstance(expected, Mapping):
            exists = expected.get("exists", True)
            overrides = {
                "process_name": expected.get("process_name") or expected.get("processName"),
                "title": expected.get("title"),
                "title_regex": expected.get("titleRegex"),
            }
            window_match.update({key: value for key, value in overrides.items() if value is not None})
        return Expectation(
            type="window_open" if exists else "window_closed",
            value={key: value for key, value in window_match.items() if value},
            timeout_seconds=timeout,
        )
    raise RecordingModelError(
        "golden_verifier_unsupported",
        f"unsupported verifier {kind!r}",
        path=f"steps.{step.step_id}.verifiers",
    )


def golden_to_test_case(golden: GoldenTrace) -> TestCase:
    steps = [_recorded_to_atomic(recorded, step_no) for step_no, recorded in enumerate(golden.steps, start=1)]
    safe_id = re.sub(r"[^A-Za-z0-9_.-]+", "-", golden.name).strip("-") or "recorded"
    return TestCase(case_id=f"GOLDEN-{safe_id}", title=golden.name, steps=tuple(steps))


def _recorded_to_atomic(recorded: RecordedStep, step_no: int) -> AtomicTestStep:
    expectations = []
    if recorded.action_id is not None:
        expectations.append(Expectation(type="action_ok"))
    expectations.extend(_verifier_expectation(recorded, item) for item in recorded.verifiers)
    return AtomicTestStep(
        step_id=recorded.step_id,
        step_no=step_no,
        title=recorded.step_id,
        action_id=recorded.action_id or "observe.assert",
        args=dict(recorded.args),
        expectations=tuple(expectations),
        required=recorded.required,
        on_error="abort",
    )


class GoldenStepMaterializer:
    def __init__(
        self,
        golden: GoldenTrace,
        *,
        environment: Mapping[str, str] | None = None,
        replay_mode: str = "semantic_only",
        visual_resolver: SafeVisualResolver | None = None,
        trace_store: TraceStore | None = None,
        trace_case_id: str | None = None,
        evidence_recorder: "ReplayEvidenceRecorder | None" = None,
        window_focus: "Callable[[str, str, str], Any] | None" = None,
    ) -> None:
        if replay_mode not in {"semantic_only", "semantic_first", "visual_only"}:
            raise RecordingModelError(
                "replay_mode_invalid", f"unsupported replay mode {replay_mode!r}", path="replay.mode"
            )
        self._selectors = {
            step.step_id: step.selector for step in (*golden.steps, *golden.cleanup)
        }
        self._end_selectors = {
            step.step_id: step.end_selector for step in (*golden.steps, *golden.cleanup)
        }
        self._next_selectors: dict[str, ReplaySelector | None] = {}
        for group in (golden.steps, golden.cleanup):
            for index, recorded in enumerate(group):
                self._next_selectors[recorded.step_id] = (
                    group[index + 1].selector if index + 1 < len(group) else None
                )
        self._environment = environment if environment is not None else os.environ
        self.resolution_attempts = 0
        self.semantic_resolutions = 0
        self.visual_fallbacks = 0
        self._replay_mode = replay_mode
        self._visual_resolver = visual_resolver
        self._trace_store = trace_store
        self.trace_case_id = trace_case_id
        self._evidence_recorder = evidence_recorder
        self._window_focus = window_focus
        self._focused_window: tuple[str, str, str] | None = None

    def _focus_for(self, selector: ReplaySelector | None, observation: Any) -> Any:
        """Bring the step's own window forward, observing it afresh."""
        if self._window_focus is None or selector is None:
            return observation
        process = str(selector.window.get("processName") or "")
        title_regex = str(selector.window.get("titleRegex") or "")
        # The window root is the identity that survives the application
        # replacing a window, renaming it, or two of its processes sharing a
        # title; the title alone cannot tell those windows apart.
        root = str(selector.window.get("rootAutomationId") or "")
        if not process or not (title_regex or root):
            return observation
        wanted = (process, title_regex, root)
        if wanted == self._focused_window:
            return observation
        refreshed = self._window_focus(process, title_regex, root)
        self._focused_window = wanted
        return refreshed if refreshed is not None else observation

    def prepare_observation_for_step(self, step: AtomicTestStep) -> None:
        """Focus the recorded window before the executor refreshes its lock.

        A preceding click can transfer foreground to a newly opened window.
        Refreshing the old lock first would fail before replay can switch to
        the next step's window.
        """
        self._focus_for(self._selectors.get(step.step_id), None)

    def recover_pre_action_observation(self, step: AtomicTestStep) -> bool:
        """Re-lock the recorded window once if its cached lock went stale.

        This runs before dispatch, so it cannot repeat a GUI mutation. A
        missing selector or failed focus still blocks the step.
        """
        selector = self._selectors.get(step.step_id)
        if selector is None or self._window_focus is None:
            return False
        self._focused_window = None
        self._focus_for(selector, None)
        return True

    def recover_post_action_observation(self, step: AtomicTestStep) -> bool:
        """Re-lock a declared next window when a click transferred foreground.

        This is only safe for an action receipt and optional window-open
        verifier; a control assertion on the old window must not be silently
        evaluated against a different window.
        """
        current = self._selectors.get(step.step_id)
        following = self._next_selectors.get(step.step_id)
        if (self._window_focus is None or current is None or following is None
                or current.window == following.window
                or any(exp.type not in {"action_ok", "window_open"}
                       for exp in step.expectations)):
            return False
        self._focus_for(following, None)
        return True

    @property
    def evidence_index(self) -> Mapping[str, EvidenceRecord]:
        if self._evidence_recorder is None:
            return {}
        return self._evidence_recorder.evidence_index

    @property
    def evidence_root(self) -> Path | None:
        if self._evidence_recorder is None:
            return None
        return self._evidence_recorder.root

    def __call__(self, step: AtomicTestStep, observation: Any) -> AtomicTestStep:
        if self._trace_store is not None:
            self._trace_store.append_dict(
                EventType.STEP_STARTED,
                payload={"step_no": step.step_no, "action_id": step.action_id},
                case_id=self.trace_case_id,
                step_id=step.step_id,
            )
        selector = self._selectors.get(step.step_id)
        observation = self._focus_for(selector, observation)
        if self._evidence_recorder is not None:
            self._evidence_recorder.record(step, observation)
        target_ref = step.target_ref
        candidate = None
        action_id = step.action_id
        visual_args = None
        # A pure assertion has no action target to materialize. Its evaluator
        # resolves the declared identity against the fresh observation; forcing
        # a click-style selector first makes changing text fail prematurely.
        if selector is not None and (selector.control or selector.visual) and step.action_id != "observe.assert":
            self.resolution_attempts += 1
            if not selector.control and self._replay_mode == "semantic_only":
                raise StepMaterializationError(
                    "replay_selector_semantic_missing",
                    "this click has only a visual selector; choose semantic_first or visual_only replay",
                )
            use_visual_only = (self._replay_mode == "visual_only" or not selector.control)
            if use_visual_only:
                if self._visual_resolver is None or selector.visual is None:
                    raise StepMaterializationError(
                        "visual_template_missing", "visual_only replay requires a visual selector"
                    )
                fallback = self._visual_resolver.resolve(step.action_id, selector, observation)
                action_id = fallback.action_id
                visual_args = dict(fallback.args)
                target_ref = None
                self.visual_fallbacks += 1
            elif (self._replay_mode == "semantic_first" and
                  self._visual_resolver is not None and selector.visual is not None):
                visual_evidence = self._visual_resolver.collect_evidence(selector, observation)
                snapshot_id = _snapshot_id(observation)
                bundle = {
                    "observation_id": snapshot_id,
                    "controls": [item if isinstance(item, Mapping) else vars(item)
                                 for item in _targets(observation)],
                    "active_window": _get(observation, "active_window") or {},
                    "window_identity": _get(observation, "window_identity") or {},
                    "screenshot_scope": _get(observation, "screenshot_scope"),
                    "screenshot_redacted": _get(observation, "screenshot_redacted"),
                }
                ocr_boxes = tuple(_get(observation, "ocr_boxes") or ())
                try:
                    fused = HybridLocator().resolve(
                        LocatorRequest(step.action_id, selector.window, selector.control,
                                       ocr_text=str(selector.control.get("name") or "")),
                        bundle, LocatorPolicy(mode="semantic_first"),
                        visual_candidates=visual_evidence, ocr_boxes=ocr_boxes,
                    )
                except LocatorError as exc:
                    if exc.code == "locator_ambiguous":
                        semantic_count = len(HybridLocator().collect_candidates(
                            LocatorRequest(step.action_id, selector.window, selector.control), bundle))
                        code = "visual_match_ambiguous" if semantic_count == 0 else "replay_target_ambiguous"
                    else:
                        code = exc.code
                    raise StepMaterializationError(code, str(exc),
                                                   details={"candidate_count": exc.candidate_count}) from exc
                if fused.candidate is None:
                    fallback = self._visual_resolver.resolve(step.action_id, selector, observation)
                    action_id = fallback.action_id
                    visual_args = dict(fallback.args)
                    target_ref = None
                    self.visual_fallbacks += 1
                else:
                    item = fused.candidate
                    target_ref = TargetRef(
                        snapshot_id=snapshot_id, target_id=item.target_id,
                        expected_process_name=str(selector.window.get("processName") or ""),
                        fingerprint=_get(item.source, "fingerprint"),
                        selector_hint=dict(selector.control),
                    )
                    self.semantic_resolutions += 1
                    candidate = item.source
            else:
                try:
                    target_ref = resolve_replay_selector(selector, observation)
                except StepMaterializationError as exc:
                    if (
                        exc.code != "replay_target_not_found"
                        or self._replay_mode != "semantic_first"
                        or self._visual_resolver is None
                        or selector.visual is None
                    ):
                        raise
                    fallback = self._visual_resolver.resolve(step.action_id, selector, observation)
                    action_id = fallback.action_id
                    visual_args = dict(fallback.args)
                    target_ref = None
                    self.visual_fallbacks += 1
                else:
                    self.semantic_resolutions += 1
                    candidate = next(
                        (
                            item for item in _targets(observation)
                            if _get(item, "target_id", "targetId") == target_ref.target_id
                        ),
                        None,
                    )
        args = dict(step.args)
        source = args.pop("textSource", None)
        if source is not None:
            if not isinstance(source, Mapping) or source.get("kind") != "env":
                raise StepMaterializationError(
                    "golden_secret_source_invalid", "only environment secret sources are supported"
                )
            name = source.get("name")
            if not isinstance(name, str) or name not in self._environment:
                raise StepMaterializationError(
                    "golden_secret_missing",
                    f"required environment secret {name!r} is unavailable",
                    details={"environment_name": name},
                )
            args["text"] = self._environment[name]
        if visual_args is not None:
            args = visual_args

        control = selector.control if selector is not None else {}
        expected_process = selector.window.get("processName") if selector is not None else None
        if visual_args is None and step.action_id in {"gui.click", "gui.click_target"}:
            semantic_args = {
                "automation_id": control.get("automationId"),
                "text": None if control.get("automationId") or control.get("identifier") else control.get("name"),
                "control_type": control.get("controlType"),
                "expected_process_name": expected_process,
            }
            args.update({key: value for key, value in semantic_args.items() if value not in (None, "")})
        elif step.action_id == "gui.type_text":
            semantic_args = {
                "control_id": _get(candidate, "control_id", "controlId"),
                "class_name": _get(candidate, "class_name", "className"),
            }
            args.update({key: value for key, value in semantic_args.items() if value not in (None, "")})
        elif step.action_id == "gui.select":
            semantic_args = {
                "control_id": _get(candidate, "control_id", "controlId"),
                "text": control.get("name"),
                "class_name": _get(candidate, "class_name", "className"),
            }
            args.update({key: value for key, value in semantic_args.items() if value not in (None, "")})
        elif step.action_id == "pointer.drag" and visual_args is None:
            end_selector = self._end_selectors.get(step.step_id)
            if end_selector is None or not end_selector.control:
                raise StepMaterializationError(
                    "replay_drag_end_missing",
                    "a recorded drag requires a stable release-point selector",
                )
            end_target = resolve_replay_selector(end_selector, observation)
            end_candidate = next(
                (
                    item for item in _targets(observation)
                    if _get(item, "target_id", "targetId") == end_target.target_id
                ),
                None,
            )
            start_x, start_y = _anchor_point(candidate, control)
            end_x, end_y = _anchor_point(end_candidate, end_selector.control)
            args.update({"x1": start_x, "y1": start_y, "x2": end_x, "y2": end_y})
        elif step.action_id in {
            "pointer.double_click", "pointer.right_click",
            "pointer.middle_click", "pointer.scroll",
        }:
            rect = _get(candidate, "rect", "rectangle")
            if isinstance(rect, (list, tuple)) and len(rect) == 4:
                x = (int(rect[0]) + int(rect[2])) // 2
                y = (int(rect[1]) + int(rect[3])) // 2
                args.update({"x": x, "y": y})
            if expected_process and step.action_id in {
                "pointer.double_click", "pointer.right_click",
                "pointer.middle_click",
            }:
                args["expected_process_name"] = expected_process
        return replace(step, action_id=action_id, target_ref=target_ref, args=args)


@dataclass(frozen=True)
class ReplayRuntime:
    executor: AtomicExecutor
    catalog_version: str
    catalog_digest: str
    trace_integrity: bool = True
    cleanup_passed: bool = True
    execution_context: ExecutionContext = ExecutionContext()
    trace_store: TraceStore | None = None
    replay_mode: str = "semantic_only"
    visual_resolver: SafeVisualResolver | None = None
    #: Focus the window a step belongs to and return a fresh observation of it.
    window_focus: Callable[[str, str, str], Any] | None = None
    #: Persist source-redacted runtime frames into the trace's screenshots
    #: directory.  Requires a trace store; frames that the target did not
    #: redact are refused rather than written.
    persist_replay_screenshots: bool = False
    end_product_run: Callable[[], None] | None = None


@dataclass(frozen=True)
class ReplayRunResult:
    case_result: CaseRunResult
    evaluation: ReplayEvaluation
    cleanup_result: CaseRunResult | None = None
    trace_root: Path | None = None
    evaluation_path: Path | None = None

    @property
    def task_success(self) -> bool:
        return self.evaluation.task_success

    @property
    def summary(self) -> dict[str, Any]:
        return {
            "outcome": self.case_result.outcome.value,
            "evaluation": self.evaluation.to_dict(),
            "cleanup_outcome": (
                self.cleanup_result.outcome.value if self.cleanup_result else None
            ),
            "trace_directory": str(self.trace_root) if self.trace_root else None,
            "evaluation_path": str(self.evaluation_path) if self.evaluation_path else None,
        }


def _persist_evaluation(path: Path, evaluation: ReplayEvaluation) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".evaluation.json.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(canonical_json(evaluation.to_dict()))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _run_golden_trace(target: ReplayRuntime, golden: GoldenTrace) -> ReplayRunResult:
    if golden.status != "ready":
        # 门槛本身不在这里改，只是把「为什么不能回放」说明白。incomplete 最常见
        # 来源是 scroll/drag 没有绑定的结果 verifier —— 那不是补模板能解决的，
        # 要补的是「证明移动产生了状态变化」的断言。
        unready = [
            (s.step_id, s.action_id)
            for s in (*golden.steps, *golden.cleanup)
            if s.status != "ready"
        ]
        hint = ""
        if unready:
            hint = (f"；{len(unready)} 个步骤未 ready，例如 {unready[:5]}。"
                    "scroll/drag 通常缺绑定的结果 verifier：先用 record assert "
                    "在动作后补一条内容/位置断言，再重编译。")
        raise RecordingModelError(
            "golden_trace_incomplete",
            f"only ready golden traces can replay (golden.status={golden.status!r}){hint}",
            path="golden.status",
        )
    expected_version = golden.catalog.get("version")
    expected_digest = golden.catalog.get("digest")
    if expected_version != target.catalog_version or expected_digest != target.catalog_digest:
        raise RecordingModelError(
            "golden_catalog_mismatch",
            "golden trace catalog binding differs from the replay runtime",
            path="golden.catalog",
        )
    expected_profile = golden.environment.get("profile")
    if expected_profile and expected_profile != target.execution_context.profile:
        raise RecordingModelError(
            "golden_profile_mismatch",
            "golden trace profile differs from the replay execution profile",
            path="golden.environment.profile",
        )

    if target.persist_replay_screenshots and target.trace_store is None:
        raise RecordingModelError(
            "replay_evidence_root_missing",
            "persisting replay screenshots requires a trace store",
            path="replay.trace_store",
        )
    main_case = golden_to_test_case(golden)
    def make_materializer(case_id: str) -> GoldenStepMaterializer:
        return GoldenStepMaterializer(
            golden,
            replay_mode=target.replay_mode,
            visual_resolver=target.visual_resolver,
            trace_store=target.trace_store,
            trace_case_id=case_id,
            evidence_recorder=(
                ReplayEvidenceRecorder(target.trace_store)
                if target.persist_replay_screenshots and target.trace_store is not None
                else None
            ),
            window_focus=target.window_focus,
        )

    materializer = make_materializer(main_case.case_id)
    case_result = target.executor.run_case(
        main_case,
        step_materializer=materializer,
        execution_context=target.execution_context,
    )

    def append_step_results(result: CaseRunResult) -> None:
        if target.trace_store is None:
            return
        for step_result in result.step_results:
            for expectation in step_result.expectation_results:
                target.trace_store.append_dict(
                    EventType.EXPECTATION_RESULT,
                    payload=expectation.to_dict(),
                    case_id=result.case_id,
                    step_id=step_result.step_id,
                )
            target.trace_store.append_dict(
                EventType.STEP_COMPLETED,
                payload={"status": step_result.status.value, "error": step_result.error},
                case_id=result.case_id,
                step_id=step_result.step_id,
            )

    append_step_results(case_result)
    cleanup_result = None
    if golden.cleanup:
        cleanup_case = TestCase(
            case_id=golden_to_test_case(golden).case_id + "-CLEANUP",
            title=golden.name + " cleanup",
            steps=tuple(
                _recorded_to_atomic(recorded, step_no)
                for step_no, recorded in enumerate(golden.cleanup, start=1)
            ),
        )
        if target.trace_store is not None:
            target.trace_store.append_dict(
                EventType.CLEANUP_STARTED,
                payload={"case_id": cleanup_case.case_id},
                case_id=cleanup_case.case_id,
            )
        cleanup_materializer = make_materializer(cleanup_case.case_id)
        cleanup_result = target.executor.run_case(
            cleanup_case,
            step_materializer=cleanup_materializer,
            execution_context=target.execution_context,
        )
        append_step_results(cleanup_result)
        if target.trace_store is not None:
            target.trace_store.append_dict(
                EventType.CLEANUP_COMPLETED,
                payload={"case_id": cleanup_case.case_id, "outcome": cleanup_result.outcome.value},
                case_id=cleanup_case.case_id,
            )

    required_results = [
        result for step, result in zip(golden.steps, case_result.step_results) if step.required
    ]
    passed = sum(result.status is StepStatus.PASSED for result in required_results)
    assertion_results = [
        expectation
        for result in case_result.step_results
        for expectation in result.expectation_results
        if expectation.expectation_type != "action_ok"
    ]
    assertion_rate = (
        sum(item.status is StepStatus.PASSED for item in assertion_results) / len(assertion_results)
        if assertion_results else 1.0
    )
    semantic_rate = (
        materializer.semantic_resolutions / materializer.resolution_attempts
        if materializer.resolution_attempts else 1.0
    )
    cleanup_passed = target.cleanup_passed and (
        cleanup_result is None or cleanup_result.outcome.value == "passed"
    )
    action_pairs = [
        (step, result)
        for step, result in zip(golden.steps, case_result.step_results)
        if step.action_id is not None
    ]
    if cleanup_result is not None:
        action_pairs.extend(
            (step, result)
            for step, result in zip(golden.cleanup, cleanup_result.step_results)
            if step.action_id is not None
        )
    executed_path_actions = sum(result.action is not None for _, result in action_pairs)
    path_fidelity = (
        executed_path_actions / len(action_pairs) if action_pairs else 1.0
    )
    if target.trace_store is not None:
        terminal = (
            EventType.TRACE_COMPLETED
            if case_result.outcome.value == "passed" and cleanup_passed
            else EventType.TRACE_ABORTED
        )
        target.trace_store.finalize(
            terminal,
            payload={
                "case_id": case_result.case_id,
                "outcome": case_result.outcome.value,
                "cleanup_passed": cleanup_passed,
            },
        )
        trace_integrity = target.trace_integrity and target.trace_store.verify().ok
    else:
        trace_integrity = target.trace_integrity

    task_success = (
        passed == len(required_results)
        and assertion_rate == 1.0
        and cleanup_passed
        and trace_integrity
    )
    evaluation = ReplayEvaluation(
        task_success=task_success,
        required_steps=len(required_results),
        passed_steps=passed,
        assertion_pass_rate=assertion_rate,
        semantic_resolution_rate=semantic_rate,
        visual_fallback_count=materializer.visual_fallbacks,
        path_fidelity=path_fidelity,
        cleanup_passed=cleanup_passed,
        trace_integrity=trace_integrity,
    )
    evaluation_path = None
    if target.trace_store is not None:
        evaluation_path = target.trace_store.root / "evaluation.json"
        _persist_evaluation(evaluation_path, evaluation)
    return ReplayRunResult(
        case_result,
        evaluation,
        cleanup_result,
        target.trace_store.root if target.trace_store is not None else None,
        evaluation_path,
    )


def replay_golden_trace(target: ReplayRuntime, golden: GoldenTrace) -> ReplayRunResult:
    """Ensure target GUI lease is released even when replay raises."""
    try:
        return _run_golden_trace(target, golden)
    finally:
        if target.end_product_run is not None:
            target.end_product_run()


__all__ = [
    "GoldenStepMaterializer",
    "ReplayEvidenceRecorder",
    "ReplayRunResult",
    "ReplayRuntime",
    "golden_to_test_case",
    "load_golden_trace",
    "replay_golden_trace",
    "resolve_replay_selector",
]
