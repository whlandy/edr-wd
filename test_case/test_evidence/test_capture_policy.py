"""P1.4 acceptance gate — capture policy matrix (architecture §14.2).

One test per row of the §14.2 table. The policy resolver returns a
CapturePlan; tests pin the roles + mandatory flag.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


_REPO = Path(__file__).resolve().parents[2]
for p in (str(_REPO / "target"), str(_REPO / "agent")):
    if p not in sys.path:
        sys.path.insert(0, p)


from trace import (  # noqa: E402
    CaptureContext,
    CapturePlan,
    resolve_capture_plan,
)


# ---------------------------------------------------------------------------
# §14.2 table — one test per row
# ---------------------------------------------------------------------------


def test_case_start_baseline_mandatory():
    plan = resolve_capture_plan(CaptureContext(is_case_start=True))
    assert "baseline" in plan.roles
    assert plan.mandatory is True


def test_observation_only_success_no_evidence_unless_visual():
    """Observation-only step on success with no visual expectation:
    no captures."""
    plan = resolve_capture_plan(CaptureContext(is_observation_only=True))
    assert plan.roles == ()


def test_observation_only_visual_expectation_captures_after():
    plan = resolve_capture_plan(CaptureContext(
        is_observation_only=True,
        expectation_requires_visual=True,
    ))
    assert "after" in plan.roles


def test_observation_only_failure_mandatory_failure():
    plan = resolve_capture_plan(CaptureContext(
        is_observation_only=True,
        outcome_failed=True,
    ))
    assert "failure" in plan.roles
    assert plan.mandatory is True


def test_stable_mutation_captures_after():
    plan = resolve_capture_plan(CaptureContext())
    assert "after" in plan.roles
    assert plan.mandatory is False


def test_expected_transition_captures_before_and_after():
    plan = resolve_capture_plan(CaptureContext(transition_expected=True))
    assert set(plan.roles) == {"before", "after"}


def test_checkpointed_action_captures_before_and_after():
    plan = resolve_capture_plan(CaptureContext(is_checkpointed=True))
    assert set(plan.roles) == {"before", "after"}


def test_irreversible_action_captures_before_and_after():
    plan = resolve_capture_plan(CaptureContext(is_irreversible=True))
    assert set(plan.roles) == {"before", "after"}


def test_blocked_step_mandatory_failure():
    plan = resolve_capture_plan(CaptureContext(outcome_blocked=True))
    assert "failure" in plan.roles
    assert plan.mandatory is True


def test_failed_step_mandatory_failure():
    plan = resolve_capture_plan(CaptureContext(outcome_failed=True))
    assert "failure" in plan.roles
    assert plan.mandatory is True


# ---------------------------------------------------------------------------
# Test cannot weaken mandatory policy
# ---------------------------------------------------------------------------


def test_mandatory_failure_persists_even_with_baseline():
    """case-start + failure → both baseline and failure fire."""
    plan = resolve_capture_plan(CaptureContext(
        is_case_start=True,
        outcome_failed=True,
    ))
    assert "baseline" in plan.roles
    assert "failure" in plan.roles
    assert plan.mandatory is True


def test_transition_failure_includes_failure_image():
    plan = resolve_capture_plan(CaptureContext(
        transition_expected=True,
        outcome_failed=True,
    ))
    assert set(plan.roles) == {"before", "after", "failure"}
    assert plan.mandatory is True


# ---------------------------------------------------------------------------
# Contract
# ---------------------------------------------------------------------------


def test_resolve_returns_capture_plan():
    plan = resolve_capture_plan(CaptureContext())
    assert isinstance(plan, CapturePlan)


def test_empty_context_yields_empty_or_after_only():
    plan = resolve_capture_plan(CaptureContext())
    # Stable same-page mutation captures "after".
    assert plan.roles == ("after",) or plan.roles == ()