"""
expectations.py — Typed expectation evaluators (architecture §9.3, FR-P1.2-07).

P0.2 ships a `EXPECTATION_TYPE_REGISTRY` of metadata (timeout range,
required fields). P1.2 adds the evaluators themselves.

Evaluators are pure functions with the signature:

    evaluate(step, expectation, observation, action_receipt)
        -> ExpectationResult

The registry `EXPECTATION_REGISTRY` is keyed by `expectation_type`. P1.2
ships 9 evaluators:

    action_ok
    window_open
    window_closed
    active_window_owner
    control_exists
    control_absent
    control_text_equals
    control_text_contains
    window_text_contains

`visual_evidence_captured` validates role-matching evidence and its
on-disk digest (FR-P1.4-08).

Evaluator contract:

  * An evaluator must return `passed` or `failed`.  It must NOT return
    `blocked` — `blocked` is an executor-level outcome for missing
    preconditions, not an evaluator-level outcome.
  * `expectation.timeout_seconds` is enforced by `executor.py` at a
    higher level via polling, not inside the evaluator.
  * A raised exception inside the evaluator is converted to a
    `failed` `ExpectationResult` whose `diagnostic` is the exception
    class name + message. Architecture §9.3: "Unknown expectation
    types fail validation before execution." P0.2 already does that;
    P1.2 must not regress it.
"""

from __future__ import annotations

from contextvars import ContextVar, Token
from datetime import datetime, timezone
from pathlib import Path
import re
import time as _time
from typing import Any, Callable, Mapping

from target.action_dispatcher import ActionReceipt
from target.time_assertions import matching_times, parse_time_range

from .models import ExpectationResult, StepStatus


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _passed(
    expectation_type: str,
    expected: Any = None,
    actual: Any = None,
    observation_id: str | None = None,
    duration_ms: int = 0,
    diagnostic: str = "",
) -> ExpectationResult:
    return ExpectationResult(
        expectation_type=expectation_type,
        status=StepStatus.PASSED,
        expected=expected,
        actual=actual,
        observation_id=observation_id,
        duration_ms=duration_ms,
        diagnostic=diagnostic,
    )


def _failed(
    expectation_type: str,
    expected: Any,
    actual: Any,
    observation_id: str | None,
    duration_ms: int,
    diagnostic: str = "",
) -> ExpectationResult:
    return ExpectationResult(
        expectation_type=expectation_type,
        status=StepStatus.FAILED,
        expected=expected,
        actual=actual,
        observation_id=observation_id,
        duration_ms=duration_ms,
        diagnostic=diagnostic,
    )


def _observation_id(observation: Any) -> str | None:
    if observation is None:
        return None
    if isinstance(observation, dict):
        return observation.get("observation_id") or observation.get("snapshot_id")
    return getattr(observation, "observation_id", None) or getattr(
        observation, "snapshot_id", None
    )


def _windows(observation: Any) -> list[dict]:
    """Best-effort extraction of window list from observation dict."""
    if observation is None:
        return []
    if isinstance(observation, dict):
        wins = observation.get("windows") or []
        return wins if isinstance(wins, list) else []
    wins = getattr(observation, "windows", None)
    if isinstance(wins, list):
        return wins
    targets = getattr(observation, "targets", ()) or ()
    return [_target_dict(item) for item in targets if getattr(item, "kind", None) == "window"]


def _controls(observation: Any) -> list[dict]:
    if observation is None:
        return []
    if isinstance(observation, dict):
        ctrls = observation.get("controls") or []
        return ctrls if isinstance(ctrls, list) else []
    ctrls = getattr(observation, "controls", None)
    if isinstance(ctrls, list):
        return ctrls
    targets = getattr(observation, "targets", ()) or ()
    return [_target_dict(item) for item in targets if getattr(item, "kind", None) == "control"]


def _has_observed_collection(observation: Any, field: str) -> bool:
    """A negative assertion needs an actual inventory, not a missing field."""
    if observation is None:
        return False
    if isinstance(observation, Mapping):
        values = observation.get(field)
    else:
        values = getattr(observation, field, None)
    if isinstance(values, list):
        return all(isinstance(item, Mapping) for item in values)
    if isinstance(observation, Mapping):
        return False
    targets = getattr(observation, "targets", None)
    return (isinstance(targets, (list, tuple))
            and all(hasattr(item, "kind") for item in targets))


def _target_dict(target: Any) -> dict:
    if isinstance(target, dict):
        return target
    fields = (
        "target_id", "process_name", "title", "control_type",
        "automation_id", "identifier", "text", "value", "checked", "enabled",
    )
    return {field: getattr(target, field, None) for field in fields}


def _active_window(observation: Any) -> dict | None:
    if observation is None:
        return None
    if isinstance(observation, dict):
        return observation.get("active_window") or None
    return getattr(observation, "active_window", None)


def _matches(control: dict, expectation_value: Any) -> bool:
    """P1.2 matcher: equality on `text`, `control_id`, `automation_id`, `title`.

    P1.2 does not implement regex / fuzzy matching — that is a future
    P2.x refinement.  Today the user-supplied `value` is compared to
    any of the stable identity fields.
    """
    if expectation_value is None:
        return True
    if isinstance(expectation_value, dict):
        for k, v in expectation_value.items():
            if k == "title_regex":
                try:
                    if not re.search(str(v), str(control.get("title") or "")):
                        return False
                except re.error:
                    return False
                continue
            if control.get(k) != v:
                return False
        return True
    for field in ("text", "title", "automation_id", "control_id"):
        if field in control and control[field] == expectation_value:
            return True
    return False


