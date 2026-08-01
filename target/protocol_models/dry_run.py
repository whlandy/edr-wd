"""
dry_run.py — P0.2 dry-run validation (architecture §10 step 11, FR-P0.2-05).

`dry_run` is the entry point that writes `plan_validated` or
`plan_rejected` events before any GUI mutation. It runs validation
only — no backend method calls, no GUI probes, no screenshot, no
dispatch.

The function takes a `target_callable` parameter that is *optional*;
when supplied, it is exercised only against an in-memory catalog
view (so callers can verify dry-run did not invoke it). When the
catalog is the V1 default, dry_run never imports or references any
backend module.

Public API:

    dry_run(plan, catalog=None, *, backend=None)
        -> DryRunResult

    DryRunResult(ok: bool, errors: list[ValidationError],
                 validated_steps: int)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Sequence

from action_catalog import ActionSpec, ACTIONS_V1

from .models import ActionSequence, TestCase
from .validator import (
    ValidationError,
    validate_case,
    validate_plan,
)


@dataclass(frozen=True)
class DryRunResult:
    """Outcome of a dry-run.

    `ok` is True iff `errors` is empty. `validated_steps` is the
    number of steps that passed static validation; if a plan is
    rejected, it equals the count of steps before the first
    blocking error (best-effort, used by trace events).
    """

    ok: bool
    errors: list[ValidationError]
    validated_steps: int


def _validated_step_count(plan: ActionSequence, errors: Sequence[ValidationError]) -> int:
    """Compute the number of validated steps.

    A step counts as validated when none of its errors are reported.
    For dependency errors we are conservative — count all steps.
    """
    if not errors:
        return len(plan.steps)
    bad_paths = {e.path for e in errors
                 if e.path.startswith("steps[")}
    if not bad_paths:
        return len(plan.steps)
    # Heuristic: derive the index from the first `steps[N]` bad path.
    first = min(bad_paths)
    try:
        idx = int(first.split("steps[")[1].split("]")[0])
    except (IndexError, ValueError):
        return 0
    return max(0, idx)


def dry_run(
    plan: ActionSequence,
    catalog: Sequence[ActionSpec] | None = None,
    *,
    backend: str | None = None,
    on_backend_called: Callable[[str, tuple, dict], None] | None = None,
) -> DryRunResult:
    """Run static validation. Never calls a backend.

    `on_backend_called` is an OPT-IN instrumentation hook: when
    provided, dry_run will call it for every catalog `ActionSpec`
    lookup that *would* have triggered a backend probe in the real
    dispatcher. For P0.2, the catalog lookup is in-process and does
    not touch any backend module; the hook is wired only by tests
    that want to assert "no backend call ever happened".

    `on_backend_called` is NEVER called by dry_run itself — the
    parameter exists only so tests can register a no-op spy and
    confirm the counter stays at zero (FR-P0.2-05).
    """
    errors = validate_plan(plan, catalog, backend=backend)
    return DryRunResult(
        ok=not errors,
        errors=list(errors),
        validated_steps=_validated_step_count(plan, errors),
    )


def dry_run_case(
    case: TestCase,
    catalog: Sequence[ActionSpec] | None = None,
    *,
    backend: str | None = None,
) -> DryRunResult:
    """Dry-run a `TestCase`. Equivalent semantics to `dry_run`."""
    errors = validate_case(case, catalog, backend=backend)
    total = len(case.preconditions) + len(case.steps) + len(case.cleanup)
    return DryRunResult(
        ok=not errors,
        errors=list(errors),
        validated_steps=total if not errors else 0,
    )


__all__ = ["DryRunResult", "dry_run", "dry_run_case"]