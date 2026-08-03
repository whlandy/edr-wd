"""projection.py — Trace → projection layer (P2.2 — Commit G.1).

Round 2 review #5 (projections) requires the recovery cycle
to be visible in the existing ``step-results.json`` and in
a new ``trace.md`` file.

Commit G.1 introduces the projection layer:

    TraceStore.read_events()
        |
        v
    ProjectionProtocol.project(events, registry, branch_filter)
        |
        v
    ProjectionResult (data + outputs)

Commit G.1 implements:

    * :class:`StepResultsProjection` — produces a
      :class:`StepResultsPayload` keyed by step_id, including
      recovery cycle metadata.
    * :class:`ProjectionRegistry` — extensible registry for
      alternative projections (the trace.md writer is a
      separate projection in Commit G.2).

Design doc: ``docs/requirements/P2-recovery-planner.md`` §4.4
(projection producer boundary).

Boundary:

    * Projections do **not** call :class:`TraceStore.append_dict`.
      They consume events; they do not write them.
    * Projections do **not** call :class:`RecoveryExecutor`.
      They are downstream of the trace store.
    * Projections do **not** mutate the
      :class:`BranchRegistry` — they only read it for
      lineage queries.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Protocol, runtime_checkable

from agent.execution.branch_ancestry import BranchRegistry
from agent.execution.step_results import write_atomic
from agent.execution.trace_payload import TracePayload
from agent.trace.events import EventType, TraceEvent


# ---------------------------------------------------------------------------
# Projection protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class ProjectionProtocol(Protocol):
    """A projection consumes :class:`TraceEvent` and produces
    a :class:`ProjectionResult`.

    Projections are stateless; the same instance can be reused
    for multiple calls.

    Implementations must:

    * Be deterministic (same input -> same output).
    * Not mutate the input events.
    * Not raise on missing events (the trace may be partial).
    """

    name: str

    def project(
        self,
        events: list[TraceEvent],
        *,
        branch_registry: BranchRegistry,
    ) -> "ProjectionResult":
        ...


# ---------------------------------------------------------------------------
# Result envelope
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProjectionResult:
    """Output of a projection.

    Attributes:
        name: The projection's name (matches
            :attr:`ProjectionProtocol.name`).
        payload: The projection data as a JSON-serializable
            mapping.
    """

    name: str
    payload: Mapping[str, object]


# ---------------------------------------------------------------------------
# StepResultsPayload — typed payload
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StepResultsPayload:
    """Per-step outcome including recovery cycle metadata.

    Attributes:
        step_id: The step identifier.
        status: One of ``"passed"``, ``"failed"``,
            ``"recovered"``, ``"replanned"``.
        attempts: Number of attempts (1 for non-recovery steps).
        recovery_branch_id: The branch id used for recovery
            (``None`` if no recovery occurred).
        error_code: Machine-friendly code (set on failure).
        detail: Human-readable detail.
        elapsed_ms: Wall-clock duration of the step (including
            recovery, if any).
    """

    step_id: str
    status: str
    attempts: int
    recovery_branch_id: str | None
    error_code: str | None
    detail: str
    elapsed_ms: int

    def to_dict(self) -> dict[str, object]:
        return {
            "step_id": self.step_id,
            "status": self.status,
            "attempts": self.attempts,
            "recovery_branch_id": self.recovery_branch_id,
            "error_code": self.error_code,
            "detail": self.detail,
            "elapsed_ms": self.elapsed_ms,
        }


# ---------------------------------------------------------------------------
# StepResultsProjection
# ---------------------------------------------------------------------------


class StepResultsProjection:
    """Produces per-step outcomes with recovery cycle metadata.

    Algorithm:

    1. Group events by ``step_id``.
    2. For each step, find the latest :attr:`EventType.STEP_RESULT`
       (if any) — that's the canonical outcome.
    3. If recovery events exist for the step:

       * ``recovery_result.status == "success"`` → ``status = "recovered"``
       * ``recovery_result.status == "replanned"`` → ``status = "replanned"``
       * Otherwise the step failed.

    4. The ``attempts`` count is taken from the recovery cycle
       (``attempts`` field on the recovery_result event).
    5. ``recovery_branch_id`` is taken from the recovery_requested
       event's ``branch_id`` field.

    Boundary: this projection reads events only; it never
    appends to the trace store.
    """

    name: str = "step_results"

    def project(
        self,
        events: list[TraceEvent],
        *,
        branch_registry: BranchRegistry,
    ) -> ProjectionResult:
        # ``branch_registry`` is currently unused for this
        # projection (step-results is keyed by step_id, not
        # branch). The signature reserves the slot for future
        # lineage-aware projections.
        del branch_registry

        by_step: dict[str, list[TraceEvent]] = {}
        for event in events:
            step_id = event.step_id
            if step_id is None:
                continue
            by_step.setdefault(step_id, []).append(event)

        results: dict[str, object] = {}
        for step_id, step_events in by_step.items():
            results[step_id] = self._project_step(step_events)

        payload: dict[str, object] = {
            "schema": "step-results/v1",
            "steps": results,
        }
        return ProjectionResult(name=self.name, payload=payload)

    def _project_step(
        self,
        events: list[TraceEvent],
    ) -> dict[str, object]:
        # Walk events in order; collect recovery context.
        step_completed_evt: TraceEvent | None = None
        recovery_requested_evt: TraceEvent | None = None
        recovery_result_evt: TraceEvent | None = None
        elapsed_ms = 0

        for event in events:
            et = event.event_type
            if et is EventType.STEP_COMPLETED:
                step_completed_evt = event
            elif et is EventType.RECOVERY_REQUESTED:
                recovery_requested_evt = event
            elif et is EventType.RECOVERY_RESULT:
                recovery_result_evt = event

        # Determine status.
        if recovery_result_evt is not None:
            rec_status = recovery_result_evt.payload.get("status", "")
            if rec_status == "success":
                status = "recovered"
            elif rec_status == "replanned":
                status = "replanned"
            elif rec_status == "failed":
                status = "failed"
            else:
                status = "failed"
        elif step_completed_evt is not None:
            sr_status = step_completed_evt.payload.get("status", "")
            status = sr_status if sr_status in ("passed", "failed") else "unknown"
        else:
            status = "unknown"

        attempts = (
            int(recovery_result_evt.payload.get("attempts", 1))
            if recovery_result_evt is not None
            else 1
        )
        recovery_branch_id: str | None = (
            recovery_requested_evt.payload.get("branch_id")
            if recovery_requested_evt is not None
            else None
        )
        error_code: str | None = (
            recovery_result_evt.payload.get("error_code")
            if recovery_result_evt is not None
            else step_completed_evt.payload.get("error_code")
            if step_completed_evt is not None
            else None
        )
        detail: str = (
            recovery_result_evt.payload.get("detail", "")
            if recovery_result_evt is not None
            else step_completed_evt.payload.get("detail", "")
            if step_completed_evt is not None
            else ""
        )
        if recovery_result_evt is not None:
            elapsed_ms = int(recovery_result_evt.payload.get("elapsed_ms", 0))

        # Resolve step_id: prefer step_completed, fall back to
        # recovery events, then "unknown".
        resolved_step_id = "unknown"
        if step_completed_evt is not None and step_completed_evt.step_id is not None:
            resolved_step_id = step_completed_evt.step_id
        elif recovery_result_evt is not None and recovery_result_evt.step_id is not None:
            resolved_step_id = recovery_result_evt.step_id
        elif recovery_requested_evt is not None and recovery_requested_evt.step_id is not None:
            resolved_step_id = recovery_requested_evt.step_id

        payload = StepResultsPayload(
            step_id=resolved_step_id,
            status=status,
            attempts=attempts,
            recovery_branch_id=(
                recovery_branch_id
                if isinstance(recovery_branch_id, str)
                else None
            ),
            error_code=(
                error_code if isinstance(error_code, str) else None
            ),
            detail=str(detail),
            elapsed_ms=elapsed_ms,
        )
        return payload.to_dict()


# ---------------------------------------------------------------------------
# ProjectionRegistry
# ---------------------------------------------------------------------------


@dataclass
class ProjectionRegistry:
    """Extensible registry of projections.

    Use::

        registry = ProjectionRegistry()
        registry.register(StepResultsProjection())
        registry.register(TraceMarkdownProjection())

        results = registry.project_all(events, branch_registry=registry)

    Multiple projections share one :class:`BranchRegistry`
    instance so lineage queries are consistent.
    """

    _projections: dict[str, ProjectionProtocol] = field(
        default_factory=dict, init=False, repr=False,
    )

    def register(self, projection: ProjectionProtocol) -> None:
        self._projections[projection.name] = projection

    def get(self, name: str) -> ProjectionProtocol:
        try:
            return self._projections[name]
        except KeyError as e:
            raise KeyError(f"projection {name!r} not registered") from e

    def names(self) -> tuple[str, ...]:
        return tuple(self._projections.keys())

    def project_all(
        self,
        events: list[TraceEvent],
        *,
        branch_registry: BranchRegistry,
    ) -> dict[str, ProjectionResult]:
        return {
            name: proj.project(events, branch_registry=branch_registry)
            for name, proj in self._projections.items()
        }


# ---------------------------------------------------------------------------
# Convenience: write a projection result to disk
# ---------------------------------------------------------------------------


def write_projection(
    path: Path,
    result: ProjectionResult,
) -> Path:
    """Write a :class:`ProjectionResult` to ``path`` atomically.

    Used by Commit G.2's trace.md writer and by callers that
    want to persist the step-results.json payload.
    """
    return write_atomic(path, result.payload)


def load_projection_payload(path: Path) -> dict[str, object] | None:
    """Read a projection file's payload. Returns None if absent."""
    path = Path(path)
    if not path.exists():
        return None
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


__all__ = [
    "ProjectionProtocol",
    "ProjectionResult",
    "StepResultsPayload",
    "StepResultsProjection",
    "ProjectionRegistry",
    "write_projection",
    "load_projection_payload",
]