# ---------------------------------------------------------------------------
# Evaluators
# ---------------------------------------------------------------------------


def evaluate_action_ok(
    step: Any,
    expectation: Any,
    observation: Any,
    action_receipt: ActionReceipt | None,
) -> ExpectationResult:
    """`action_ok` is true when the receipt reports success (P1.2 MVP).

    The receipt is the executor's source of truth for "did the action
    run?"; an explicit `expected=False` flips the assertion to expect
    failure (e.g. negative-test cases).
    """
    expected_ok = bool(expectation.value) if expectation.value is not None else True
    if action_receipt is None:
        return _failed(
            "action_ok", expected=expected_ok, actual=None,
            observation_id=_observation_id(observation), duration_ms=0,
            diagnostic="no action receipt",
        )
    actual_ok = bool(action_receipt.ok)
    if actual_ok == expected_ok:
        return _passed(
            "action_ok",
            expected=expected_ok,
            actual=actual_ok,
            observation_id=action_receipt.action_id,
            diagnostic="",
        )
    return _failed(
        "action_ok",
        expected=expected_ok,
        actual=actual_ok,
        observation_id=action_receipt.action_id,
        duration_ms=0,
        diagnostic=f"action_ok expected={expected_ok} got={actual_ok}",
    )


def _expectation_value_window(expectation: Any) -> Any:
    # Accept either a string (process / title / class) or a dict
    # containing any combination of `process`, `title`, `class`.
    return expectation.value


def evaluate_window_open(
    step: Any, expectation: Any, observation: Any, action_receipt: ActionReceipt | None,
) -> ExpectationResult:
    expected = _expectation_value_window(expectation)
    windows = _windows(observation)
    for w in windows:
        if _matches(w, expected):
            return _passed("window_open", expected, w, _observation_id(observation), 0)
    return _failed("window_open", expected, windows, _observation_id(observation), 0,
                   diagnostic="no matching window")


def evaluate_window_closed(
    step: Any, expectation: Any, observation: Any, action_receipt: ActionReceipt | None,
) -> ExpectationResult:
    expected = _expectation_value_window(expectation)
    if not _has_observed_collection(observation, "windows"):
        return _failed("window_closed", expected, None, _observation_id(observation), 0,
                       diagnostic="window inventory unavailable or invalid")
    windows = _windows(observation)
    for w in windows:
        if _matches(w, expected):
            return _failed("window_closed", expected, w, _observation_id(observation), 0,
                           diagnostic="matching window still present")
    return _passed("window_closed", expected, windows, _observation_id(observation), 0)


def evaluate_active_window_owner(
    step: Any, expectation: Any, observation: Any, action_receipt: ActionReceipt | None,
) -> ExpectationResult:
    expected = expectation.value
    active = _active_window(observation) or {}
    actual_owner = active.get("owner") or active.get("process_name")
    if expected == actual_owner:
        return _passed("active_window_owner", expected, actual_owner,
                       _observation_id(observation), 0)
    return _failed("active_window_owner", expected, actual_owner,
                   _observation_id(observation), 0,
                   diagnostic="active window owner mismatch")


def _matching_controls(observation: Any, expected: Any) -> list[dict]:
    return [control for control in _controls(observation)
            if _matches(control, expected)]


def _valid_control_selector(expected: Any) -> bool:
    """A control assertion must name a widget, never the whole inventory."""
    if isinstance(expected, Mapping):
        identity_fields = {
            "automation_id", "identifier", "control_id", "control_type",
            "text", "name", "title", "title_regex", "class_name",
        }
        return bool(identity_fields.intersection(expected)) and all(
            isinstance(key, str) and bool(key)
            and value is not None and value != ""
            for key, value in expected.items()
        )
    return isinstance(expected, str) and bool(expected)


def _find_control(observation: Any, expected: Any) -> dict | None:
    """A positive control assertion must identify exactly one current widget."""
    if not _valid_control_selector(expected):
        return None
    matches = _matching_controls(observation, expected)
    return matches[0] if len(matches) == 1 else None


def evaluate_control_exists(
    step: Any, expectation: Any, observation: Any, action_receipt: ActionReceipt | None,
) -> ExpectationResult:
    expected = expectation.value
    found = _find_control(observation, expected)
    if found is not None:
        return _passed("control_exists", expected, found, _observation_id(observation), 0)
    return _failed("control_exists", expected, None, _observation_id(observation), 0,
                   diagnostic="control not uniquely found")


def evaluate_control_absent(
    step: Any, expectation: Any, observation: Any, action_receipt: ActionReceipt | None,
) -> ExpectationResult:
    expected = expectation.value
    if not _valid_control_selector(expected):
        return _failed("control_absent", expected, None, _observation_id(observation), 0,
                       diagnostic="control selector is missing or invalid")
    if not _has_observed_collection(observation, "controls"):
        return _failed("control_absent", expected, None, _observation_id(observation), 0,
                       diagnostic="control inventory unavailable or invalid")
    found = _matching_controls(observation, expected)
    if not found:
        return _passed("control_absent", expected, None, _observation_id(observation), 0)
    return _failed("control_absent", expected, found, _observation_id(observation), 0,
                   diagnostic="control unexpectedly present")


def _control_text(control: dict | None) -> str:
    if control is None:
        return ""
    return str(control.get("text") or control.get("name") or "")


