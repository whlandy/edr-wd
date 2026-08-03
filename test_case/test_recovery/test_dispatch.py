"""test_dispatch.py — P2.2 restore dispatch + trace adapter tests (Commit E).

These tests cover Commit E.1 (concrete :class:`RestoreHandler`
implementations per strategy) and Commit E.2
(:class:`TraceStoreAdapter` forwarding :class:`RequestedEvent`
to :class:`TraceStore`).
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Mapping

import pytest

from agent.execution.recovery import (
    FailureContext,
    RecoveryErrorCode,
    RecoveryPlan,
    RecoverySeverity,
    RecoveryStatus,
    RestoreResult,
    RestoreStrategy,
)
from agent.execution.recovery_events import (
    RecoveryRequestedPayload,
    RequestedEvent,
    make_recovery_requested_event,
    make_recovery_result_event,
)
from agent.execution.restore_dispatch import (
    ProcessRestartHandler,
    ReconnectHandler,
    RedriveHandler,
    ReobserveReplanHandler,
    RestoreDispatch,
)
from agent.execution.trace_adapter import TraceStoreAdapter
from agent.trace.events import EventType
from agent.trace.store import TraceStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _failure() -> FailureContext:
    return FailureContext(
        step_id="step-1",
        branch_id="BR-001",
        failure_kind="state_drift",
    )


def _plan(
    strategy: RestoreStrategy,
    *,
    steps: tuple[str, ...] = (),
) -> RecoveryPlan:
    return RecoveryPlan(
        strategy=strategy,
        steps=steps,
        severity=RecoverySeverity.PAGE,
        timeout_ms=30_000,
    )


def _payload(failure: FailureContext, plan: RecoveryPlan) -> dict[str, object]:
    return {"failure": failure, "plan": plan, "branch_id": "BR-002"}


# ---------------------------------------------------------------------------
# E.1.1 — RedriveHandler
# ---------------------------------------------------------------------------


def test_redrive_handler_succeeds():
    h = RedriveHandler()
    out = h(RestoreStrategy.REDRIVE, _payload(_failure(), _plan(RestoreStrategy.REDRIVE)))
    assert out.ok is True
    assert out.snapshot_id == "snap-redrive"


def test_redrive_handler_rejects_wrong_strategy():
    h = RedriveHandler()
    out = h(RestoreStrategy.RECONNECT, {})
    assert out.ok is False
    assert out.code is RecoveryErrorCode.RESTORE_NOT_AVAILABLE


# ---------------------------------------------------------------------------
# E.1.2 — ReconnectHandler
# ---------------------------------------------------------------------------


def test_reconnect_handler_succeeds_with_injected_fn():
    def reconnect(payload: Mapping[str, object]) -> RestoreResult:
        return RestoreResult(ok=True, code=None, snapshot_id="snap-relock")

    h = ReconnectHandler(session_reconnect=reconnect)
    out = h(
        RestoreStrategy.RECONNECT,
        _payload(_failure(), _plan(RestoreStrategy.RECONNECT)),
    )
    assert out.ok is True
    assert out.snapshot_id == "snap-relock"


def test_reconnect_handler_without_fn_returns_lock_failed():
    h = ReconnectHandler(session_reconnect=None)
    out = h(RestoreStrategy.RECONNECT, {})
    assert out.ok is False
    assert out.code is RecoveryErrorCode.RESTORE_LOCK_VERIFY_FAILED


def test_reconnect_handler_swallows_handler_exceptions():
    def reconnect(payload: Mapping[str, object]) -> RestoreResult:
        raise RuntimeError("backend died")

    h = ReconnectHandler(session_reconnect=reconnect)
    out = h(RestoreStrategy.RECONNECT, {})
    assert out.ok is False
    assert out.code is RecoveryErrorCode.RESTORE_LOCK_VERIFY_FAILED


def test_reconnect_handler_rejects_wrong_strategy():
    h = ReconnectHandler(session_reconnect=None)
    out = h(RestoreStrategy.PROCESS_RESTART, {})
    assert out.ok is False
    assert out.code is RecoveryErrorCode.RESTORE_NOT_AVAILABLE


# ---------------------------------------------------------------------------
# E.1.3 — ProcessRestartHandler
# ---------------------------------------------------------------------------


def test_process_restart_runs_plan_steps_in_order():
    h = ProcessRestartHandler(
        catalog_dispatch=lambda a, p: RestoreResult(ok=True, code=None, snapshot_id=f"snap-{a}")
    )
    out = h(
        RestoreStrategy.PROCESS_RESTART,
        _payload(
            _failure(),
            _plan(RestoreStrategy.PROCESS_RESTART, steps=("close", "reopen")),
        ),
    )
    assert out.ok is True
    assert out.snapshot_id == "snap-restart"


def test_process_restart_short_circuits_on_first_action_failure():
    seen: list[str] = []

    def catalog(action_id: str, payload: Mapping[str, object]) -> RestoreResult:
        seen.append(action_id)
        if action_id == "close":
            return RestoreResult(
                ok=False,
                code=RecoveryErrorCode.RESTORE_LOCK_VERIFY_FAILED,
                snapshot_id=None,
            )
        return RestoreResult(ok=True, code=None, snapshot_id="snap")

    h = ProcessRestartHandler(catalog_dispatch=catalog)
    out = h(
        RestoreStrategy.PROCESS_RESTART,
        _payload(
            _failure(),
            _plan(RestoreStrategy.PROCESS_RESTART, steps=("close", "reopen")),
        ),
    )
    assert out.ok is False
    assert out.code is RecoveryErrorCode.RESTORE_LOCK_VERIFY_FAILED
    # Only the first action was attempted.
    assert seen == ["close"]


def test_process_restart_without_catalog_returns_not_available():
    h = ProcessRestartHandler(catalog_dispatch=None)
    out = h(
        RestoreStrategy.PROCESS_RESTART,
        _payload(
            _failure(),
            _plan(RestoreStrategy.PROCESS_RESTART, steps=("a", "b")),
        ),
    )
    assert out.ok is False
    assert out.code is RecoveryErrorCode.RESTORE_NOT_AVAILABLE


def test_process_restart_swallows_catalog_exception():
    def catalog(action_id: str, payload: Mapping[str, object]) -> RestoreResult:
        raise RuntimeError("backend crashed")

    h = ProcessRestartHandler(catalog_dispatch=catalog)
    out = h(
        RestoreStrategy.PROCESS_RESTART,
        _payload(
            _failure(),
            _plan(RestoreStrategy.PROCESS_RESTART, steps=("a",)),
        ),
    )
    assert out.ok is False
    assert out.code is RecoveryErrorCode.RESTORE_NOT_AVAILABLE


def test_process_restart_with_empty_steps_returns_success():
    """Degenerate case: planner emits PROCESS_RESTART with no
    inverse actions (empty SOP inverse set). Treat as success."""
    h = ProcessRestartHandler(
        catalog_dispatch=lambda a, p: RestoreResult(ok=True, code=None, snapshot_id="snap")
    )
    out = h(
        RestoreStrategy.PROCESS_RESTART,
        _payload(_failure(), _plan(RestoreStrategy.PROCESS_RESTART)),
    )
    assert out.ok is True
    assert out.snapshot_id == "snap-restart-empty"


def test_process_restart_rejects_wrong_strategy():
    h = ProcessRestartHandler(
        catalog_dispatch=lambda a, p: RestoreResult(ok=True, code=None, snapshot_id="snap")
    )
    out = h(RestoreStrategy.REDRIVE, {})
    assert out.ok is False
    assert out.code is RecoveryErrorCode.RESTORE_NOT_AVAILABLE


# ---------------------------------------------------------------------------
# E.1.4 — ReobserveReplanHandler
# ---------------------------------------------------------------------------


def test_reobserve_replan_handler_succeeds():
    h = ReobserveReplanHandler()
    out = h(
        RestoreStrategy.REOBSERVE_REPLAN,
        _payload(_failure(), _plan(RestoreStrategy.REOBSERVE_REPLAN)),
    )
    assert out.ok is True
    assert out.snapshot_id == "snap-reobserve"


def test_reobserve_replan_rejects_wrong_strategy():
    h = ReobserveReplanHandler()
    out = h(RestoreStrategy.REDRIVE, {})
    assert out.ok is False
    assert out.code is RecoveryErrorCode.RESTORE_NOT_AVAILABLE


# ---------------------------------------------------------------------------
# E.1.5 — RestoreDispatch factory
# ---------------------------------------------------------------------------


def test_restore_dispatch_returns_redrive_for_redrive_strategy():
    d = RestoreDispatch()
    h = d.handler_for(RestoreStrategy.REDRIVE)
    out = h(RestoreStrategy.REDRIVE, {})
    assert out.ok is True


def test_restore_dispatch_returns_reconnect_for_reconnect_strategy():
    d = RestoreDispatch()
    h = d.handler_for(RestoreStrategy.RECONNECT)
    out = h(RestoreStrategy.RECONNECT, {})
    # No session_reconnect injected -> RESTORE_LOCK_VERIFY_FAILED.
    assert out.ok is False
    assert out.code is RecoveryErrorCode.RESTORE_LOCK_VERIFY_FAILED


def test_restore_dispatch_returns_process_restart_for_process_restart():
    d = RestoreDispatch(
        catalog_dispatch=lambda a, p: RestoreResult(ok=True, code=None, snapshot_id="snap"),
    )
    h = d.handler_for(RestoreStrategy.PROCESS_RESTART)
    out = h(
        RestoreStrategy.PROCESS_RESTART,
        _payload(
            _failure(),
            _plan(RestoreStrategy.PROCESS_RESTART, steps=("only",)),
        ),
    )
    assert out.ok is True
    assert out.snapshot_id == "snap-restart"


def test_restore_dispatch_returns_reobserve_for_replan():
    d = RestoreDispatch()
    h = d.handler_for(RestoreStrategy.REOBSERVE_REPLAN)
    out = h(RestoreStrategy.REOBSERVE_REPLAN, {})
    assert out.ok is True
    assert out.snapshot_id == "snap-reobserve"


def test_restore_dispatch_caches_handlers():
    """Calling ``handler_for`` twice returns the same callable."""
    d = RestoreDispatch()
    a = d.handler_for(RestoreStrategy.REDRIVE)
    b = d.handler_for(RestoreStrategy.REDRIVE)
    assert a is b


# ---------------------------------------------------------------------------
# E.2.1 — TraceStoreAdapter
# ---------------------------------------------------------------------------


@pytest.fixture
def trace_store() -> TraceStore:
    """Fresh :class:`TraceStore` rooted in a tempdir."""
    with tempfile.TemporaryDirectory() as tmpdir:
        store = TraceStore(Path(tmpdir))
        store.open(trace_id="trace-recovery-test", create=True)
        yield store


def test_trace_adapter_forwards_recovery_requested(trace_store: TraceStore) -> None:
    adapter = TraceStoreAdapter(trace_store)
    evt = make_recovery_requested_event(
        step_id="step-1",
        branch_id="BR-002",
        parent_branch_id="BR-001",
        strategy=RestoreStrategy.PROCESS_RESTART,
        severity=RecoverySeverity.APPLICATION,
        attempts_so_far=0,
        replans_so_far=0,
    )
    adapter.forward([evt])
    events = trace_store.read_events()
    # Bootstrap emits trace_started; we add 1.
    assert len(events) >= 2
    last = events[-1]
    assert last.event_type is EventType.RECOVERY_REQUESTED
    assert last.step_id == "step-1"
    assert last.payload["strategy"] == "process_restart"


def test_trace_adapter_forwards_recovery_result(trace_store: TraceStore) -> None:
    adapter = TraceStoreAdapter(trace_store)
    evt = make_recovery_result_event(
        step_id="step-1",
        branch_id="BR-002",
        parent_branch_id="BR-001",
        strategy=RestoreStrategy.REDRIVE,
        status=RecoveryStatus.SUCCESS,
        attempts=1,
        elapsed_ms=42,
        error_code=None,
        detail="",
    )
    adapter.forward([evt])
    events = trace_store.read_events()
    last = events[-1]
    assert last.event_type is EventType.RECOVERY_RESULT
    assert last.payload["status"] == "success"
    assert last.payload["elapsed_ms"] == 42


def test_trace_adapter_forwards_full_cycle(trace_store: TraceStore) -> None:
    """A typical cycle: requested + result."""
    adapter = TraceStoreAdapter(trace_store)
    requested = make_recovery_requested_event(
        step_id="step-1",
        branch_id="BR-002",
        parent_branch_id="BR-001",
        strategy=RestoreStrategy.REDRIVE,
        severity=RecoverySeverity.PAGE,
        attempts_so_far=0,
        replans_so_far=0,
    )
    result = make_recovery_result_event(
        step_id="step-1",
        branch_id="BR-002",
        parent_branch_id="BR-001",
        strategy=RestoreStrategy.REDRIVE,
        status=RecoveryStatus.SUCCESS,
        attempts=1,
        elapsed_ms=10,
        error_code=None,
        detail="",
    )
    adapter.forward([requested, result])

    events = trace_store.read_events()
    types = [e.event_type for e in events[-2:]]
    assert types == [EventType.RECOVERY_REQUESTED, EventType.RECOVERY_RESULT]


def test_trace_adapter_skips_unknown_event_type(trace_store: TraceStore) -> None:
    adapter = TraceStoreAdapter(trace_store)
    bogus = RequestedEvent(
        event_type="bogus_event_type",
        payload=RecoveryRequestedPayload(
            step_id="step-1",
            branch_id="BR-002",
            parent_branch_id="BR-001",
            strategy=RestoreStrategy.REDRIVE,
            severity=RecoverySeverity.PAGE,
            attempts_so_far=0,
            replans_so_far=0,
        ),
    )
    forwarded = adapter.forward([bogus])
    assert forwarded == []


def test_trace_adapter_strict_raises_on_unknown(trace_store: TraceStore) -> None:
    adapter = TraceStoreAdapter(trace_store)
    bogus = RequestedEvent(
        event_type="bogus_event_type",
        payload=RecoveryRequestedPayload(
            step_id="step-1",
            branch_id="BR-002",
            parent_branch_id="BR-001",
            strategy=RestoreStrategy.REDRIVE,
            severity=RecoverySeverity.PAGE,
            attempts_so_far=0,
            replans_so_far=0,
        ),
    )
    with pytest.raises(ValueError, match="unknown recovery event type"):
        adapter.forward_strict([bogus])


def test_trace_adapter_persists_to_jsonl(trace_store: TraceStore) -> None:
    """Round-trip: events written by the adapter are JSON-parseable
    on disk (so trace consumer / projection tooling can read them).
    """
    adapter = TraceStoreAdapter(trace_store)
    adapter.forward([
        make_recovery_result_event(
            step_id="step-1",
            branch_id="BR-002",
            parent_branch_id="BR-001",
            strategy=RestoreStrategy.RECONNECT,
            status=RecoveryStatus.FAILED,
            attempts=2,
            elapsed_ms=100,
            error_code="restore_lock_verify_failed",
            detail="lock not acquired",
        ),
    ])
    events_path = trace_store._state.events_path  # type: ignore[attr-defined]
    lines = events_path.read_text().strip().splitlines()
    last = json.loads(lines[-1])
    assert last["event_type"] == "recovery_result"
    assert last["payload"]["error_code"] == "restore_lock_verify_failed"
    assert last["payload"]["detail"] == "lock not acquired"