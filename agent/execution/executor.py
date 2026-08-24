"""
executor.py — AtomicExecutor (P1.2).

Layering:

    target/protocol_models  — TestCase / AtomicTestStep / Expectation
    target/action_dispatcher — dispatch() returns ActionReceipt
    agent/execution         — AtomicExecutor (THIS FILE)

The executor runs the per-step state machine (architecture §11, 16
steps) and applies the on_error policies:

    * abort (default)
    * retry (bounded; default N=2)
    * capture_and_abort
    * reobserve_replan (P2.2 stub; no behaviour in P1.2)

The executor never executes two mutating actions back-to-back without
an intervening `OBSERVING_AFTER` (FR-P1.2-01). The state machine
guarantees this by making `EXECUTING -> OBSERVING_AFTER` the only legal
post-execution move.

Step status precedence (§9.4): `failed > blocked > skipped > passed`.
Case outcome:

    * required step `failed`    -> case `failed`
    * required step `blocked`   -> case `blocked`
    * all required skipped      -> case `skipped`
    * otherwise                 -> case `passed`

Backend unavailability at executor start (FR-P1.2-04): every step
becomes `blocked`, never `failed`.

FR-P1.4-08: declaring `visual_evidence_captured` verifies that a
role-matching screenshot exists and still matches its recorded digest.

FR-P1.2-08: a step whose `target_ref.snapshot_id` is older than the
latest known snapshot becomes `blocked` with
`error.code == "target_stale"`.

FR-P1.2-09: `transition.expected=true` skips the pre-action
observation optimisation but does not create a checkpoint (P2.1
territory).
"""

from __future__ import annotations

import datetime as _dt
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Mapping

from target.action_dispatcher import ActionReceipt
from target.protocol_models import (
    AtomicTestStep,
    Expectation,
    TestCase,
)

from .confirmation import (
    ConfirmationDecision,
    ConfirmationGate,
    ConfirmationReason,
    ExecutionContext,
)

from .expectations import (
    _EvidenceView,
    EVALUATORS_NOT_AVAILABLE,
    EXPECTATION_REGISTRY,
    ExpectationNotAvailable,
    ExpectationResult,
)
from .models import (
    CaseOutcome,
    CaseRunResult,
    ExecutorConfig,
    OnErrorPolicy,
    StepResult,
    StepStatus,
    coerce_on_error,
    worst_status,
)
from .state_machine import (
    IllegalTransition,
    StepState,
    is_terminal,
    next_state,
)
from .step_results import write_atomic


# ---------------------------------------------------------------------------
# Observation provider interface (duck-typed)
# ---------------------------------------------------------------------------


class ObservationProvider:
    """Reads observation snapshots on demand.

    P1.2 keeps this interface minimal: an `ObservationProvider`
    implementation is whatever the executor's host wires up.  The
    fake_target fixture used in `test_case/fake_target/` implements
    this protocol directly; a future P1.3 wiring can plug in the
    real observation bridge.
    """

    def latest_snapshot_id(self) -> str | None:
        raise NotImplementedError

    def get_snapshot(self, snapshot_id: str) -> Mapping[str, Any] | None:
        raise NotImplementedError

    def refresh(self) -> str:
        """Take a fresh observation; return its snapshot_id."""
        raise NotImplementedError


class BackendUnavailable(Exception):
    """FR-P1.2-04: backend unreachable at executor start."""