def _control_time_texts(control: dict | None) -> tuple[str, ...]:
    """UIA/Accessibility may expose log timestamps as text, name, or value."""
    if control is None:
        return ()
    return tuple(str(value) for key in ("text", "name", "value")
                 if (value := control.get(key)) not in (None, ""))


def evaluate_control_text_equals(
    step: Any, expectation: Any, observation: Any, action_receipt: ActionReceipt | None,
) -> ExpectationResult:
    # `expectation.value` is {"match": {...}, "text": "..."}.
    raw = expectation.value
    if not isinstance(raw, dict):
        return _failed("control_text_equals", raw, None, _observation_id(observation),
                       0, diagnostic="value must be {match, text}")
    match = raw.get("match")
    text = raw.get("text")
    control = _find_control(observation, match)
    if control is None:
        return _failed("control_text_equals", text, None, _observation_id(observation),
                       0, diagnostic="assertion control not uniquely found")
    actual = _control_text(control)
    if actual == text:
        return _passed("control_text_equals", text, actual, _observation_id(observation), 0)
    return _failed("control_text_equals", text, actual, _observation_id(observation), 0,
                   diagnostic="text mismatch")


def evaluate_control_text_contains(
    step: Any, expectation: Any, observation: Any, action_receipt: ActionReceipt | None,
) -> ExpectationResult:
    raw = expectation.value
    if not isinstance(raw, dict):
        return _failed("control_text_contains", raw, None, _observation_id(observation),
                       0, diagnostic="value must be {match, text}")
    match = raw.get("match")
    text = raw.get("text", "")
    if not isinstance(text, str) or not text:
        return _failed("control_text_contains", text, None, _observation_id(observation),
                       0, diagnostic="expected substring must be non-empty")
    control = _find_control(observation, match)
    if control is None:
        return _failed("control_text_contains", text, None, _observation_id(observation),
                       0, diagnostic="assertion control not uniquely found")
    actual = _control_text(control)
    if text in actual:
        return _passed("control_text_contains", text, actual, _observation_id(observation), 0)
    return _failed("control_text_contains", text, actual, _observation_id(observation), 0,
                   diagnostic="text not contained")


class _ReplayClock:
    """The clock a time expectation is rendered against.

    A recorded assertion about "today" must mean the day the replay runs, not
    the day the recording was made, so the pattern is rendered here rather than
    frozen into the golden trace.  Tests substitute a fixed clock; nothing else
    should.
    """

    _now: ContextVar[Callable[[], "datetime"] | None] = ContextVar(
        "replay_clock", default=None,
    )

    @classmethod
    def set(cls, now: Callable[[], "datetime"] | None) -> None:
        cls._now.set(now)

    @classmethod
    def now(cls) -> "datetime":
        from datetime import datetime

        now = cls._now.get()
        return now() if now is not None else datetime.now()


def render_time_pattern(pattern: str, now: "datetime | None" = None) -> str:
    """Render a strftime pattern against the replay-time clock."""
    moment = now if now is not None else _ReplayClock.now()
    return moment.strftime(pattern)


def evaluate_control_text_contains_time(
    step: Any, expectation: Any, observation: Any, action_receipt: ActionReceipt | None,
) -> ExpectationResult:
    """Assert a control shows the time *now*, not the time it was recorded.

    `expectation.value` is {"match": {...}, "pattern": "<strftime>"}.  The
    pattern is rendered when this runs, so a log view recorded showing
    2026-08-18 still passes on any later day.
    """
    raw = expectation.value
    if not isinstance(raw, dict):
        return _failed("control_text_contains_time", raw, None, _observation_id(observation),
                       0, diagnostic="value must be {match, pattern}")
    pattern = raw.get("pattern")
    if not isinstance(pattern, str) or not pattern:
        return _failed("control_text_contains_time", pattern, None,
                       _observation_id(observation), 0,
                       diagnostic="pattern must be a non-empty strftime string")
    try:
        expected = render_time_pattern(pattern)
    except (ValueError, TypeError) as exc:
        return _failed("control_text_contains_time", pattern, None,
                       _observation_id(observation), 0,
                       diagnostic=f"invalid strftime pattern: {exc}")
    control = _find_control(observation, raw.get("match"))
    if control is None:
        return _failed("control_text_contains_time", expected, None,
                       _observation_id(observation), 0,
                       diagnostic="assertion control not uniquely found")
    actual = _control_text(control)
    if expected in actual:
        return _passed("control_text_contains_time", expected, actual,
                       _observation_id(observation), 0)
    return _failed("control_text_contains_time", expected, actual,
                   _observation_id(observation), 0,
                   diagnostic=f"rendered pattern {pattern!r} not contained")


def _evaluate_control_property(expectation_type, property_name, expectation, observation):
    raw = expectation.value
    if not isinstance(raw, dict) or "expected" not in raw:
        return _failed(
            expectation_type, raw, None, _observation_id(observation), 0,
            diagnostic="value must be {match, expected}",
        )
    control = _find_control(observation, raw.get("match"))
    if control is None:
        return _failed(
            expectation_type, raw["expected"], None, _observation_id(observation), 0,
            diagnostic="assertion control not uniquely found",
        )
    actual = control.get(property_name) if control is not None else None
    expected = raw["expected"]
    if actual == expected:
        return _passed(expectation_type, expected, actual, _observation_id(observation), 0)
    return _failed(
        expectation_type, expected, actual, _observation_id(observation), 0,
        diagnostic=f"{property_name} mismatch",
    )


