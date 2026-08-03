"""test_projection.py — P2.2 projection + trace.md tests (Commit G).

These tests cover Commit G.1 (:class:`StepResultsProjection`)
and Commit G.2 (:class:`TraceMarkdownProjection`), plus the
:class:`ProjectionRegistry` integration.

Round 2 review #5: step-results / trace.md projection.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from agent.execution.branch_ancestry import BranchRegistry
from agent.execution.branch_events import make_branch_created_event
from agent.execution.projection import (
    ProjectionRegistry,
    StepResultsPayload,
    StepResultsProjection,
    load_projection_payload,
    write_projection,
)
from agent.execution.branch_events import (
    make_branch_created_event,
    make_replan_created_event,
)
from agent.execution.recovery import Branch, RecoverySeverity
from agent.execution.recovery_events import (
    make_recovery_requested_event,
    make_recovery_result_event,
)
from agent.execution.recovery import (
    ReplanState,
    RestoreStrategy,
    RecoveryStatus,
)
from agent.execution.trace_adapter import TraceStoreAdapter
from agent.execution.trace_md import TraceMarkdownProjection
from agent.trace.events import TraceEvent
from agent.trace.store import TraceStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def trace_store() -> TraceStore:
    with tempfile.TemporaryDirectory() as tmpdir:
        store = TraceStore(Path(tmpdir))
        store.open(trace_id="trace-g-test", create=True)
        yield store


@pytest.fixture
def branch_registry() -> BranchRegistry:
    reg = BranchRegistry()
    reg.register(Branch(
        branch_id="BR-001",
        forked_from_event_id="ev-fork-1",
        forked_from_checkpoint_id="cp-1",
        parent_branch_id=None,
        head_event_id="ev-head-1",
    ))
    reg.register(Branch(
        branch_id="BR-002",
        forked_from_event_id="ev-fork-2",
        forked_from_checkpoint_id="cp-2",
        parent_branch_id="BR-001",
        head_event_id="ev-head-2",
    ))
    return reg


# ---------------------------------------------------------------------------
# G.1.1 — StepResultsPayload
# ---------------------------------------------------------------------------


def test_step_results_payload_to_dict():
    p = StepResultsPayload(
        step_id="step-1",
        status="recovered",
        attempts=2,
        recovery_branch_id="BR-002",
        error_code=None,
        detail="redrive succeeded",
        elapsed_ms=100,
    )
    d = p.to_dict()
    assert d == {
        "step_id": "step-1",
        "status": "recovered",
        "attempts": 2,
        "recovery_branch_id": "BR-002",
        "error_code": None,
        "detail": "redrive succeeded",
        "elapsed_ms": 100,
    }


def test_step_results_payload_is_frozen():
    p = StepResultsPayload(
        step_id="step-1",
        status="passed",
        attempts=1,
        recovery_branch_id=None,
        error_code=None,
        detail="",
        elapsed_ms=10,
    )
    with pytest.raises((AttributeError, TypeError)):
        p.step_id = "step-2"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# G.1.2 — StepResultsProjection
# ---------------------------------------------------------------------------


def test_step_results_projection_with_no_events():
    p = StepResultsProjection()
    result = p.project([], branch_registry=BranchRegistry())
    assert result.name == "step_results"
    assert result.payload["schema"] == "step-results/v1"
    assert result.payload["steps"] == {}


def test_step_results_projection_with_step_completed_only():
    from agent.trace.events import EventType

    store = TraceStore(Path(tempfile.mkdtemp()))
    store.open(trace_id="trace-x", create=True)
    store.append_dict(
        EventType.STEP_COMPLETED,
        payload={"status": "passed", "error_code": None, "detail": "ok"},
        step_id="step-1",
    )
    events = store.read_events()
    p = StepResultsProjection()
    result = p.project(events, branch_registry=BranchRegistry())
    steps = result.payload["steps"]  # type: ignore[index]
    assert "step-1" in steps
    assert steps["step-1"]["status"] == "passed"
    assert steps["step-1"]["attempts"] == 1
    assert steps["step-1"]["recovery_branch_id"] is None


def test_step_results_projection_with_recovery_success():
    """Recovery succeeded -> status = 'recovered', attempts > 1."""
    from agent.trace.events import EventType

    store = TraceStore(Path(tempfile.mkdtemp()))
    store.open(trace_id="trace-recov", create=True)
    adapter = TraceStoreAdapter(store)
    adapter.forward([
        make_recovery_requested_event(
            step_id="step-1",
            branch_id="BR-002",
            parent_branch_id="BR-001",
            strategy=RestoreStrategy.REDRIVE,
            severity=RecoverySeverity.PAGE,
            attempts_so_far=0,
            replans_so_far=0,
        ),
        make_recovery_result_event(
            step_id="step-1",
            branch_id="BR-002",
            parent_branch_id="BR-001",
            strategy=RestoreStrategy.REDRIVE,
            status=RecoveryStatus.SUCCESS,
            attempts=1,
            elapsed_ms=42,
            error_code=None,
            detail="",
        ),
    ])
    events = store.read_events()
    p = StepResultsProjection()
    result = p.project(events, branch_registry=BranchRegistry())
    steps = result.payload["steps"]  # type: ignore[index]
    assert steps["step-1"]["status"] == "recovered"
    assert steps["step-1"]["recovery_branch_id"] == "BR-002"


def test_step_results_projection_with_recovery_failure():
    from agent.trace.events import EventType

    store = TraceStore(Path(tempfile.mkdtemp()))
    store.open(trace_id="trace-fail", create=True)
    adapter = TraceStoreAdapter(store)
    adapter.forward([
        make_recovery_requested_event(
            step_id="step-1",
            branch_id="BR-002",
            parent_branch_id="BR-001",
            strategy=RestoreStrategy.RECONNECT,
            severity=RecoverySeverity.SESSION,
            attempts_so_far=0,
            replans_so_far=0,
        ),
        make_recovery_result_event(
            step_id="step-1",
            branch_id="BR-002",
            parent_branch_id="BR-001",
            strategy=RestoreStrategy.RECONNECT,
            status=RecoveryStatus.FAILED,
            attempts=3,
            elapsed_ms=200,
            error_code="restore_lock_verify_failed",
            detail="lock not acquired",
        ),
    ])
    events = store.read_events()
    p = StepResultsProjection()
    result = p.project(events, branch_registry=BranchRegistry())
    steps = result.payload["steps"]  # type: ignore[index]
    assert steps["step-1"]["status"] == "failed"
    assert steps["step-1"]["attempts"] == 3
    assert steps["step-1"]["error_code"] == "restore_lock_verify_failed"
    assert steps["step-1"]["detail"] == "lock not acquired"


def test_step_results_projection_with_replan():
    """REOBSERVE_REPLAN success -> status = 'replanned'."""
    store = TraceStore(Path(tempfile.mkdtemp()))
    store.open(trace_id="trace-replan", create=True)
    adapter = TraceStoreAdapter(store)
    adapter.forward([
        make_recovery_requested_event(
            step_id="step-1",
            branch_id="BR-002",
            parent_branch_id="BR-001",
            strategy=RestoreStrategy.REOBSERVE_REPLAN,
            severity=RecoverySeverity.WINDOW,
            attempts_so_far=0,
            replans_so_far=0,
        ),
        make_recovery_result_event(
            step_id="step-1",
            branch_id="BR-002",
            parent_branch_id="BR-001",
            strategy=RestoreStrategy.REOBSERVE_REPLAN,
            status=RecoveryStatus.REPLANNED,
            attempts=1,
            elapsed_ms=15,
            error_code=None,
            detail="",
        ),
    ])
    events = store.read_events()
    p = StepResultsProjection()
    result = p.project(events, branch_registry=BranchRegistry())
    steps = result.payload["steps"]  # type: ignore[index]
    assert steps["step-1"]["status"] == "replanned"


def test_step_results_projection_writes_atomically():
    store = TraceStore(Path(tempfile.mkdtemp()))
    store.open(trace_id="trace-atomic", create=True)
    adapter = TraceStoreAdapter(store)
    adapter.forward([
        make_recovery_result_event(
            step_id="step-1",
            branch_id="BR-002",
            parent_branch_id="BR-001",
            strategy=RestoreStrategy.REDRIVE,
            status=RecoveryStatus.SUCCESS,
            attempts=1,
            elapsed_ms=10,
            error_code=None,
            detail="",
        ),
    ])
    events = store.read_events()
    p = StepResultsProjection()
    result = p.project(events, branch_registry=BranchRegistry())

    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "step-results.json"
        write_projection(path, result)
        assert path.exists()
        # Atomic write: no .tmp left behind.
        leftover = list(path.parent.glob("*.tmp"))
        assert leftover == []
        loaded = load_projection_payload(path)
        assert loaded == result.payload


# ---------------------------------------------------------------------------
# G.1.3 — ProjectionRegistry
# ---------------------------------------------------------------------------


def test_projection_registry_register_and_get():
    reg = ProjectionRegistry()
    p = StepResultsProjection()
    reg.register(p)
    assert reg.get("step_results") is p
    assert reg.names() == ("step_results",)


def test_projection_registry_unknown_raises():
    reg = ProjectionRegistry()
    with pytest.raises(KeyError, match="not registered"):
        reg.get("nonexistent")


def test_projection_registry_project_all():
    store = TraceStore(Path(tempfile.mkdtemp()))
    store.open(trace_id="trace-all", create=True)
    adapter = TraceStoreAdapter(store)
    adapter.forward([
        make_recovery_result_event(
            step_id="step-1",
            branch_id="BR-002",
            parent_branch_id="BR-001",
            strategy=RestoreStrategy.REDRIVE,
            status=RecoveryStatus.SUCCESS,
            attempts=1,
            elapsed_ms=10,
            error_code=None,
            detail="",
        ),
    ])
    events = store.read_events()

    reg = ProjectionRegistry()
    reg.register(StepResultsProjection())
    reg.register(TraceMarkdownProjection())

    results = reg.project_all(events, branch_registry=BranchRegistry())
    assert set(results.keys()) == {"step_results", "trace_md"}
    assert results["step_results"].payload["steps"]["step-1"]["status"] == "recovered"
    assert "Recovery Cycle" in results["trace_md"].payload["content"]


# ---------------------------------------------------------------------------
# G.2.1 — TraceMarkdownProjection
# ---------------------------------------------------------------------------


def test_trace_md_projection_with_no_events():
    p = TraceMarkdownProjection()
    result = p.project([], branch_registry=BranchRegistry())
    md = result.payload["content"]  # type: ignore[index]
    assert "# Trace" in md
    assert "## Recovery Cycle" in md
    assert "## Branch Lineage" in md
    assert "No recovery cycles recorded." in md
    assert "No branches recorded." in md


def test_trace_md_recovery_cycle_table():
    store = TraceStore(Path(tempfile.mkdtemp()))
    store.open(trace_id="trace-md-rec", create=True)
    adapter = TraceStoreAdapter(store)
    adapter.forward([
        make_recovery_requested_event(
            step_id="step-1",
            branch_id="BR-002",
            parent_branch_id="BR-001",
            strategy=RestoreStrategy.REDRIVE,
            severity=RecoverySeverity.PAGE,
            attempts_so_far=0,
            replans_so_far=0,
        ),
        make_recovery_result_event(
            step_id="step-1",
            branch_id="BR-002",
            parent_branch_id="BR-001",
            strategy=RestoreStrategy.REDRIVE,
            status=RecoveryStatus.SUCCESS,
            attempts=1,
            elapsed_ms=42,
            error_code=None,
            detail="",
        ),
    ])
    events = store.read_events()
    p = TraceMarkdownProjection()
    result = p.project(events, branch_registry=BranchRegistry())
    md = result.payload["content"]  # type: ignore[index]
    assert "### Step `step-1`" in md
    assert "`recovery_requested`" in md
    assert "`recovery_result`" in md
    assert "| event_type | branch_id | payload |" in md


def test_trace_md_replan_in_recovery_cycle():
    store = TraceStore(Path(tempfile.mkdtemp()))
    store.open(trace_id="trace-md-replan", create=True)
    adapter = TraceStoreAdapter(store)
    adapter.forward([
        make_recovery_requested_event(
            step_id="step-1",
            branch_id="BR-002",
            parent_branch_id="BR-001",
            strategy=RestoreStrategy.REOBSERVE_REPLAN,
            severity=RecoverySeverity.WINDOW,
            attempts_so_far=0,
            replans_so_far=0,
        ),
        make_recovery_result_event(
            step_id="step-1",
            branch_id="BR-002",
            parent_branch_id="BR-001",
            strategy=RestoreStrategy.REOBSERVE_REPLAN,
            status=RecoveryStatus.REPLANNED,
            attempts=1,
            elapsed_ms=10,
            error_code=None,
            detail="",
        ),
        make_replan_created_event(
            branch_id="BR-002",
            parent_branch_id="BR-001",
            strategy=RestoreStrategy.REOBSERVE_REPLAN,
            replan_state=ReplanState.AVAILABLE,
            step_id="step-1",
            detail="need to reobserve",
        ),
    ])
    events = store.read_events()
    p = TraceMarkdownProjection()
    result = p.project(events, branch_registry=BranchRegistry())
    md = result.payload["content"]  # type: ignore[index]
    assert "`replan_created`" in md


def test_trace_md_branch_lineage_tree(branch_registry: BranchRegistry):
    store = TraceStore(Path(tempfile.mkdtemp()))
    store.open(trace_id="trace-md-lineage", create=True)
    adapter = TraceStoreAdapter(store)
    adapter.forward([
        make_branch_created_event(Branch(
            branch_id="BR-001",
            forked_from_event_id="ev-1",
            forked_from_checkpoint_id="cp-1",
            parent_branch_id=None,
            head_event_id="ev-h-1",
        )),
        make_branch_created_event(Branch(
            branch_id="BR-002",
            forked_from_event_id="ev-2",
            forked_from_checkpoint_id="cp-2",
            parent_branch_id="BR-001",
            head_event_id="ev-h-2",
        )),
    ])
    events = store.read_events()
    p = TraceMarkdownProjection()
    result = p.project(events, branch_registry=branch_registry)
    md = result.payload["content"]  # type: ignore[index]
    assert "**BR-001**" in md
    assert "**BR-002**" in md
    assert "└─ " in md  # Child branch uses indent marker.


def test_trace_md_branch_lineage_empty_registry_with_events():
    """Events present but registry empty -> 'No branches recorded.'"""
    store = TraceStore(Path(tempfile.mkdtemp()))
    store.open(trace_id="trace-md-empty-reg", create=True)
    adapter = TraceStoreAdapter(store)
    adapter.forward([
        make_recovery_requested_event(
            step_id="step-1",
            branch_id="BR-002",
            parent_branch_id="BR-001",
            strategy=RestoreStrategy.REDRIVE,
            severity=RecoverySeverity.PAGE,
            attempts_so_far=0,
            replans_so_far=0,
        ),
    ])
    events = store.read_events()
    p = TraceMarkdownProjection()
    result = p.project(events, branch_registry=BranchRegistry())
    md = result.payload["content"]  # type: ignore[index]
    # Recovery events render the cycle subsection, but lineage
    # subsection is empty (registry has nothing).
    assert "### Step `step-1`" in md
    assert "No branches recorded." in md


def test_trace_md_full_cycle_writes_to_disk():
    """Full E2E: events + registry + projection + write_projection."""
    store = TraceStore(Path(tempfile.mkdtemp()))
    store.open(trace_id="trace-md-full", create=True)
    adapter = TraceStoreAdapter(store)
    adapter.forward([
        make_branch_created_event(Branch(
            branch_id="BR-001",
            forked_from_event_id="ev-1",
            forked_from_checkpoint_id="cp-1",
            parent_branch_id=None,
            head_event_id="ev-h-1",
        )),
        make_recovery_requested_event(
            step_id="step-1",
            branch_id="BR-001",
            parent_branch_id=None,
            strategy=RestoreStrategy.REDRIVE,
            severity=RecoverySeverity.PAGE,
            attempts_so_far=0,
            replans_so_far=0,
        ),
        make_recovery_result_event(
            step_id="step-1",
            branch_id="BR-001",
            parent_branch_id=None,
            strategy=RestoreStrategy.REDRIVE,
            status=RecoveryStatus.SUCCESS,
            attempts=1,
            elapsed_ms=10,
            error_code=None,
            detail="ok",
        ),
    ])
    events = store.read_events()
    registry = BranchRegistry()
    registry.register(Branch(
        branch_id="BR-001",
        forked_from_event_id="ev-1",
        forked_from_checkpoint_id="cp-1",
        parent_branch_id=None,
        head_event_id="ev-h-1",
    ))
    p = TraceMarkdownProjection()
    result = p.project(events, branch_registry=registry)

    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "trace.md"
        write_projection(path, result)
        assert path.exists()
        text = path.read_text()
        assert "# Trace" in text
        assert "## Recovery Cycle" in text
        assert "## Branch Lineage" in text
        # Round-trip via JSON-loadable payload.
        loaded = json.loads(json.dumps(result.payload))
        assert loaded["schema"] == "trace.md/v1"