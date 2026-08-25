"""P1.2 acceptance gate — AtomicExecutor behaviour.

Covers FR-P1.2-01..09 via unit-level scenarios against the fake
backend / fake observation provider.
"""

from __future__ import annotations

import dataclasses
import sys
from pathlib import Path

import pytest


_REPO = Path(__file__).resolve().parents[2]
for p in (str(_REPO / "target"), str(_REPO / "agent"), str(_REPO / "test_case")):
    if p not in sys.path:
        sys.path.insert(0, p)


from action_dispatcher import ActionReceipt  # noqa: E402
from protocol_models import (  # noqa: E402
    AtomicTestStep,
    Expectation,
    TargetRef,
    TestCase,
)

from execution import (  # noqa: E402
    AtomicExecutor,
    BackendUnavailable,
    CaseOutcome,
    ExecutorConfig,
    StepStatus,
)
from fake_target import FakeBackend, FakeObservationProvider  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def backend() -> FakeBackend:
    return FakeBackend()


@pytest.fixture
def obs_provider() -> FakeObservationProvider:
    return FakeObservationProvider()


@pytest.fixture
def executor(backend, obs_provider) -> AtomicExecutor:
    return AtomicExecutor(
        dispatch=backend.dispatch,
        observation_provider=obs_provider,
    )


def _target(snapshot_id: str = "snap-fake", target_id: str = "tgt-1"):
    return TargetRef(
        snapshot_id=snapshot_id,
        target_id=target_id,
        selector_hint={"text": "OK"},
    )


def _case(*steps, case_id="TC-X"):
    return TestCase(
        case_id=case_id,
        title="fixture",
        steps=tuple(steps),
        timeout_seconds=60,
    )


def _step(step_id, *, action_id="gui.click", expectations=(), target_ref=None, on_error="abort", required=True):
    return AtomicTestStep(
        step_id=step_id,
        step_no=int(step_id.lstrip("S")),
        action_id=action_id,
        args={"x": 1, "y": 2},
        target_ref=target_ref,
        expectations=tuple(expectations),
        on_error=on_error,
        required=required,
    )


# ---------------------------------------------------------------------------
# Acceptance #1: three steps pass with a stub backend
# ---------------------------------------------------------------------------


def test_three_steps_pass(executor, backend, obs_provider):
    obs_provider.set_snapshot({
        "windows": [{"process": "app", "title": "OK"}],
        "controls": [{"automation_id": "btn", "text": "OK"}],
    })
    case = _case(
        _step("S001", expectations=[Expectation(type="action_ok")]),
        _step("S002", action_id="gui.type_text",
              expectations=[Expectation(type="action_ok")]),
        _step("S003", expectations=[
            Expectation(type="window_text_contains", value="OK"),
        ]),
    )
    result = executor.run_case(case)
    assert result.outcome is CaseOutcome.PASSED
    assert [sr.status for sr in result.step_results] == [
        StepStatus.PASSED, StepStatus.PASSED, StepStatus.PASSED,
    ]
    # Backend was called exactly three times.
    assert sum(backend.call_count.values()) == 3


# ---------------------------------------------------------------------------
# Acceptance #2: action_ok but expectation failed -> step is failed
# ---------------------------------------------------------------------------


def test_action_ok_but_expectation_failed(executor, backend, obs_provider):
    # Snapshot lacks the expected text.
    obs_provider.set_snapshot({"windows": [], "controls": []})
    case = _case(
        _step("S001", expectations=[
            Expectation(type="action_ok"),
            Expectation(type="window_text_contains", value="Welcome"),
        ]),
    )
    result = executor.run_case(case)
    assert result.step_results[0].status is StepStatus.FAILED
    assert result.outcome is CaseOutcome.FAILED
    # The action still ran.
    assert backend.call_count.get("gui.click") == 1


# ---------------------------------------------------------------------------
# Acceptance #3: backend unavailable at start -> every step blocked
# ---------------------------------------------------------------------------


def test_backend_unavailable_blocks_every_step(obs_provider, backend):
    obs_provider.raise_on_refresh = True
    executor = AtomicExecutor(dispatch=backend.dispatch, observation_provider=obs_provider)
    case = _case(
        _step("S001"),
        _step("S002"),
        _step("S003"),
    )
    result = executor.run_case(case)
    assert result.outcome is CaseOutcome.BLOCKED
    assert [sr.status for sr in result.step_results] == [
        StepStatus.BLOCKED, StepStatus.BLOCKED, StepStatus.BLOCKED,
    ]
    assert all(sr.error and sr.error["code"] == "backend_unavailable"
               for sr in result.step_results)