def evaluate_control_value_equals(step, expectation, observation, action_receipt):
    return _evaluate_control_property("control_value_equals", "value", expectation, observation)


def evaluate_control_checked_equals(step, expectation, observation, action_receipt):
    return _evaluate_control_property("control_checked_equals", "checked", expectation, observation)


def evaluate_control_enabled_equals(step, expectation, observation, action_receipt):
    return _evaluate_control_property("control_enabled_equals", "enabled", expectation, observation)


def evaluate_window_text_contains(
    step: Any, expectation: Any, observation: Any, action_receipt: ActionReceipt | None,
) -> ExpectationResult:
    expected_text = expectation.value
    if not isinstance(expected_text, str) or not expected_text:
        return _failed("window_text_contains", expected_text, None,
                       _observation_id(observation), 0,
                       diagnostic="expected substring must be non-empty")
    hits: list[str] = []
    for w in _windows(observation):
        text = str(w.get("text") or w.get("title") or "")
        if expected_text in text:
            hits.append(text)
    if hits:
        return _passed("window_text_contains", expected_text, hits,
                       _observation_id(observation), 0)
    # Fallback: search every control's text.
    for c in _controls(observation):
        text = _control_text(c)
        if expected_text in text:
            return _passed("window_text_contains", expected_text, [text],
                           _observation_id(observation), 0)
    return _failed("window_text_contains", expected_text, [],
                   _observation_id(observation), 0,
                   diagnostic="text not found in window or tree")


def evaluate_window_text_contains_time(
    step: Any, expectation: Any, observation: Any, action_receipt: ActionReceipt | None,
) -> ExpectationResult:
    """Assert the window shows the time *now*, without naming a control.

    `expectation.value` is the strftime pattern itself.  Writing an assertion
    should not require knowing a control's automation id: the user knows what
    the screen must show, not which widget shows it.
    """
    pattern = expectation.value
    if not isinstance(pattern, str) or not pattern:
        return _failed("window_text_contains_time", pattern, None,
                       _observation_id(observation), 0,
                       diagnostic="pattern must be a non-empty strftime string")
    try:
        expected = render_time_pattern(pattern)
    except (ValueError, TypeError) as exc:
        return _failed("window_text_contains_time", pattern, None,
                       _observation_id(observation), 0,
                       diagnostic=f"invalid strftime pattern: {exc}")
    for window in _windows(observation):
        text = str(window.get("text") or window.get("title") or "")
        if expected in text:
            return _passed("window_text_contains_time", expected, [text],
                           _observation_id(observation), 0)
    for control in _controls(observation):
        text = _control_text(control)
        if expected in text:
            return _passed("window_text_contains_time", expected, [text],
                           _observation_id(observation), 0)
    return _failed("window_text_contains_time", expected, [],
                   _observation_id(observation), 0,
                   diagnostic=f"rendered pattern {pattern!r} not found in window or tree")


def _time_range_spec(value: Any) -> tuple[str, int | None]:
    if not isinstance(value, dict):
        raise ValueError("time range value must be {prefix, durationSeconds?}")
    prefix = value.get("prefix")
    duration = value.get("durationSeconds")
    parse_time_range(prefix, duration)
    return prefix, duration


def evaluate_control_text_time_range(
    step: Any, expectation: Any, observation: Any, action_receipt: ActionReceipt | None,
) -> ExpectationResult:
    raw = expectation.value
    try:
        prefix, duration = _time_range_spec(raw)
    except ValueError as exc:
        return _failed("control_text_time_range", raw, None, _observation_id(observation), 0, str(exc))
    control = _find_control(observation, raw.get("match"))
    if control is None:
        return _failed("control_text_time_range", raw, None, _observation_id(observation), 0,
                       "assertion control not uniquely found")
    actual = _control_time_texts(control)
    if any(matching_times(text, prefix, duration) for text in actual):
        return _passed("control_text_time_range", raw, actual, _observation_id(observation), 0)
    return _failed("control_text_time_range", raw, actual, _observation_id(observation), 0,
                   "no observed time is within the expected range")


def evaluate_window_text_time_range(
    step: Any, expectation: Any, observation: Any, action_receipt: ActionReceipt | None,
) -> ExpectationResult:
    raw = expectation.value
    try:
        prefix, duration = _time_range_spec(raw)
    except ValueError as exc:
        return _failed("window_text_time_range", raw, None, _observation_id(observation), 0, str(exc))
    texts = [str(window.get("text") or window.get("title") or "") for window in _windows(observation)]
    texts.extend(text for control in _controls(observation)
                 for text in _control_time_texts(control))
    matches = [text for text in texts if matching_times(text, prefix, duration)]
    if matches:
        return _passed("window_text_time_range", raw, matches, _observation_id(observation), 0)
    return _failed("window_text_time_range", raw, texts, _observation_id(observation), 0,
                   "no observed time is within the expected range")


# ---------------------------------------------------------------------------
# visual_evidence_captured — P1.4 functional implementation
# ---------------------------------------------------------------------------
#
# Looks up the EvidenceRecord for the current step in an
# `evidence_index` mapping (evidence_id -> EvidenceRecord),
# verifies the on-disk file's SHA-256 matches the record, and
# returns PASSED/FAILED accordingly. The evaluator signature is
# the same as the others; the executor passes the evidence_index
# via a task-local "current evidence view" that the executor
# populates around each step.


