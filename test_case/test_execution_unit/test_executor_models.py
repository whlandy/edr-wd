"""P1.2 acceptance gate — executor models."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


_REPO = Path(__file__).resolve().parents[2]
for p in (str(_REPO / "target"), str(_REPO / "agent")):
    if p not in sys.path:
        sys.path.insert(0, p)


from execution import (  # noqa: E402
    CaseOutcome,
    OnErrorPolicy,
    STEP_STATUS_PRECEDENCE,
    StepStatus,
    coerce_on_error,
    worst_status,
)


def test_step_status_precedence_order():
    assert STEP_STATUS_PRECEDENCE[0] is StepStatus.FAILED
    assert STEP_STATUS_PRECEDENCE[-1] is StepStatus.PASSED


def test_worst_status_picks_higher_precedence():
    assert worst_status(StepStatus.PASSED, StepStatus.FAILED) is StepStatus.FAILED
    assert worst_status(StepStatus.BLOCKED, StepStatus.SKIPPED) is StepStatus.BLOCKED
    assert worst_status(StepStatus.SKIPPED, StepStatus.SKIPPED) is StepStatus.SKIPPED
    assert worst_status(
        StepStatus.FAILED, StepStatus.BLOCKED, StepStatus.PASSED,
    ) is StepStatus.FAILED


def test_worst_status_single_arg_returns_that_status():
    assert worst_status(StepStatus.PASSED) is StepStatus.PASSED
    assert worst_status(StepStatus.FAILED) is StepStatus.FAILED
    assert worst_status(StepStatus.SKIPPED) is StepStatus.SKIPPED


def test_coerce_on_error_lowercases_strings():
    assert coerce_on_error("abort") is OnErrorPolicy.ABORT
    assert coerce_on_error("RETRY") is OnErrorPolicy.RETRY
    assert coerce_on_error("capture_and_abort") is OnErrorPolicy.CAPTURE_AND_ABORT


def test_coerce_on_error_passes_enum_through():
    assert coerce_on_error(OnErrorPolicy.ABORT) is OnErrorPolicy.ABORT


def test_coerce_on_error_rejects_unknown_string():
    with pytest.raises(ValueError):
        coerce_on_error("nope")


def test_coerce_on_error_rejects_non_string():
    with pytest.raises(TypeError):
        coerce_on_error(42)


def test_case_outcome_string_values_match_doc():
    assert CaseOutcome.PASSED.value == "passed"
    assert CaseOutcome.FAILED.value == "failed"
    assert CaseOutcome.BLOCKED.value == "blocked"
    assert CaseOutcome.SKIPPED.value == "skipped"


def test_step_status_string_values_match_doc():
    assert StepStatus.PASSED.value == "passed"
    assert StepStatus.SKIPPED.value == "skipped"


def test_on_error_policy_list_matches_spec():
    """P1.2 ships exactly these 4 policies."""
    assert {p.value for p in OnErrorPolicy} == {
        "abort",
        "retry",
        "capture_and_abort",
        "reobserve_replan",
    }