# ---------------------------------------------------------------------------
# Acceptance #4: retry bound respected
# ---------------------------------------------------------------------------


def test_retry_bound_respected(backend, obs_provider):
    # First two attempts fail; third succeeds.
    backend.receipt_overrides["gui.click"] = {"ok": False, "result": {"error": "boom"}}
    counter = {"n": 0}

    real_dispatch = backend.dispatch

    def maybe_ok_dispatch(*, action_id, **kwargs):
        counter["n"] += 1
        # Fail attempts 1 and 2; succeed on attempt 3 (the final
        # retry).  Condition is evaluated BEFORE real_dispatch so the
        # override flip takes effect on the very next call.
        if counter["n"] >= 3:
            backend.force_ok = True
            backend.receipt_overrides.pop("gui.click", None)
        return real_dispatch(action_id=action_id, **kwargs)

    executor = AtomicExecutor(
        dispatch=maybe_ok_dispatch,
        observation_provider=obs_provider,
        config=ExecutorConfig(retry_max=2),
    )
    case = _case(
        _step("S001", on_error="retry", expectations=[Expectation(type="action_ok")]),
    )
    result = executor.run_case(case)
    # Initial + 2 retries = 3 calls; final call succeeds.
    assert counter["n"] == 3
    assert result.step_results[0].status is StepStatus.PASSED


def test_retry_exhausted_aborts(backend, obs_provider):
    backend.receipt_overrides["gui.click"] = {"ok": False, "result": {"error": "boom"}}
    executor = AtomicExecutor(
        dispatch=backend.dispatch,
        observation_provider=obs_provider,
        config=ExecutorConfig(retry_max=2),
    )
    case = _case(
        _step("S001", on_error="retry", expectations=[Expectation(type="action_ok")]),
        _step("S002"),
    )
    result = executor.run_case(case)
    # 1 initial + 2 retries = 3 attempts, all failed -> step FAILED,
    # then capture_and_abort-style default kicks in (retry policy:
    # retry exhausted -> abort remaining).
    assert result.step_results[0].status is StepStatus.FAILED
    assert result.step_results[1].status is StepStatus.SKIPPED
    assert result.aborted is True
    assert result.abort_reason == "retry_exhausted"


# ---------------------------------------------------------------------------
# Acceptance #5: capture_and_abort stops case, remaining skipped
# ---------------------------------------------------------------------------


def test_capture_and_abort_stops_case(backend, obs_provider):
    backend.receipt_overrides["gui.click"] = {"ok": False, "result": {"error": "boom"}}
    executor = AtomicExecutor(dispatch=backend.dispatch, observation_provider=obs_provider)
    case = _case(
        _step("S001", on_error="capture_and_abort",
              expectations=[Expectation(type="action_ok")]),
        _step("S002"),
        _step("S003"),
    )
    result = executor.run_case(case)
    statuses = [sr.status for sr in result.step_results]
    assert statuses[0] is StepStatus.FAILED
    assert statuses[1] is StepStatus.SKIPPED
    assert statuses[2] is StepStatus.SKIPPED
    assert result.aborted is True
    assert result.abort_reason == "capture_and_abort"
    # Case outcome is FAILED (a required step failed).
    assert result.outcome is CaseOutcome.FAILED


# ---------------------------------------------------------------------------
# Acceptance #6: step-results.json is atomic
# ---------------------------------------------------------------------------


def test_step_results_atomic_write(executor, backend, obs_provider, tmp_path):
    target = tmp_path / "step-results.json"
    case = _case(_step("S001"))
    executor.run_case(case, step_results_path=target)
    assert target.exists()
    # File is valid JSON and complete.
    import json
    payload = json.loads(target.read_text())
    assert payload["case_id"] == "TC-X"
    assert len(payload["step_results"]) == 1


# ---------------------------------------------------------------------------
# FR-P1.2-07: visual_evidence_captured blocks the step
# ---------------------------------------------------------------------------


def test_visual_evidence_evaluator_runs_when_no_evidence_recorded(executor, backend, obs_provider):
    case = _case(
        _step("S001", expectations=[
            Expectation(type="visual_evidence_captured"),
        ]),
    )
    result = executor.run_case(case)
    sr = result.step_results[0]
    # P1.4: visual_evidence_captured is now a real evaluator;
    # the step is no longer hard-blocked.
    assert sr.status is StepStatus.FAILED