class _EvidenceView:
    """Per-step view of the evidence index.

    `set_evidence_index(idx)` is called by the executor right
    before evaluating expectations for a step. The evaluator
    reads the active index via `current_evidence_index()`.
    """
    _state: ContextVar[tuple[Mapping[str, "EvidenceRecord"], Path | None]] = (
        ContextVar("execution_evidence_view", default=({}, None))
    )

    @classmethod
    def set_evidence_index(
        cls, idx: Mapping[str, "EvidenceRecord"], trace_dir: Path | None = None,
    ) -> Token:
        return cls._state.set((idx, trace_dir))

    @classmethod
    def reset(cls, token: Token) -> None:
        cls._state.reset(token)

    @classmethod
    def current_evidence_index(cls):
        return cls._state.get()[0]

    @classmethod
    def current_trace_dir(cls) -> Path | None:
        return cls._state.get()[1]


def evaluate_visual_evidence_captured(
    step, expectation, observation, action_receipt,
):
    """FR-P1.4-08: visual_evidence_captured returns passed when the
    required screenshot exists with a matching digest; failed
    when missing or corrupt.

    The evaluator reads the per-step evidence_index from
    `_EvidenceView`. If the index is empty (no evidence has been
    recorded for this step), returns FAILED with diagnostic.
    """
    from trace.evidence import verify_evidence_on_disk

    idx = _EvidenceView.current_evidence_index()
    trace_dir = _EvidenceView.current_trace_dir()
    step_id = getattr(step, "step_id", "") if step is not None else ""
    matching = [ev for ev in idx.values() if ev.step_id == step_id] if idx else []
    if not matching:
        return _failed(
            "visual_evidence_captured",
            expected="present",
            actual=None,
            observation_id=None,
            duration_ms=0,
            diagnostic=f"no evidence record for step {step_id!r}",
        )
    expected_role = (
        expectation.value if isinstance(expectation.value, str) else "after"
    )
    role_matches = [ev for ev in matching if ev.role == expected_role]
    if not role_matches:
        first = matching[0]
        return _failed(
            "visual_evidence_captured",
            expected=expected_role,
            actual=first.role,
            observation_id=first.evidence_id,
            duration_ms=0,
            diagnostic=f"expected role {expected_role!r}, got {first.role!r}",
        )
    if trace_dir is None:
        return _failed(
            "visual_evidence_captured",
            expected=expected_role,
            actual=expected_role,
            observation_id=role_matches[0].evidence_id,
            duration_ms=0,
            diagnostic="evidence trace directory is unavailable",
        )
    for ev in role_matches:
        if verify_evidence_on_disk(ev, trace_dir):
            return _passed(
                "visual_evidence_captured",
                expected=expected_role,
                actual=ev.role,
                observation_id=ev.evidence_id,
                duration_ms=0,
                diagnostic="",
            )
    first = role_matches[0]
    return _failed(
        "visual_evidence_captured",
        expected=expected_role,
        actual=first.role,
        observation_id=first.evidence_id,
        duration_ms=0,
        diagnostic="evidence file is missing or has an invalid digest",
    )


# ---------------------------------------------------------------------------
# region_query — container-row / multi-component assertion (06 design §4–§5)
# ---------------------------------------------------------------------------
#
# The evaluator owns the observation window: its monotonic deadline starts
# when it is called (i.e. after the bound action completed — wizard/preview
# time never counts), samples the container every REGION_SAMPLE_INTERVAL
# seconds through the wired row provider, and stops at the first sample whose
# rows satisfy the whole condition tree (first_match).  ``absent_full_period``
# is the opposite completion rule: it may only pass at the deadline, and only
# when every sample was readable and none matched.
#
# Content time is recomputed per sample against an aware wall clock: the
# accepted window is always [C−recentSeconds, C] with C = this sample's now;
# future timestamps are rejected and unparseable / timezone-unknown values are
# undetermined — never False.
#
# Provider wiring: the executor exposes whatever its observation provider can
# serve through `_RegionRowProviderView` (set per step, like the evidence
# view).  Without a provider the evaluator falls back to ONE sample read from
# the current observation snapshot: first_match still converges via the
# executor's own post-action polling, while absent_full_period honestly
# reports `region_query_undetermined` because a single snapshot can never
# prove "never matched during the whole period".


REGION_SAMPLE_INTERVAL = 0.5


class _RegionRowProviderView:
    """Per-step view of the container-row provider.

    The executor sets it right before the ASSERTING phase from its
    observation provider (duck-typed: `query_container_rows(match, fields)`
    and optionally `read_component_values(components)`); tests wire stubs the
    same way. Purely in-process wiring — no payload extension.
    """

    _state: ContextVar[Any] = ContextVar("region_row_provider", default=None)

    @classmethod
    def set(cls, provider: Any) -> Token:
        return cls._state.set(provider)

    @classmethod
    def reset(cls, token: Token) -> None:
        cls._state.reset(token)

    @classmethod
    def current(cls) -> Any:
        return cls._state.get()


class _RegionClock:
    """The timezone-aware clock a region query samples against.

    Defaults to real UTC-now; tests substitute a fixed aware clock.  Content
    time is parsed against this clock's timezone rules — a naive recorded
    timestamp stays undetermined, it is never assumed local.
    """

    _now: ContextVar[Callable[[], "datetime"] | None] = ContextVar(
        "region_query_clock", default=None,
    )

    @classmethod
    def set(cls, now: Callable[[], "datetime"] | None) -> Token:
        return cls._now.set(now)

    @classmethod
    def reset(cls, token: Token) -> None:
        cls._now.reset(token)

    @classmethod
    def now(cls) -> "datetime":
        from datetime import datetime as _dt

        factory = cls._now.get()
        if factory is not None:
            return factory()
        return _dt.now(tz=_dt.timezone.utc)


