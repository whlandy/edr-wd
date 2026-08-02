"""P1.2 acceptance gate — integration tests against the executor's
fake target harness.

The integration tests mirror the acceptance criteria in
`docs/requirements/P1-execution-evidence-mvp.md` §"Acceptance
Criteria" 1..6:

    1. A fake MCP target executes a 3-step case end to end; step
       statuses are `passed`.
    2. `action_ok=true` plus a failing `window_text_contains`
       produces a `failed` step (not `passed`).
    3. Backend unavailable at start produces `blocked` for every step.
    4. Retry policy retries exactly N times on `failed`, then stops.
    5. `capture_and_abort` stops the case after the failing step and
       leaves remaining steps in `skipped`.
    6. `step-results.json` overwrites atomically — readers always see
       a complete file or the prior complete file.

Plus a couple of `Required Tests` items that did not fit naturally in
the unit-test file.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest


_REPO = Path(__file__).resolve().parents[2]
for p in (str(_REPO / "target"), str(_REPO / "agent"), str(_REPO / "test_case")):
    if p not in sys.path:
        sys.path.insert(0, p)


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
    write_atomic,
)
from fake_target import FakeBackend, FakeObservationProvider  # noqa: E402


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


def _target():
    return TargetRef(
        snapshot_id="snap-fake", target_id="tgt-1",
        selector_hint={"text": "OK"},
    )


def _step(step_id, **kwargs):
    """Build an AtomicTestStep with sane defaults."""
    action_id = kwargs.pop("action_id", "gui.click")
    expectations = kwargs.pop("expectations", (Expectation(type="action_ok"),))
    on_error = kwargs.pop("on_error", "abort")
    target_ref = kwargs.pop("target_ref", None)
    return AtomicTestStep(
        step_id=step_id,
        step_no=int(step_id.lstrip("S")),
        action_id=action_id,
        args={"x": 1, "y": 2},
        target_ref=target_ref,
        expectations=expectations,
        on_error=on_error,
        required=kwargs.pop("required", True),
    )


# ---------------------------------------------------------------------------
# Acceptance #1 — 3-step happy path
# ---------------------------------------------------------------------------


def test_acceptance_three_step_happy_path(executor, backend, obs_provider):
    obs_provider.set_snapshot({
        "windows": [{"title": "Welcome to Protection Center"}],
        "controls": [{"text": "Welcome"}],
    })
    case = TestCase(
        case_id="TC-PROT-1",
        title="Open Protection Center",
        steps=(
            _step("S001"),
            _step("S002", action_id="gui.type_text"),
            _step("S003", expectations=(
                Expectation(type="window_text_contains", value="Welcome"),
            )),
        ),
        timeout_seconds=60,
    )
    result = executor.run_case(case)
    assert result.outcome is CaseOutcome.PASSED
    assert all(sr.status is StepStatus.PASSED for sr in result.step_results)


# ---------------------------------------------------------------------------
# Acceptance #2 — action_ok + failing expectation -> FAILED
# ---------------------------------------------------------------------------


def test_acceptance_action_ok_but_expectation_failed(executor, backend, obs_provider):
    obs_provider.set_snapshot({"windows": [{"title": "Home"}], "controls": []})
    case = TestCase(
        case_id="TC-PROT-2",
        title="Click + Verify",
        steps=(
            _step("S001", expectations=(
                Expectation(type="action_ok"),
                Expectation(type="window_text_contains", value="Welcome"),
            )),
        ),
    )
    result = executor.run_case(case)
    assert result.step_results[0].status is StepStatus.FAILED
    assert result.outcome is CaseOutcome.FAILED


# ---------------------------------------------------------------------------
# Acceptance #3 — backend unavailable -> every step BLOCKED
# ---------------------------------------------------------------------------


def test_acceptance_backend_unavailable(executor, backend, obs_provider):
    obs_provider.raise_on_refresh = True
    case = TestCase(
        case_id="TC-PROT-3",
        title="Backend offline",
        steps=(_step("S001"), _step("S002"), _step("S003")),
    )
    result = executor.run_case(case)
    assert result.outcome is CaseOutcome.BLOCKED
    assert all(
        sr.status is StepStatus.BLOCKED and
        sr.error and sr.error["code"] == "backend_unavailable"
        for sr in result.step_results
    )


# ---------------------------------------------------------------------------
# Acceptance #4 — retry bound
# ---------------------------------------------------------------------------


def test_acceptance_retry_bound(backend, obs_provider):
    """Backend fails twice, then succeeds on the third attempt.

    P1.2 review #1 Blocker 4 — deterministic responses queue.
    """
    from fake_target import _failed_receipt, _ok_receipt
    backend.queue_receipts(
        _failed_receipt("gui.click"),
        _failed_receipt("gui.click"),
        _ok_receipt("gui.click"),
    )
    executor = AtomicExecutor(
        dispatch=backend.dispatch,
        observation_provider=obs_provider,
        config=ExecutorConfig(retry_max=2),
    )
    case = TestCase(
        case_id="TC-RETRY",
        title="Retry bound",
        steps=(_step("S001", on_error="retry"),),
    )
    result = executor.run_case(case)
    # 1 initial + 2 retries = 3 calls; the third succeeds.
    assert backend.call_count["gui.click"] == 3
    assert result.step_results[0].status is StepStatus.PASSED


def test_acceptance_retry_exhausted_blocks_subsequent_steps(backend, obs_provider):
    backend.receipt_overrides["gui.click"] = {"ok": False, "result": {"error": "boom"}}
    executor = AtomicExecutor(
        dispatch=backend.dispatch,
        observation_provider=obs_provider,
        config=ExecutorConfig(retry_max=2),
    )
    case = TestCase(
        case_id="TC-RETRY-EXHAUST",
        title="Retry exhausted",
        steps=(
            _step("S001", on_error="retry"),
            _step("S002"),
        ),
    )
    result = executor.run_case(case)
    assert result.step_results[0].status is StepStatus.FAILED
    assert result.step_results[1].status is StepStatus.SKIPPED
    assert result.abort_reason == "retry_exhausted"


# ---------------------------------------------------------------------------
# Acceptance #5 — capture_and_abort
# ---------------------------------------------------------------------------


def test_acceptance_capture_and_abort(executor, backend, obs_provider):
    backend.receipt_overrides["gui.click"] = {"ok": False, "result": {"error": "boom"}}
    case = TestCase(
        case_id="TC-CAPABORT",
        title="Capture and abort",
        steps=(
            _step("S001", on_error="capture_and_abort"),
            _step("S002"),
            _step("S003"),
        ),
    )
    result = executor.run_case(case)
    statuses = [sr.status for sr in result.step_results]
    assert statuses == [StepStatus.FAILED, StepStatus.SKIPPED, StepStatus.SKIPPED]
    assert result.aborted is True
    assert result.abort_reason == "capture_and_abort"
    assert result.outcome is CaseOutcome.FAILED


# ---------------------------------------------------------------------------
# Acceptance #6 — atomic step-results.json
# ---------------------------------------------------------------------------


def test_acceptance_step_results_atomic(executor, backend, obs_provider, tmp_path):
    target = tmp_path / "step-results.json"
    case = TestCase(
        case_id="TC-ATOMIC",
        title="Atomic write",
        steps=(_step("S001"), _step("S002"), _step("S003")),
    )
    executor.run_case(case, step_results_path=target)
    payload = json.loads(target.read_text())
    assert payload["case_id"] == "TC-ATOMIC"
    assert len(payload["step_results"]) == 3


def test_acceptance_step_results_partial_progress(executor, backend, obs_provider, tmp_path):
    """If the backend raises mid-case, the steps that already
    completed remain on disk; subsequent steps become blocked.
    This demonstrates the atomic-write durability contract."""
    target = tmp_path / "step-results.json"
    # First step succeeds (gui.type_text); second step's backend raises.
    backend.raise_on.add("gui.click")

    case = TestCase(
        case_id="TC-CRASH",
        title="Crash mid-case",
        steps=(
            _step("S001", action_id="gui.type_text"),  # succeeds
            _step("S002"),  # raises -> blocked (FR-P1.2-04)
            _step("S003"),  # skipped after abort
        ),
    )
    executor.run_case(case, step_results_path=target)

    # The two recorded steps are persisted atomically.
    assert target.exists()
    payload = json.loads(target.read_text())
    assert payload["case_id"] == "TC-CRASH"
    assert len(payload["step_results"]) == 3
    statuses = [sr["status"] for sr in payload["step_results"]]
    assert statuses[0] == "passed"
    assert statuses[1] == "blocked"
    assert statuses[2] == "skipped"


# ---------------------------------------------------------------------------
# Required Test: visual evidence unavailable blocks step
# ---------------------------------------------------------------------------


def test_required_visual_evidence_unavailable_blocks(executor, backend, obs_provider):
    """P1.4: visual_evidence_captured evaluator runs and FAILED
    when no evidence has been recorded for the step."""
    case = TestCase(
        case_id="TC-VIS",
        title="Visual evidence required but unavailable",
        steps=(
            _step("S001", expectations=(Expectation(type="visual_evidence_captured"),)),
        ),
    )
    result = executor.run_case(case)
    sr = result.step_results[0]
    assert sr.status is StepStatus.FAILED
    # The expectation result is in the step's expectation_results list.
    vis_results = [
        er for er in sr.expectation_results
        if er.expectation_type == "visual_evidence_captured"
    ]
    assert len(vis_results) == 1
    assert vis_results[0].status is StepStatus.FAILED


# ---------------------------------------------------------------------------
# Required Test: target_stale blocks step
# ---------------------------------------------------------------------------


def test_required_target_stale_blocks(executor, backend, obs_provider):
    obs_provider.snapshot_id = "snap-current"
    case = TestCase(
        case_id="TC-STALE",
        title="Stale target reference",
        steps=(
            _step("S001", target_ref=TargetRef(
                snapshot_id="snap-old", target_id="tgt-1",
                selector_hint={"text": "OK"},
            )),
        ),
    )
    result = executor.run_case(case)
    sr = result.step_results[0]
    assert sr.status is StepStatus.BLOCKED
    assert sr.error["code"] == "target_stale"


# ---------------------------------------------------------------------------
# Configuration: visual_evidence_available flips availability
# ---------------------------------------------------------------------------


def test_config_visual_evidence_available_flag(backend, obs_provider):
    """P1.4: visual_evidence_available is now a no-op flag (the
    evaluator is always wired). The flag is preserved for
    forward-compatibility but has no effect on the step outcome.
    """
    executor = AtomicExecutor(
        dispatch=backend.dispatch,
        observation_provider=obs_provider,
        config=ExecutorConfig(visual_evidence_available=False),
    )
    case = TestCase(
        case_id="TC-VIS-CONF",
        title="Visual evidence (P1.4 default-on)",
        steps=(
            _step("S001", expectations=(Expectation(type="visual_evidence_captured"),)),
        ),
    )
    result = executor.run_case(case)
    # No evidence recorded -> the expectation FAILED, step FAILED,
    # but never blocked (the hard gate is gone).
    assert result.step_results[0].status is StepStatus.FAILED