# ---------------------------------------------------------------------------
# FR-P1.2-08: target_stale blocks the step
# ---------------------------------------------------------------------------


def test_target_stale_blocks_step(executor, backend, obs_provider):
    # Provider has a different latest snapshot than the step's.
    obs_provider.snapshot_id = "snap-latest"
    case = _case(
        _step("S001", target_ref=_target(snapshot_id="snap-stale", target_id="tgt-1")),
    )
    result = executor.run_case(case)
    sr = result.step_results[0]
    assert sr.status is StepStatus.BLOCKED
    assert sr.error["code"] == "target_stale"
    assert sr.error["step_snapshot_id"] == "snap-stale"
    assert sr.error["latest_snapshot_id"] == "snap-latest"


def test_target_fresh_does_not_block(executor, backend, obs_provider):
    obs_provider.snapshot_id = "snap-fresh"
    case = _case(
        _step("S001", target_ref=_target(snapshot_id="snap-fresh", target_id="tgt-1")),
    )
    result = executor.run_case(case)
    assert result.step_results[0].status is StepStatus.PASSED


# ---------------------------------------------------------------------------
# FR-P1.2-09: transition.expected=True skips pre-action observation optimisation
# ---------------------------------------------------------------------------


def test_transition_expected_skips_pre_action(executor, backend, obs_provider):
    from protocol_models import Transition
    obs_provider.snapshot_id = "snap-fresh"
    step = _step("S001", target_ref=_target(snapshot_id="snap-fresh", target_id="tgt-1"))
    step = AtomicTestStep(
        step_id=step.step_id,
        step_no=step.step_no,
        action_id=step.action_id,
        args=step.args,
        target_ref=step.target_ref,
        expectations=step.expectations,
        on_error=step.on_error,
        required=step.required,
        transition=Transition(expected=True, kind="page_navigation"),
    )
    case = _case(step)
    result = executor.run_case(case)
    # The step still runs (no checkpoint created, that's P2.1).
    assert result.step_results[0].status in (StepStatus.PASSED, StepStatus.FAILED)
    # StepResult records transition_expected=True.
    assert result.step_results[0].transition_expected is True


# ---------------------------------------------------------------------------
# FR-P1.2-01: never two mutations back-to-back without OBSERVING_AFTER
# ---------------------------------------------------------------------------


def test_no_back_to_back_mutations_without_observing(executor, backend, obs_provider):
    """Mutations go through EXECUTING -> OBSERVING_AFTER; the only
    state machine move from EXECUTING is OBSERVING_AFTER or ABORTED,
    never another EXECUTING. We assert that by reading the state
    machine directly (since dispatch is hidden)."""
    from execution import StepState, next_state

    # Only legal successor of EXECUTING is OBSERVING_AFTER or ABORTED.
    legal = {StepState.OBSERVING_AFTER, StepState.ABORTED}
    successors = [s for s in StepState
                  if (s.value, StepState.EXECUTING.value) not in {("aborted", "executing")}]
    # The above is a tautology; instead, inspect TRANSITIONS directly.
    from execution import TRANSITIONS
    assert TRANSITIONS[StepState.EXECUTING] == frozenset({
        StepState.OBSERVING_AFTER, StepState.ABORTED,
    })


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def test_writes_step_results_path_per_step(executor, backend, obs_provider, tmp_path):
    """Each step writes to disk, so partial progress is visible even
    if the process dies mid-case."""
    target = tmp_path / "step-results.json"
    case = _case(
        _step("S001"),
        _step("S002"),
        _step("S003"),
    )
    executor.run_case(case, step_results_path=target)
    import json
    payload = json.loads(target.read_text())
    assert len(payload["step_results"]) == 3


def test_skipped_steps_after_abort_carry_skip_reason(executor, backend, obs_provider):
    backend.receipt_overrides["gui.click"] = {"ok": False, "result": {"error": "boom"}}
    case = _case(
        _step("S001", on_error="abort",
              expectations=[Expectation(type="action_ok")]),
        _step("S002", expectations=[Expectation(type="action_ok")]),
        _step("S003", expectations=[Expectation(type="action_ok")]),
    )
    result = executor.run_case(case)
    assert result.step_results[1].status is StepStatus.SKIPPED
    assert result.step_results[1].error == {"code": "skipped_after_abort"}
    assert result.step_results[2].status is StepStatus.SKIPPED