def region_clock(factory: Callable[[], "datetime"] | None):
    """Context-manager form of ``_RegionClock.set`` (tests substitute a clock)."""
    import contextlib

    @contextlib.contextmanager
    def scope():
        token = _RegionClock.set(factory)
        try:
            yield factory
        finally:
            _RegionClock.reset(token)

    return scope()


def _region_leaf_fields(conditions: Any) -> list[str]:
    """Fields the row provider must read for this condition tree."""
    fields: set[str] = set()

    def walk(node: Any) -> None:
        if not isinstance(node, Mapping):
            return
        if "children" in node:
            for child in node.get("children") or ():
                walk(child)
            return
        field = node.get("field")
        if isinstance(field, str) and field != "rows":
            fields.add(field)

    walk(conditions)
    return sorted(fields)


def _control_matches_identity(control: Mapping[str, Any], identity: Mapping[str, Any]) -> bool:
    def first_non_empty(source: Mapping[str, Any], *keys: str) -> Any:
        for key in keys:
            if source.get(key) not in (None, ""):
                return source.get(key)
        return None

    expected_automation = identity.get("automation_id") or identity.get("automationId")
    expected_identifier = identity.get("identifier")
    expected_type = identity.get("control_type") or identity.get("controlType")
    expected_text = identity.get("text") or identity.get("name")
    if expected_automation:
        return str(first_non_empty(control, "automation_id", "automationId")) == str(expected_automation)
    if expected_identifier:
        return str(first_non_empty(control, "identifier")) == str(expected_identifier)
    if expected_type and expected_text:
        return (
            str(first_non_empty(control, "control_type", "controlType", "role")) == str(expected_type)
            and str(first_non_empty(control, "text", "name", "title")) == str(expected_text)
        )
    return False


def _ancestor_matches_identity(ancestor: Any, identity: Mapping[str, Any]) -> bool:
    if not isinstance(ancestor, Mapping):
        return False
    flat: dict[str, Any] = {}
    for key, value in ancestor.items():
        flat[key] = value
        flat[re.sub(r"(?<!^)(?=[A-Z])", "_", str(key)).lower()] = value
    return _control_matches_identity(flat, identity)


def _row_from_control(control: Mapping[str, Any], index: int) -> dict[str, Any]:
    def text_of(*keys: str) -> Any:
        for key in keys:
            value = control.get(key)
            if value not in (None, ""):
                return value
        return None

    row: dict[str, Any] = {
        "rowIndex": index,
        "name": text_of("name", "text", "title"),
        "text": text_of("text", "name", "title"),
        "value": text_of("value"),
        "checked": control.get("checked"),
        "enabled": control.get("enabled"),
        "time": text_of("time", "text", "value"),
        "snapshotId": control.get("target_id") or control.get("targetId"),
    }
    return row


def _rows_from_observation(
    observation: Any, match: Mapping[str, Any], fields: list[str],
) -> dict[str, Any]:
    """Single-sample fallback: read container rows from the current snapshot.

    Rows are the controls whose nearest recorded ancestor is the container
    (the observation reports ancestry nearest-first).  This is an
    approximation of one UIA walk — the authoritative sampling path is the
    wired provider; this fallback exists so the executor's own post-action
    polling keeps giving first_match fresh snapshots until that wiring lands.
    """
    controls: Any = None
    if isinstance(observation, Mapping):
        controls = observation.get("controls")
    else:
        controls = getattr(observation, "controls", None)
    if not isinstance(controls, (list, tuple)):
        return {"ok": False, "error": "observation_controls_unavailable"}
    rows: list[dict[str, Any]] = []
    for control in controls:
        if not isinstance(control, Mapping):
            continue
        if _control_matches_identity(control, match):
            continue  # the container itself is not one of its rows
        ancestry = control.get("ancestry") or []
        if not ancestry:
            continue
        nearest = ancestry[0]
        if _ancestor_matches_identity(nearest, match):
            rows.append(_row_from_control(control, len(rows)))
    return {"ok": True, "rows": rows, "sampling": "single_observation_snapshot"}


def _virtual_component_row(values: Any) -> dict[str, Any]:
    """One logical row combining each fixed component's current value."""
    row: dict[str, Any] = {"rowIndex": 0}
    if isinstance(values, Mapping) and not isinstance(values.get("values"), (list, tuple)):
        row.update({key: value for key, value in values.items() if key != "rowIndex"})
        return row
    items = values.get("values") if isinstance(values, Mapping) else values
    component_values: dict[str, Any] = {}
    for item in items or ():
        if isinstance(item, Mapping) and item.get("field"):
            row[str(item["field"])] = item.get("value")
            # P2f：按稳定键入 componentValues（同 field 不同身份不覆盖）；
            # 无 key 旧载荷=不产键通道（兼容）。
            if item.get("key"):
                component_values[str(item["key"])] = item.get("value")
    if component_values:
        row["componentValues"] = component_values
    return row


