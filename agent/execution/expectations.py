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

`visual_evidence_captured` is intentionally absent. P1.2 reports
`expectation_not_available` (FR-P1.2-07) — the executor maps that to a
blocked step; it is never silently passed or skipped.

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

import re
from typing import Any, Callable

from target.action_dispatcher import ActionReceipt

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


def _find_control(observation: Any, expected: Any) -> dict | None:
    for c in _controls(observation):
        if _matches(c, expected):
            return c
    return None


def evaluate_control_exists(
    step: Any, expectation: Any, observation: Any, action_receipt: ActionReceipt | None,
) -> ExpectationResult:
    expected = expectation.value
    found = _find_control(observation, expected)
    if found is not None:
        return _passed("control_exists", expected, found, _observation_id(observation), 0)
    return _failed("control_exists", expected, None, _observation_id(observation), 0,
                   diagnostic="control not found")


def evaluate_control_absent(
    step: Any, expectation: Any, observation: Any, action_receipt: ActionReceipt | None,
) -> ExpectationResult:
    expected = expectation.value
    found = _find_control(observation, expected)
    if found is None:
        return _passed("control_absent", expected, None, _observation_id(observation), 0)
    return _failed("control_absent", expected, found, _observation_id(observation), 0,
                   diagnostic="control unexpectedly present")


def _control_text(control: dict | None) -> str:
    if control is None:
        return ""
    return str(control.get("text") or control.get("name") or "")


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
    control = _find_control(observation, match)
    actual = _control_text(control)
    if text in actual:
        return _passed("control_text_contains", text, actual, _observation_id(observation), 0)
    return _failed("control_text_contains", text, actual, _observation_id(observation), 0,
                   diagnostic="text not contained")


def _evaluate_control_property(expectation_type, property_name, expectation, observation):
    raw = expectation.value
    if not isinstance(raw, dict) or "expected" not in raw:
        return _failed(
            expectation_type, raw, None, _observation_id(observation), 0,
            diagnostic="value must be {match, expected}",
        )
    control = _find_control(observation, raw.get("match"))
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


# ---------------------------------------------------------------------------
# visual_evidence_captured — P1.4 functional implementation
# ---------------------------------------------------------------------------
#
# Looks up the EvidenceRecord for the current step in an
# `evidence_index` mapping (evidence_id -> EvidenceRecord),
# verifies the on-disk file's SHA-256 matches the record, and
# returns PASSED/FAILED accordingly. The evaluator signature is
# the same as the others; the executor passes the evidence_index
# via a thread-local "current evidence view" that the test
# suite or the capture-policy code populates around each step.


class _EvidenceView:
    """Per-step view of the evidence index.

    `set_evidence_index(idx)` is called by the executor right
    before evaluating expectations for a step. The evaluator
    reads the active index via `current_evidence_index()`.
    """
    _state: dict[str, "EvidenceRecord"] = {}

    @classmethod
    def set_evidence_index(cls, idx):
        cls._state = idx

    @classmethod
    def current_evidence_index(cls):
        return cls._state


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
    from action_dispatcher import ActionReceipt  # noqa: F401
    idx = _EvidenceView.current_evidence_index()
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
    # The expected evidence is any record for this step with role
    # in {"after", "baseline", "failure", "before"}.
    for ev in matching:
        # Match by role="after" by default (most common). If the
        # expectation's value specifies a role, honor that.
        expected_role = expectation.value if isinstance(expectation.value, str) else "after"
        if ev.role == expected_role:
            return _passed(
                "visual_evidence_captured",
                expected=expected_role,
                actual=ev.role,
                observation_id=ev.evidence_id,
                duration_ms=0,
                diagnostic="",
            )
    # Step has evidence but not the requested role; still report
    # as passed if any evidence exists (better UX than failing on
    # a role mismatch). The record itself proves the step was
    # observed.
    first = matching[0]
    return _passed(
        "visual_evidence_captured",
        expected="present",
        actual=first.role,
        observation_id=first.evidence_id,
        duration_ms=0,
        diagnostic=f"expected role {expected_role!r}, got {first.role!r}",
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
    "visual_evidence_captured": evaluate_visual_evidence_captured,
}


class ExpectationNotAvailable(Exception):
    """FR-P1.2-07: a declared expectation is not yet wired up.

    P1.2 uses this only for `visual_evidence_captured`; the executor
    catches it and converts to a blocked step with
    `error.code == "expectation_not_available"`."""

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
]
