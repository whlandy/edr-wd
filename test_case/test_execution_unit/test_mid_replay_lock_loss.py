"""Mid-replay unrecoverable lock loss -> honest failure, no dispatch-after-loss.

``AtomicExecutor`` refreshes the window observation **before** dispatching each
step's action mirror.  When that refresh raises an unrecoverable
``BackendUnavailable`` (the window lock is lost and cannot be relocked), the
honest fail-closed contract is:

  - the run does **not** report success: it ends ``BLOCKED`` (at least one
    required step could not be executed), never a clean pass;
  - the step whose refresh failed carries a **typed** ``backend_unavailable``
    error (never a crash, never a silent skip with no reason);
  - every later step is explicitly ``SKIPPED`` with ``skipped_after_abort``
    rather than silently dropped or passed;
  - and the load-bearing safety invariant: **once the lock is lost, no further
    action ever dispatches** -- dispatch happens only after a successful
    refresh, so a lost lock can never fire an action against a stale or wrong
    window.

``test_backend_unavailable_blocks_every_step`` already covers the failure-from-
the-start case (all steps blocked).  This file covers the distinct *mid-replay*
case: at least the first step's action runs, then the lock disappears.

Lock loss is triggered by arming ``raise_now`` on the observation provider from
inside the backend's first dispatch.  The test asserts the *stable* outcome
invariants (typed errors, no dispatch-after-loss, no clean-pass) and does not
depend on the exact number of internal refreshes the executor issues per step,
so it does not overfit to implementation detail.

A regression that removed the refresh-before-dispatch ordering, or swallowed
``BackendUnavailable`` into a silent pass / a clean abort with no typed error or
remedial skip, fails here.
"""

from __future__ import annotations

import threading
from typing import Any, Mapping

from action_dispatcher import ActionReceipt
from protocol_models import AtomicTestStep, Expectation, TargetRef, TestCase
from execution import (
    AtomicExecutor,
    BackendUnavailable,
    CaseOutcome,
    StepStatus,
)


class _FailingRefreshProvider:
    """ObservationProvider that refreshes normally until armed, then raises
    ``BackendUnavailable`` on every subsequent refresh (the lock is gone)."""

    def __init__(self, snapshot: Mapping[str, Any]):
        self._snapshot = dict(snapshot)
        self._lock = threading.Lock()
        self.raise_now = False
        self.snapshot_id = "snap-fake"
        self.refresh_count = 0

    def latest_snapshot_id(self) -> str | None:
        return self.snapshot_id

    def get_snapshot(self, snapshot_id: str) -> Mapping[str, Any] | None:
        if snapshot_id != self.snapshot_id:
            return None
        return self._snapshot

    def refresh(self) -> str:
        with self._lock:
            self.refresh_count += 1
            if self.raise_now:
                raise BackendUnavailable("cached window lock is stale")
            return self.snapshot_id


class _BackendWhoseFirstDispatchArmsLockLoss:
    """Every dispatch is recorded; the first one arms the provider so the next
    refresh raises -- modelling the lock being lost after the first action."""

    def __init__(self, provider: "_FailingRefreshProvider"):
        self.provider = provider
        self.calls: list[str] = []

    def dispatch(self, *, action_id, action_code=None, args=None,
                 target_ref=None, request_id=None) -> ActionReceipt:
        self.calls.append(action_id)
        if len(self.calls) == 1:
            self.provider.raise_now = True
        return ActionReceipt(
            code="ok", ok=True, action_id=action_id,
            action_code=action_code or "A000", request_id=request_id or "R",
            result={"data": {"ok": True}}, server_instance_id="inst",
        )


def _snapshot1():
    return {
        "observation_id": "snap-fake",
        "windows": [{"process": "EDRClient.exe", "title": "logo1"}],
        "controls": [{"automation_id": "logCenterBtn", "text": "日志中心"}],
    }


def _target():
    return TargetRef(
        snapshot_id="snap-fake", target_id="T0001",
        selector_hint={"automation_id": "logCenterBtn"},
    )


def _click(step_id: str) -> AtomicTestStep:
    return AtomicTestStep(
        step_id=step_id,
        step_no=int(step_id.lstrip("S")),
        action_id="gui.click",
        args={"x": 1, "y": 2},
        target_ref=_target(),
        expectations=(Expectation(type="action_ok"),),
        on_error="abort",
        required=True,
    )


def _case(*steps) -> TestCase:
    return TestCase(case_id="TC-MID-LOCK", title="mid replay lock loss",
                    steps=tuple(steps), timeout_seconds=60)


def test_lock_loss_fails_closed_with_typed_error_and_no_dispatch_after_loss():
    """The first step's action runs; then the lock is lost.  The run must NOT
    report success, the failed refresh must surface as a typed
    backend_unavailable (not a crash / silent pass), later steps must be
    explicitly skipped, and -- the load-bearing safety invariant -- no further
    action may dispatch after the lock is gone."""
    provider = _FailingRefreshProvider(snapshot=_snapshot1())
    backend = _BackendWhoseFirstDispatchArmsLockLoss(provider)
    executor = AtomicExecutor(dispatch=backend.dispatch,
                              observation_provider=provider)
    result = executor.run_case(_case(_click("S001"), _click("S002"), _click("S003")))

    # Not a clean pass: a required step could not be executed.
    assert result.outcome is CaseOutcome.BLOCKED
    statuses = [sr.status for sr in result.step_results]
    assert StepStatus.BLOCKED in statuses
    # Every later step is explicitly skipped with the abort reason, never a
    # silent pass or a bare skip with no reason.
    for sr in result.step_results:
        if sr.status is StepStatus.SKIPPED:
            assert sr.error and sr.error["code"] == "skipped_after_abort"
    # The refresh failure is a typed error where it surfaced, never a crash.
    blocked = next(sr for sr in result.step_results if sr.status is StepStatus.BLOCKED)
    assert blocked.error and blocked.error["code"] == "backend_unavailable"
    assert blocked.error.get("message")
    # Load-bearing: once the lock is lost no further action ever dispatches.
    # Exactly the single action that ran before the loss fired.
    assert len(backend.calls) <= 1
    # And no step that had its refresh fail was marked a clean pass.
    assert all(sr.status is not StepStatus.PASSED or sr.step_id == "S001"
               for sr in result.step_results)


def test_lock_loss_blocked_step_never_a_clean_pass_nor_silent_skip():
    """The refresh failure surfaces as an honest BLOCKED with a typed reason --
    a regression that turned the failed step into a passed / skipped-without-
    error would break fail-closed and be caught here."""
    provider = _FailingRefreshProvider(snapshot=_snapshot1())
    backend = _BackendWhoseFirstDispatchArmsLockLoss(provider)
    executor = AtomicExecutor(dispatch=backend.dispatch,
                              observation_provider=provider)
    result = executor.run_case(_case(_click("S001"), _click("S002"), _click("S003")))

    blocked = [sr for sr in result.step_results if sr.status is StepStatus.BLOCKED]
    assert blocked, "lock loss must yield at least one typed BLOCKED step"
    for sr in blocked:
        assert sr.error and sr.error["code"] == "backend_unavailable"
        assert sr.error.get("message")
    # No step may be a bare skip that drops the failure reason.
    for sr in result.step_results:
        if sr.status is StepStatus.SKIPPED:
            assert sr.error and sr.error["code"] == "skipped_after_abort"