def _component_values_from_observation(
    observation: Any, components: list[Mapping[str, Any]],
) -> dict[str, Any]:
    controls: Any = None
    if isinstance(observation, Mapping):
        controls = observation.get("controls")
    else:
        controls = getattr(observation, "controls", None)
    if not isinstance(controls, (list, tuple)):
        return {"ok": False, "error": "observation_controls_unavailable"}
    values: list[dict[str, Any]] = []
    for component in components:
        identity = component.get("identity") or {}
        hits = [c for c in controls if isinstance(c, Mapping)
                and _control_matches_identity(c, identity)]
        if len(hits) != 1:
            entry = {"field": component.get("field"), "value": None,
                     "reason": "component_not_uniquely_found"}
            if component.get("key"):
                entry["key"] = component["key"]
            values.append(entry)
            continue
        entry: dict[str, Any] = {
            "field": component.get("field"),
            "value": _row_from_control(hits[0], 0).get(str(component.get("field"))),
        }
        # P2f：稳定键随采样值回传（身份锚定评估链路）。
        if component.get("key"):
            entry["key"] = component["key"]
        values.append(entry)
    return {"ok": True, "values": values, "sampling": "single_observation_snapshot"}


def _compact_sample(outcome: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "result": outcome.get("result"),
        "rowCount": outcome.get("rowCount"),
        "matchCount": outcome.get("matchCount"),
        "unreadable": outcome.get("unreadable"),
    }


def evaluate_region_query(
    step: Any,
    expectation: Any,
    observation: Any,
    action_receipt: ActionReceipt | None,
) -> ExpectationResult:
    """`region_query` evaluator: monotonic observation window + row conjunction."""
    from target.recording.region_assertion import (
        RegionDraftError, evaluate_region_conditions,
    )

    raw = expectation.value
    if not isinstance(raw, dict):
        return _failed("region_query", raw, None, _observation_id(observation), 0,
                       diagnostic="value must be {match, target, quantifier, conditions, contentTime, observation}")
    match = raw.get("match") or {}
    conditions = raw.get("conditions") or {}
    target = raw.get("target") or {}
    mode = target.get("mode", "container_rows")
    quantifier = raw.get("quantifier") or "any"
    content_time = raw.get("contentTime") or {}
    observation_spec = raw.get("observation") or {}
    recent_seconds = float(content_time.get("recentSeconds", 5.0) or 5.0)
    absent_mode = observation_spec.get("mode") == "absent_full_period"
    deadline = float(expectation.timeout_seconds or 60.0)
    components = list(target.get("components") or ())
    fields = _region_leaf_fields(conditions)
    provider = _RegionRowProviderView.current()

    def result_of(status: StepStatus, actual: Any, diagnostic: str,
                  duration_ms: int) -> ExpectationResult:
        return ExpectationResult(
            expectation_type="region_query", status=status,
            expected={
                "quantifier": quantifier, "mode": mode,
                "observation": observation_spec.get("mode", "first_match"),
            },
            actual=actual,
            observation_id=_observation_id(observation),
            duration_ms=duration_ms,
            diagnostic=diagnostic,
        )

    started = _time.monotonic()

    def sample_once() -> tuple[dict[str, Any], str]:
        """One sample's condition outcome + where its rows came from."""
        if mode == "fixed_components":
            if provider is not None and callable(getattr(provider, "read_component_values", None)):
                payload = provider.read_component_values(components)
                sampling = "provider"
            else:
                payload = _component_values_from_observation(observation, components)
                sampling = "single_observation_snapshot"
            # A fixed-component sample is one logical row.  The shared
            # evaluator accepts a sequence of rows (or an error payload),
            # not a bare row mapping.  Preserve an unreadable provider result
            # instead of turning it into an apparently readable row.
            if isinstance(payload, Mapping) and payload.get("ok") is False:
                rows = payload
            else:
                rows = [_virtual_component_row(payload)]
        elif provider is not None and callable(getattr(provider, "query_container_rows", None)):
            rows = provider.query_container_rows(match, fields)
            sampling = "provider"
        else:
            rows = _rows_from_observation(observation, match, fields)
            sampling = "single_observation_snapshot"
        now = _RegionClock.now()
        outcome = evaluate_region_conditions(
            conditions, rows, now, recent_seconds, quantifier=quantifier,
        )
        return outcome, sampling

    # No wired provider: one sample per call.  The executor's own post-action
    # polling re-runs this evaluator against refreshed snapshots until the
    # declared deadline, which is what gives first_match its convergence
    # without a second timer.  absence can never be proven from one snapshot.
    if provider is None or not (
        callable(getattr(provider, "query_container_rows", None))
        or callable(getattr(provider, "read_component_values", None))
    ):
        try:
            outcome, sampling = sample_once()
        except RegionDraftError as exc:
            return result_of(StepStatus.FAILED, None,
                             f"region_query_invalid: {exc}", 0)
        except Exception as exc:  # noqa: BLE001 - a raising provider is undetermined
            return result_of(
                StepStatus.FAILED,
                {"sampling": "provider", "error": str(exc)},
                "region_query_undetermined", 0,
            )
        evidence = {"sampling": sampling, "samples": [dict(outcome)],
                    "singleSample": True}
        if outcome.get("result") == "pass":
            if absent_mode:
                evidence["reason"] = "matched_during_absent_window"
                return result_of(StepStatus.FAILED, evidence,
                                 "region_query_matched_during_absent_window", 0)
            return result_of(StepStatus.PASSED, evidence, "", 0)
        if absent_mode:
            evidence["reason"] = "single_snapshot_cannot_prove_full_period_absence"
        elif outcome.get("result") == "fail" and not outcome.get("unreadable"):
            # A readable sample without a match is a real "not yet" — leave the
            # outcome to the executor's polling instead of faking a final pass.
            evidence["reason"] = "not_matched_in_this_sample"
        else:
            evidence["reason"] = "sample_undetermined"
        return result_of(StepStatus.FAILED, evidence, "region_query_undetermined", 0)

    # Provider wired: this evaluator owns the whole observation window.
    full_samples: list[dict[str, Any]] = []
    compact_samples: list[dict[str, Any]] = []
    deadline_at = started + deadline
    while True:
        try:
            outcome, sampling = sample_once()
        except Exception as exc:  # noqa: BLE001 - a raising provider is undetermined
            outcome = {"result": "undetermined", "rowCount": 0, "matchCount": 0,
                       "unreadable": f"{type(exc).__name__}: {exc}"}
            sampling = "provider"
        if not compact_samples or not full_samples:
            full_samples.append(dict(outcome))
        elif outcome.get("result") == "pass" or _time.monotonic() >= deadline_at:
            full_samples.append(dict(outcome))
        compact_samples.append({**_compact_sample(outcome),
                                "nowWall": outcome.get("nowWall"),
                                "sampling": sampling})
        if outcome.get("result") == "pass":
            if absent_mode:
                return result_of(
                    StepStatus.FAILED,
                    {"sampling": sampling, "samples": full_samples,
                     "compactSamples": compact_samples,
                     "reason": "matched_during_absent_window"},
                    "region_query_matched_during_absent_window", 0,
                )
            return result_of(
                StepStatus.PASSED,
                {"sampling": sampling, "samples": full_samples,
                 "compactSamples": compact_samples},
                "", 0,
            )
        remaining = deadline_at - _time.monotonic()
        if remaining <= 0:
            break
        _time.sleep(min(REGION_SAMPLE_INTERVAL, remaining))

    any_undetermined = (
        any(sample.get("result") == "undetermined" for sample in compact_samples)
        or any(sample.get("unreadable") for sample in compact_samples)
    )
    if absent_mode and not any_undetermined:
        # 到期且整个观察期从未匹配（每个采样都可判定）→ 通过。
        return result_of(
            StepStatus.PASSED,
            {"sampling": "provider", "samples": full_samples,
             "compactSamples": compact_samples},
            "", 0,
        )
    diagnostic = ("region_query_undetermined" if any_undetermined
                  else "region_query_not_satisfied")
    return result_of(
        StepStatus.FAILED,
        {"sampling": "provider", "samples": full_samples,
         "compactSamples": compact_samples,
         "reason": "undetermined_samples" if any_undetermined
         else ("absence_unprovable" if absent_mode else "no_match_before_deadline")},
        diagnostic, 0,
    )


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