class StepMaterializationError(Exception):
    """A dynamic step could not bind to the current observation."""

    def __init__(self, code: str, message: str, *, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.details = dict(details or {})


# ---------------------------------------------------------------------------
# Helper: timestamp utilities
# ---------------------------------------------------------------------------


def _utcnow_iso() -> str:
    return _dt.datetime.now(tz=_dt.timezone.utc).isoformat(timespec="milliseconds")


def _ms(elapsed_seconds: float) -> int:
    return int(round(elapsed_seconds * 1000.0))


def _observation_only_action(
    *, action_id: str, action_code: str, request_id: str, **_: Any,
) -> ActionReceipt:
    return ActionReceipt.from_ok(
        action_id=action_id,
        action_code=action_code,
        request_id=request_id,
        result={"observation_only": True},
    )


DEFAULT_LOCAL_ACTION_HANDLERS: Mapping[str, Callable[..., ActionReceipt]] = {
    "observe.assert": _observation_only_action,
}


# ---------------------------------------------------------------------------
# AtomicExecutor
# ---------------------------------------------------------------------------


class AtomicExecutor:
    """Runs one TestCase end-to-end.

    The executor is constructed with all its collaborators; this is a
    deliberate DI choice so tests can swap in stubs without monkey-
    patching. The constructor does NOT itself touch the network or
    the filesystem.
    """

    def __init__(
        self,
        *,
        dispatch: Callable[..., ActionReceipt],
        observation_provider: ObservationProvider,
        expectations: Mapping[str, Callable[..., ExpectationResult]]
            | None = None,
        config: ExecutorConfig | None = None,
        confirmation_gate: ConfirmationGate | None = None,
        risk_lookup: Callable[[str], tuple[str, str]] | None = None,
        step_materializer: Callable[[AtomicTestStep, Any], AtomicTestStep] | None = None,
        local_action_handlers: Mapping[str, Callable[..., ActionReceipt]] | None = None,
    ) -> None:
        self._dispatch = dispatch
        self._observations = observation_provider
        self._expectations = (
            EXPECTATION_REGISTRY if expectations is None else expectations
        )
        self._config = config or ExecutorConfig()
        # P3.1.E.B — confirmation boundary at the executor.
        # The gate is OPTIONAL: when not wired, the executor
        # behaves as before (no confirmation check). When wired,
        # the gate runs before every dispatch.
        self._confirmation_gate: ConfirmationGate | None = (
            confirmation_gate
        )
        # risk_lookup: action_id -> (risk, side_effect).
        # When None, the gate (if any) treats all actions as
        # risk="low", side_effect="none" (no confirmation needed).
        self._risk_lookup: Callable[[str], tuple[str, str]] = (
            risk_lookup
            or (lambda _action_id: ("low", "none"))
        )
        self._step_materializer = step_materializer
        self._local_action_handlers = (
            DEFAULT_LOCAL_ACTION_HANDLERS
            if local_action_handlers is None else local_action_handlers
        )

    # -----------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------

    def run_case(
        self,
        case: TestCase,
        *,
        target_selector: str = "",
        step_results_path: Path | None = None,
        execution_context: ExecutionContext | None = None,
        step_materializer: Callable[[AtomicTestStep, Any], AtomicTestStep] | None = None,
    ) -> CaseRunResult:
        """Execute every step in `case.steps`.

        Writes `step-results.json` to `step_results_path` after each
        step when provided (FR-P1.2-06). Backend availability is
        probed once; if unavailable, every step becomes `blocked`
        (FR-P1.2-04).

        `execution_context` carries profile + confirmation tokens
        (P3.1.E.B). When the executor is configured with a
        `confirmation_gate`, the context is required for steps
        with risk/side_effect that triggers the gate. When the
        context is None, the executor uses a default context
        with profile="default" and no tokens."""

        started = _utcnow_iso()
        start_perf = time.perf_counter()

        backend_unavailable = self._probe_backend()

        steps = list(case.steps)
        required_flags = tuple(step.required for step in steps)
        step_results: list[StepResult] = []
        aborted = False
        abort_reason = ""
        # P3.1.E.B — execution context (profile + tokens).
        # Stash on the executor so run_step() can read it.
        self._execution_context: ExecutionContext = (
            execution_context or ExecutionContext()
        )

        if backend_unavailable:
            for step in steps:
                sr = self._blocked_unavailable(step)
                step_results.append(sr)
                self._persist(step_results_path, case, step_results)
            ended = _utcnow_iso()
            return CaseRunResult(
                case_id=case.case_id,
                outcome=self._derive_case_outcome(
                    step_results, required_flags,
                ),
                started_at=started,
                ended_at=ended,
                duration_ms=_ms(time.perf_counter() - start_perf),
                step_results=step_results,
                aborted=False,
                abort_reason="backend_unavailable",
            )

        materializer = step_materializer or self._step_materializer
        for step_index, step in enumerate(steps):
            sr = self.run_step(case, step, snapshot=None, step_materializer=materializer)
            step_results.append(sr)
            self._persist(step_results_path, case, step_results)

            if sr.status == StepStatus.BLOCKED and step.required:
                aborted = True
                abort_reason = "required_step_blocked"
                for remaining in steps[step_index + 1:]:
                    step_results.append(self._skipped(remaining))
                    self._persist(step_results_path, case, step_results)
                break

            if sr.status == StepStatus.FAILED and step.required:
                policy = coerce_on_error(step.on_error)
                if policy is OnErrorPolicy.RETRY:
                    # Bounded retry around the *whole step*; the
                    # in-step retry counter resets per step.
                    sr, step_results = self._retry_step(
                        case, step, sr, step_results, materializer,
                    )
                    if sr.status is StepStatus.FAILED:
                        # Retry exhausted; decide whether to skip
                        # remaining steps based on policy.
                        aborted = True
                        abort_reason = "retry_exhausted"
                        for remaining in steps[step_index + 1:]:
                            step_results.append(self._skipped(remaining))
                            self._persist(step_results_path, case, step_results)
                        break
                    continue
                elif policy is OnErrorPolicy.CAPTURE_AND_ABORT:
                    aborted = True
                    abort_reason = "capture_and_abort"
                    for remaining in steps[step_index + 1:]:
                        step_results.append(self._skipped(remaining))
                        self._persist(step_results_path, case, step_results)
                    break
                else:
                    # Default + reobserve_replan stub -> abort.
                    aborted = True
                    abort_reason = "on_error_abort"
                    for remaining in steps[step_index + 1:]:
                        step_results.append(self._skipped(remaining))
                        self._persist(step_results_path, case, step_results)
                    break

        ended = _utcnow_iso()
        outcome = self._derive_case_outcome(step_results, required_flags)
        return CaseRunResult(
            case_id=case.case_id,
            outcome=outcome,
            started_at=started,
            ended_at=ended,
            duration_ms=_ms(time.perf_counter() - start_perf),
            step_results=step_results,
            aborted=aborted,
            abort_reason=abort_reason,
        )

    def run_step(
        self,
        case: TestCase,
        step: AtomicTestStep,
        snapshot: Mapping[str, Any] | None,
        step_materializer: Callable[[AtomicTestStep, Any], AtomicTestStep] | None = None,
    ) -> StepResult:
        """Run a single step (also exercised by the retry loop).

        The caller can pass a pre-computed snapshot, or `None` to let
        the executor refresh. The full per-step algorithm is
        implemented via the state machine."""

        started = _utcnow_iso()
        start_perf = time.perf_counter()

        state = StepState.CREATED
        receipt: ActionReceipt | None = None
        observation: Mapping[str, Any] | None = snapshot
        expectation_results: list[ExpectationResult] = []
        error_payload: dict[str, Any] | None = None
        transition_expected = bool(
            step.transition is not None and step.transition.expected
        )

        # 1. CREATED -> VALIDATING: gate on visual-evidence and stale target.
        state = next_state(state, StepState.VALIDATING)

        # Pre-execution validation: visual-evidence not available.
        for exp in step.expectations:
            if exp.type in EVALUATORS_NOT_AVAILABLE:
                ended = _utcnow_iso()
                return self._finalise_blocked(
                    step, started, ended, start_perf,
                    error_payload={
                        "code": "expectation_not_available",
                        "expectation": exp.type,
                    },
                    transition_expected=transition_expected,
                )

        # 2. VALIDATING -> OBSERVING (refresh).
        state = next_state(state, StepState.OBSERVING)
        try:
            snapshot_id = self._observations.refresh()
            observation = self._observations.get_snapshot(snapshot_id)
        except BackendUnavailable as exc:
            ended = _utcnow_iso()
            return self._finalise_blocked(
                step, started, ended, start_perf,
                error_payload={"code": "backend_unavailable", "message": str(exc)},
                transition_expected=transition_expected,
            )

        materializer = step_materializer or self._step_materializer
        if materializer is not None:
            try:
                step = materializer(step, observation)
            except StepMaterializationError as exc:
                ended = _utcnow_iso()
                return self._finalise_blocked(
                    step,
                    started,
                    ended,
                    start_perf,
                    error_payload={
                        "code": exc.code,
                        "message": str(exc),
                        "details": exc.details,
                    },
                    transition_expected=transition_expected,
                )

        # FR-P1.2-08: target stale -> blocked.
        if step.target_ref is not None and step.target_ref.snapshot_id:
            latest = self._observations.latest_snapshot_id()
            if latest is not None and step.target_ref.snapshot_id != latest:
                ended = _utcnow_iso()
                return self._finalise_blocked(
                    step, started, ended, start_perf,
                    error_payload={
                        "code": "target_stale",
                        "step_snapshot_id": step.target_ref.snapshot_id,
                        "latest_snapshot_id": latest,
                    },
                    transition_expected=transition_expected,
                )

        # 3. OBSERVING -> PREPARING_STEP. Skip CAPTURING_BEFORE if no policy.
        state = next_state(state, StepState.PREPARING_STEP)

        # P3.1.E.B — confirmation boundary (D14).
        # The gate is OPTIONAL. When wired, it runs BEFORE
        # EXECUTING. A required decision yields a blocked step
        # with code="confirmation_required" + action_id, and
        # the dispatcher is NEVER called (per D14: plan MUST
        # NOT be dispatched).
        if self._confirmation_gate is not None:
            risk, side_effect = self._risk_lookup(step.action_id)
            decision: ConfirmationDecision = (
                self._confirmation_gate.check(
                    action_id=step.action_id,
                    risk=risk,
                    side_effect=side_effect,
                    context=self._execution_context,
                )
            )
            if decision.required:
                ended = _utcnow_iso()
                return self._finalise_blocked(
                    step, started, ended, start_perf,
                    error_payload={
                        "code": decision.reject_code,
                        "action_id": decision.action_id,
                        "reason": decision.reason.value,
                        "risk": decision.risk,
                        "side_effect": decision.side_effect,
                        "profile": decision.profile,
                    },
                    transition_expected=transition_expected,
                )

        skip_before = (
            transition_expected
            or step.evidence is None
            or getattr(step.evidence, "screenshot", None) not in ("before", "before_after")
        )
        if not skip_before:
            state = next_state(state, StepState.CAPTURING_BEFORE)
            # P1.4 will hook screenshot capture here; P1.2 is a no-op.

        # 4. CAPTURING_BEFORE -> EXECUTING (or PREPARING_STEP -> EXECUTING).
        if state is StepState.CAPTURING_BEFORE:
            state = next_state(state, StepState.EXECUTING)
        else:
            state = next_state(state, StepState.EXECUTING)

        # 5. EXECUTING -> OBSERVING_AFTER.
        try:
            request_id = f"R-{uuid.uuid4().hex[:12]}"
            local_handler = self._local_action_handlers.get(step.action_id)
            if local_handler is not None:
                receipt = local_handler(
                    action_id=step.action_id,
                    action_code=step.action_code,
                    request_id=request_id,
                    args=dict(step.args),
                    target_ref=(
                        _target_ref_to_dict(step.target_ref)
                        if step.target_ref is not None else None
                    ),
                )
            else:
                receipt = self._dispatch(
                    action_id=step.action_id,
                    action_code=step.action_code,
                    args=dict(step.args),
                    target_ref=(
                        _target_ref_to_dict(step.target_ref)
                        if step.target_ref is not None
                        else None
                    ),
                    request_id=request_id,
                )
        except Exception as exc:  # dispatcher raises on unknown action_id etc.
            ended = _utcnow_iso()
            return self._finalise_blocked(
                step, started, ended, start_perf,
                error_payload={
                    "code": "dispatcher_exception",
                    "message": f"{type(exc).__name__}: {exc}",
                },
                transition_expected=transition_expected,
            )

        state = next_state(state, StepState.OBSERVING_AFTER)
        # P1.2 does not classify transitions; that's P2.1. We do,
        # however, refresh the observation so expectations evaluate
        # against post-action state.
        try:
            snapshot_id = self._observations.refresh()
            observation = self._observations.get_snapshot(snapshot_id)
        except BackendUnavailable as exc:
            ended = _utcnow_iso()
            return self._finalise_blocked(
                step, started, ended, start_perf,
                error_payload={"code": "backend_unavailable", "message": str(exc)},
                transition_expected=transition_expected,
                receipt=receipt,
            )

        # 6. CAPTURING_AFTER (policy dependent).
        skip_after = (
            transition_expected
            or step.evidence is None
            or getattr(step.evidence, "screenshot", None) not in ("after", "before_after")
        )
        if not skip_after:
            state = next_state(state, StepState.CAPTURING_AFTER)
            # P1.4 hook; no-op in P1.2.
        # (Otherwise OBSERVING_AFTER -> ASSERTING directly.)
        if state is StepState.OBSERVING_AFTER:
            state = next_state(state, StepState.ASSERTING)
        else:
            state = next_state(state, StepState.ASSERTING)

        # 7. ASSERTING: expose only this materializer's evidence while
        # evaluating the current step, then restore the previous context.
        evidence_index = getattr(materializer, "evidence_index", {})
        evidence_root = getattr(materializer, "evidence_root", None)
        evidence_token = _EvidenceView.set_evidence_index(
            evidence_index, evidence_root,
        )
        try:
            for exp in step.expectations:
                evaluator = self._expectations.get(exp.type)
                if evaluator is None:
                    if exp.type in EVALUATORS_NOT_AVAILABLE:
                        error_payload = {
                            "code": "expectation_not_available",
                            "expectation": exp.type,
                        }
                        break
                    expectation_results.append(ExpectationResult(
                        expectation_type=exp.type,
                        status=StepStatus.FAILED,
                        diagnostic="unknown expectation type",
                    ))
                    continue
                t0 = time.perf_counter()
                try:
                    result = evaluator(step, exp, observation, receipt)
                except Exception as exc:
                    result = ExpectationResult(
                        expectation_type=exp.type,
                        status=StepStatus.FAILED,
                        diagnostic=f"{type(exc).__name__}: {exc}",
                        duration_ms=_ms(time.perf_counter() - t0),
                    )
                else:
                    if result.duration_ms == 0:
                        result = ExpectationResult(
                            expectation_type=result.expectation_type,
                            status=result.status,
                            expected=result.expected,
                            actual=result.actual,
                            observation_id=result.observation_id,
                            duration_ms=_ms(time.perf_counter() - t0),
                            diagnostic=result.diagnostic,
                        )
                expectation_results.append(result)
                if error_payload is not None:
                    break
        finally:
            _EvidenceView.reset(evidence_token)

        # 8. ASSERTING -> RECORDING.
        # Step outcome follows architecture §9.4 + Blocker 3 review
        # #1 fix: the action receipt's ok flag is the source of
        # truth for whether the action executed.  An empty
        # expectation list does not turn a failing action into a
        # passing step; a backend failure surfaces as FAILED
        # regardless of expectations.  Expectations add *more*
        # conditions, not weaker ones.
        receipt_ok = bool(receipt.ok) if receipt is not None else False
        expectation_worst = (
            worst_status(*(er.status for er in expectation_results))
            if expectation_results else StepStatus.PASSED
        )
        if error_payload is not None:
            worst = StepStatus.BLOCKED
        elif not receipt_ok:
            worst = StepStatus.FAILED
        else:
            worst = expectation_worst

        state = next_state(state, StepState.RECORDING)
        state = next_state(state, StepState.FINALIZING)
        state = next_state(state, StepState.COMPLETED)

        ended = _utcnow_iso()
        return StepResult(
            step_id=step.step_id,
            step_no=step.step_no,
            status=(
                StepStatus.BLOCKED if error_payload is not None
                else (worst if worst is not StepStatus.PASSED else StepStatus.PASSED)
            ),
            started_at=started,
            ended_at=ended,
            duration_ms=_ms(time.perf_counter() - start_perf),
            action=receipt,
            expectation_results=expectation_results,
            error=error_payload,
            transition_expected=transition_expected,
        )

    # -----------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------

    def _probe_backend(self) -> bool:
        try:
            self._observations.refresh()
            return False
        except BackendUnavailable:
            return True

    def _blocked_unavailable(self, step: AtomicTestStep) -> StepResult:
        now = _utcnow_iso()
        return StepResult(
            step_id=step.step_id,
            step_no=step.step_no,
            status=StepStatus.BLOCKED,
            started_at=now,
            ended_at=now,
            duration_ms=0,
            action=None,
            expectation_results=[],
            error={"code": "backend_unavailable"},
            transition_expected=False,
        )

    def _skipped(self, step: AtomicTestStep) -> StepResult:
        now = _utcnow_iso()
        return StepResult(
            step_id=step.step_id,
            step_no=step.step_no,
            status=StepStatus.SKIPPED,
            started_at=now,
            ended_at=now,
            duration_ms=0,
            action=None,
            expectation_results=[],
            error={"code": "skipped_after_abort"},
            transition_expected=False,
        )

    def _retry_step(
        self,
        case: TestCase,
        step: AtomicTestStep,
        initial: StepResult,
        step_results: list[StepResult],
        step_materializer: Callable[[AtomicTestStep, Any], AtomicTestStep] | None = None,
    ) -> tuple[StepResult, list[StepResult]]:
        """Bounded retry loop.  Replaces the trailing failed entry."""
        last = initial
        for _ in range(self._config.retry_max):
            # Re-run step from scratch (state machine starts at CREATED).
            attempt = self.run_step(
                case, step, snapshot=None, step_materializer=step_materializer,
            )
            if attempt.status is not StepStatus.FAILED:
                # Replace the trailing failed entry.
                step_results.pop()
                step_results.append(attempt)
                return attempt, step_results
            last = attempt
        return last, step_results

    def _finalise_blocked(
        self,
        step: AtomicTestStep,
        started: str,
        ended: str,
        start_perf: float,
        *,
        error_payload: dict[str, Any],
        transition_expected: bool,
        receipt: ActionReceipt | None = None,
    ) -> StepResult:
        return StepResult(
            step_id=step.step_id,
            step_no=step.step_no,
            status=StepStatus.BLOCKED,
            started_at=started,
            ended_at=ended,
            duration_ms=_ms(time.perf_counter() - start_perf),
            action=receipt,
            expectation_results=[],
            error=error_payload,
            transition_expected=transition_expected,
        )

    def _derive_case_outcome(
        self,
        step_results: list[StepResult],
        required_flags: tuple[bool, ...] | None = None,
    ) -> CaseOutcome:
        # Architecture §9.4 case outcome precedence:
        #   1. any required failed  -> FAILED
        #   2. any required blocked -> BLOCKED
        #   3. all required skipped -> SKIPPED
        #   4. otherwise            -> PASSED
        required_results = [
            result for index, result in enumerate(step_results)
            if required_flags is None
            or index >= len(required_flags)
            or required_flags[index]
        ]
        # Empty required set (e.g. all optional steps skipped) -> PASSED.
        if not required_results:
            return CaseOutcome.PASSED
        statuses = [sr.status for sr in required_results]
        if StepStatus.FAILED in statuses:
            return CaseOutcome.FAILED
        if StepStatus.BLOCKED in statuses:
            return CaseOutcome.BLOCKED
        if all(s is StepStatus.SKIPPED for s in statuses):
            return CaseOutcome.SKIPPED
        return CaseOutcome.PASSED

    def _persist(
        self,
        path: Path | None,
        case: TestCase,
        step_results: list[StepResult],
    ) -> None:
        if path is None:
            return
        payload = {
            "schema_version": "1.0.0",
            "case_id": case.case_id,
            "step_results": [sr.to_dict() for sr in step_results],
        }
        write_atomic(Path(path), payload)


def _target_ref_to_dict(target_ref: Any) -> dict[str, Any]:
    """Serialise a TargetRef to a dict without depending on a method
    that may not exist on the protocol_models version.  Only the
    stable identity fields are included."""
    return {
        "snapshot_id": target_ref.snapshot_id,
        "target_id": target_ref.target_id,
        "expected_process_name": getattr(target_ref, "expected_process_name", "") or "",
        "fingerprint": getattr(target_ref, "fingerprint", None),
        "selector_hint": getattr(target_ref, "selector_hint", None),
    }


__all__ = [
    "AtomicExecutor",
    "ObservationProvider",
    "StepMaterializationError",
    "BackendUnavailable",
    "IllegalTransition",
]