def test_receipt_failure_propagates_even_without_action_ok_expectation(
    executor, backend, obs_provider,
):
    """Blocker 3: ActionReceipt.ok=False must surface as FAILED
    even when the step declares no expectations at all.

    An empty expectation list must not turn a backend failure into
    a passing step.
    """
    backend.receipt_overrides["gui.click"] = {"ok": False, "result": {"error": "boom"}}
    # No expectations declared on the step.
    case = _case(_step("S001"))
    result = executor.run_case(case)
    sr = result.step_results[0]
    assert sr.status is StepStatus.FAILED, (
        "backend failure must propagate even without action_ok expectation; "
        f"got {sr.status!r}"
    )
    assert result.outcome is CaseOutcome.FAILED


def test_receipt_failure_with_passing_expectation_is_still_failed(
    executor, backend, obs_provider,
):
    """If the backend fails but all expectations evaluate to
    PASSED, the step is FAILED (the receipt is the source of
    truth, expectations only add *more* failure conditions).
    """
    backend.receipt_overrides["gui.click"] = {"ok": False, "result": {"error": "boom"}}
    # observation has the expected text, so control_text_contains passes.
    obs_provider.set_snapshot({"controls": [{"text": "Welcome"}]})
    case = _case(_step("S001", expectations=[
        Expectation(type="control_text_contains",
                   value={"match": {"text": "Welcome"}, "text": "Welcome"}),
    ]))
    result = executor.run_case(case)
    sr = result.step_results[0]
    # Backend failure dominates; expectation pass cannot rescue it.
    assert sr.status is StepStatus.FAILED


def test_all_required_passed_case_is_passed(executor, backend, obs_provider):
    """All required steps PASSED -> case outcome PASSED.

    Pins architecture §9.4 case-outcome rule #4: otherwise -> passed.
    Regression target for the bug where `_step_required` returning
    True for every step made `_derive_case_outcome` collapse to
    SKIPPED.
    """
    case = _case(
        _step("S001", expectations=[Expectation(type="action_ok")]),
        _step("S002", action_id="gui.type_text",
              expectations=[Expectation(type="action_ok")]),
    )
    result = executor.run_case(case)
    assert all(sr.status is StepStatus.PASSED for sr in result.step_results)
    assert result.outcome is CaseOutcome.PASSED
    assert result.aborted is False


def test_optional_failure_does_not_fail_or_abort_case(executor, backend):
    backend.receipt_overrides["gui.click"] = {
        "ok": False, "result": {"error": "optional failure"},
    }
    case = _case(
        _step("S001", required=False),
        _step("S002", action_id="gui.type_text", required=True),
    )

    result = executor.run_case(case)

    assert [item.status for item in result.step_results] == [
        StepStatus.FAILED, StepStatus.PASSED,
    ]
    assert result.outcome is CaseOutcome.PASSED
    assert result.aborted is False


def test_abort_uses_current_index_for_equal_steps(executor, backend):
    backend.queue_success("gui.click")
    backend.queue_failure("gui.click")
    repeated = _step("S001")

    result = executor.run_case(_case(repeated, repeated))

    assert [item.status for item in result.step_results] == [
        StepStatus.PASSED, StepStatus.FAILED,
    ]
    assert result.aborted is True


def test_all_required_skipped_case_is_skipped(executor, backend, obs_provider):
    """All required steps SKIPPED -> case outcome SKIPPED.

    Pins architecture §9.4 case-outcome rule #3: all required
    skipped -> case skipped.

    Constructed by marking every step with on_error=abort and
    pre-arranging the backend to fail so the first step is FAILED
    and the rest SKIPPED — but with all steps non-required the
    case precedence rule applies to the (empty) required set,
    yielding SKIPPED.  We assert the precedence function directly
    because P1.2 has no mechanism to *skip* a required step
    without also recording a FAILED/BLOCKED status; the rule
    exists for future extension (e.g. profile-based skip in
    P2.x).
    """
    from execution.models import StepResult
    sr = StepResult(
        step_id="S1", step_no=1, status=StepStatus.SKIPPED,
        started_at="2026-01-01T00:00:00",
        ended_at="2026-01-01T00:00:00",
        duration_ms=0, action=None, expectation_results=[],
        error={"code": "skipped_after_abort"},
    )
    outcome = executor._derive_case_outcome([sr])
    assert outcome is CaseOutcome.SKIPPED, outcome

    # Three skipped required steps -> still SKIPPED.
    skipped_results = [dataclasses.replace(sr, step_id=f"S{i}") for i in range(3)]
    outcome = executor._derive_case_outcome(skipped_results)
    assert outcome is CaseOutcome.SKIPPED