EVALUATORS_NOT_AVAILABLE: frozenset[str] = frozenset()


EXPECTATION_REGISTRY: dict[str, Callable[..., ExpectationResult]] = {
    "action_ok": evaluate_action_ok,
    "window_open": evaluate_window_open,
    "window_closed": evaluate_window_closed,
    "active_window_owner": evaluate_active_window_owner,
    "control_exists": evaluate_control_exists,
    "control_absent": evaluate_control_absent,
    "control_text_equals": evaluate_control_text_equals,
    "control_text_contains": evaluate_control_text_contains,
    "control_value_equals": evaluate_control_value_equals,
    "control_checked_equals": evaluate_control_checked_equals,
    "control_enabled_equals": evaluate_control_enabled_equals,
    "window_text_contains": evaluate_window_text_contains,
    "control_text_contains_time": evaluate_control_text_contains_time,
    "window_text_contains_time": evaluate_window_text_contains_time,
    "control_text_time_range": evaluate_control_text_time_range,
    "window_text_time_range": evaluate_window_text_time_range,
    "visual_evidence_captured": evaluate_visual_evidence_captured,
    "region_query": evaluate_region_query,
}


class ExpectationNotAvailable(Exception):
    """A declared expectation is not wired into this runtime."""

    def __init__(self, expectation_type: str) -> None:
        super().__init__(
            f"expectation {expectation_type!r} is not available in this "
            f"checkpoint; declared but not wired."
        )
        self.expectation_type = expectation_type


__all__ = [
    "EXPECTATION_REGISTRY",
    "EVALUATORS_NOT_AVAILABLE",
    "ExpectationNotAvailable",
    "evaluate_action_ok",
    "evaluate_control_text_contains_time",
    "evaluate_window_text_contains_time",
    "render_time_pattern",
    "evaluate_window_open",
    "evaluate_window_closed",
    "evaluate_active_window_owner",
    "evaluate_control_exists",
    "evaluate_control_absent",
    "evaluate_control_text_equals",
    "evaluate_control_text_contains",
    "evaluate_control_value_equals",
    "evaluate_control_checked_equals",
    "evaluate_control_enabled_equals",
    "evaluate_window_text_contains",
    "evaluate_region_query",
    "set_region_row_provider",
    "region_row_provider",
]


def set_region_row_provider(provider: Any) -> Token:
    """Wire the container-row provider for region_query evaluation.

    The executor calls this per step around the ASSERTING phase; tests may
    set a stub for the duration of one evaluator call. Returns the token to
    reset with.
    """
    return _RegionRowProviderView.set(provider)


def region_row_provider(provider: Any):
    """Context-manager form of :func:`set_region_row_provider` (tests/wiring)."""
    import contextlib

    @contextlib.contextmanager
    def scope():
        token = _RegionRowProviderView.set(provider)
        try:
            yield provider
        finally:
            _RegionRowProviderView.reset(token)

    return scope()
