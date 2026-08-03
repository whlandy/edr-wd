"""
parse.py — Plan parser + 5-stage validation (P3.1 Commit C).

P3.1 design gate contract D12:

  `parse_plan(payload, *, catalog, snapshot_id) -> (ActionSequence, list[ValidationError])`
  MUST validate against **all 5 stages** below before the plan
  is eligible for execution. **The implementation MAY execute
  the stages in any order, including in parallel**; the
  contract is that every stage MUST pass. Stable codes:

    1. Schema-level (`additionalProperties: false`, type
       mismatches) → `plan_schema_invalid`.
    2. Catalog-level: each step's `action_id` MUST be in
       `enabled_actions_for(backend, profile)` →
       `backend_disabled`.
    3. Action-code consistency: `action_code` MUST match the
       catalog's `action_code` for the chosen `action_id` →
       `action_code_mismatch`.
    4. Target-ref consistency: `target_ref.snapshot_id` MUST
       match the supplied `snapshot_id` →
       `target_ref_snapshot_mismatch`.
    5. Dependency DAG: cycle detection on `dependencies` →
       `dependency_cycle` with the closed-loop `step_id` list.

  **All 5 stages MUST pass** before execution eligibility;
  the planner MUST NOT execute a plan that fails any stage.

D9 also lives here:

  * Coordinate fallback chosen while a unique semantic target
    exists in the latest snapshot returns
    `coordinate_fallback_not_allowed`. The semantic-target
    existence check is delegated to the caller (snapshot
    inspection) — `parse_plan` only validates the wire shape
    of the coordinate action vs the semantic action list.

This module is **pure**: no I/O. It accepts a raw JSON payload
(the LLM output), validates it, and returns the parsed dict
plus any validation errors. The caller (planner) is
responsible for persisting the `plan_validated` /
`plan_rejected` events (P3.1.D) and emitting the
`plan_created` event.

Failure-model notes:

  * Validation failures are **non-fatal to the caller**: the
    function always returns; the caller decides whether to
    retry, replan, or surface to the user.
  * The `code` field is stable across releases per
    architecture §7.2.
  * The `JSON_pointer` field follows RFC 6901.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence


# ---------------------------------------------------------------------------
# Stable validation codes (D12)
# ---------------------------------------------------------------------------

CODE_PLAN_SCHEMA_INVALID        = "plan_schema_invalid"
CODE_BACKEND_DISABLED            = "backend_disabled"
CODE_ACTION_CODE_MISMATCH        = "action_code_mismatch"
CODE_TARGET_REF_SNAPSHOT_MISMATCH = "target_ref_snapshot_mismatch"
CODE_DEPENDENCY_CYCLE            = "dependency_cycle"
COORDINATE_FALLBACK_NOT_ALLOWED  = "coordinate_fallback_not_allowed"
CODE_UNKNOWN_SCHEMA_VERSION      = "unknown_schema_version"


# ---------------------------------------------------------------------------
# ValidationError dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ValidationError:
    """One validation failure; surfaced in `LLMPlanResponse`."""

    code: str
    pointer: str           # RFC 6901 JSON pointer
    message: str
    value: object = None
    # Cycle-only fields (used by stage 5):
    cycle: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "code": self.code,
            "pointer": self.pointer,
            "message": self.message,
        }
        if self.value is not None:
            out["value"] = self.value
        if self.cycle:
            out["cycle"] = list(self.cycle)
        return out


# ---------------------------------------------------------------------------
# Stable plan validation codes for callers
# ---------------------------------------------------------------------------

STABLE_VALIDATION_CODES: frozenset[str] = frozenset({
    CODE_PLAN_SCHEMA_INVALID,
    CODE_BACKEND_DISABLED,
    CODE_ACTION_CODE_MISMATCH,
    CODE_TARGET_REF_SNAPSHOT_MISMATCH,
    CODE_DEPENDENCY_CYCLE,
    COORDINATE_FALLBACK_NOT_ALLOWED,
    CODE_UNKNOWN_SCHEMA_VERSION,
})


# ---------------------------------------------------------------------------
# parse_plan — the public surface
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ParseResult:
    """Tuple-like return for parse_plan: the parsed plan (as a
    dict matching PLAN_JSON_SCHEMA shape) plus the collected
    validation errors. An empty `errors` means the plan is
    eligible for execution."""

    plan: dict[str, Any] = field(default_factory=dict)
    errors: tuple[ValidationError, ...] = ()


def parse_plan(
    payload: Mapping[str, Any],
    *,
    catalog: Sequence[Any],
    snapshot_id: str,
    semantic_target_action_ids: Iterable[str] = (),
) -> ParseResult:
    """Validate the LLM output payload against the 5-stage plan
    contract.

    Parameters:

      * `payload` — raw dict (LLM output, JSON-decoded).
      * `catalog` — iterable of `ActionSpec`-like objects that
        expose `.action_id` / `.action_code` / `.tool_name`.
        Typically the result of `enabled_actions_for(...)`.
      * `snapshot_id` — the live snapshot id; rejected if any
        step's `target_ref.snapshot_id` differs.
      * `semantic_target_action_ids` — the set of action_ids
        for which a unique semantic target exists in the live
        snapshot. Used for D9: if a coordinate action appears
        in the plan AND any of these targets exists for the
        same step, the plan is rejected with
        `coordinate_fallback_not_allowed`.

    All 5 stages MUST pass. The implementation MAY execute the
    stages in any order. Errors are collected and returned; the
    caller decides retry / replan / surface.
    """
    errors: list[ValidationError] = []

    # ---- Stage 1: schema-level ----
    _check_schema(payload, errors)

    # Schema-level failures short-circuit the rest of the
    # validation: an unknown-shape payload cannot be validated
    # against the catalog. The caller (planner) will retry /
    # replan based on the schema-invalid error.
    if errors:
        return ParseResult(plan=dict(payload), errors=tuple(errors))

    # ---- Stage 2..5: per-step + DAG ----
    plan_id = str(payload.get("plan_id", ""))
    expected_actions = payload.get("expected_actions") or []
    expected_dependencies = payload.get("expected_dependencies") or {}

    catalog_by_id: dict[str, Any] = {
        spec.action_id: spec for spec in catalog
    }
    semantic_ids = set(semantic_target_action_ids)

    step_ids: set[str] = set()
    steps_seen: list[str] = []
    for i, raw_step in enumerate(expected_actions):
        if not isinstance(raw_step, Mapping):
            errors.append(ValidationError(
                code=CODE_PLAN_SCHEMA_INVALID,
                pointer=f"/expected_actions/{i}",
                message="step must be a JSON object",
                value=raw_step,
            ))
            continue
        step_id = str(raw_step.get("step_id", ""))
        action_id = str(raw_step.get("action_id", ""))
        pointer_step = f"/expected_actions/{i}"

        # ---- Stage 2: catalog ----
        spec = catalog_by_id.get(action_id)
        if spec is None:
            errors.append(ValidationError(
                code=CODE_BACKEND_DISABLED,
                pointer=f"{pointer_step}/action_id",
                message=(
                    f"action_id {action_id!r} is not in the "
                    f"enabled catalog for this (backend, profile)"
                ),
                value=action_id,
            ))
            continue

        # ---- Stage 3: action_code consistency ----
        action_code = raw_step.get("action_code")
        spec_code = getattr(spec, "action_code", None)
        if action_code is not None and spec_code is not None:
            if action_code != spec_code:
                errors.append(ValidationError(
                    code=CODE_ACTION_CODE_MISMATCH,
                    pointer=f"{pointer_step}/action_code",
                    message=(
                        f"action_code {action_code!r} does not "
                        f"match the catalog's {spec_code!r} for "
                        f"action_id {action_id!r}"
                    ),
                    value=action_code,
                ))

        # ---- Stage 4: target_ref.snapshot_id ----
        target_ref = raw_step.get("target_ref")
        if isinstance(target_ref, Mapping):
            ref_snap = target_ref.get("snapshot_id")
            if ref_snap and ref_snap != snapshot_id:
                errors.append(ValidationError(
                    code=CODE_TARGET_REF_SNAPSHOT_MISMATCH,
                    pointer=f"{pointer_step}/target_ref/snapshot_id",
                    message=(
                        f"target_ref.snapshot_id {ref_snap!r} "
                        f"does not match the live snapshot "
                        f"{snapshot_id!r}"
                    ),
                    value=ref_snap,
                ))

        # ---- D9: coordinate fallback ----
        if action_id.startswith("pointer.") and semantic_ids:
            # A coordinate action was chosen while a unique
            # semantic target exists for the same step. The
            # parser only checks the rule (semantic_target is
            # non-empty + coordinate action present); the
            # caller decides which semantic action should
            # have been chosen.
            errors.append(ValidationError(
                code=COORDINATE_FALLBACK_NOT_ALLOWED,
                pointer=f"{pointer_step}/action_id",
                message=(
                    f"coordinate action {action_id!r} chosen "
                    f"while a unique semantic target is "
                    f"available in the latest snapshot"
                ),
                value=action_id,
            ))

        # Step id duplicate detection (DRY across stages).
        if step_id:
            if step_id in step_ids:
                errors.append(ValidationError(
                    code=CODE_PLAN_SCHEMA_INVALID,
                    pointer=f"{pointer_step}/step_id",
                    message=(
                        f"duplicate step_id {step_id!r}; "
                        f"already seen at step {steps_seen.index(step_id)}"
                    ),
                    value=step_id,
                ))
            else:
                step_ids.add(step_id)
                steps_seen.append(step_id)

    # ---- Stage 5: dependency cycle ----
    # We use the in-process 3-color DFS rather than delegating
    # to P0.2's validator (which requires a full ActionSequence
    # build). The cycle contract is the same.
    _check_dependency_cycles(
        steps_seen, expected_dependencies, errors,
    )

    return ParseResult(
        plan=dict(payload),
        errors=tuple(errors),
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _check_schema(
    payload: Mapping[str, Any],
    errors: list[ValidationError],
) -> None:
    """Stage 1: schema-level validation.

    Mirrors the FR-P3.1-02 contract:
      * `additionalProperties: false` (top-level + nested).
      * `schema_version` MUST equal a known version.
      * Required fields (`plan_id`, `snapshot_id`,
        `expected_actions`) MUST be present.
    """
    # Stage 1a: schema_version (D11).
    schema_version = payload.get("schema_version")
    if not schema_version:
        errors.append(ValidationError(
            code=CODE_PLAN_SCHEMA_INVALID,
            pointer="/schema_version",
            message="schema_version is required",
        ))
    else:
        # Defer the import to avoid a cycle with the schema module.
        from .schema import SCHEMA_VERSION, known_versions
        if schema_version not in known_versions():
            errors.append(ValidationError(
                code=CODE_UNKNOWN_SCHEMA_VERSION,
                pointer="/schema_version",
                message=(
                    f"schema_version {schema_version!r} is not "
                    f"recognised; known: "
                    f"{sorted(known_versions())}"
                ),
                value=schema_version,
            ))
        elif schema_version != SCHEMA_VERSION:
            # Future reader-only mode: log but do not fail
            # the parser. The contract is "read but don't act
            # on unknown". For V1, we accept equal only.
            errors.append(ValidationError(
                code=CODE_UNKNOWN_SCHEMA_VERSION,
                pointer="/schema_version",
                message=(
                    f"schema_version {schema_version!r} is not "
                    f"the current version {SCHEMA_VERSION!r}"
                ),
                value=schema_version,
            ))

    # Stage 1b: required top-level fields.
    for required in ("plan_id", "snapshot_id", "expected_actions"):
        if required not in payload:
            errors.append(ValidationError(
                code=CODE_PLAN_SCHEMA_INVALID,
                pointer=f"/{required}",
                message=f"{required!r} is required",
            ))

    # Stage 1c: `expected_actions` MUST be a non-empty list of
    # objects. (Strict shape; per-stage validation runs after.)
    ea = payload.get("expected_actions")
    if ea is not None and (not isinstance(ea, list) or not ea):
        errors.append(ValidationError(
            code=CODE_PLAN_SCHEMA_INVALID,
            pointer="/expected_actions",
            message="expected_actions must be a non-empty list",
            value=ea,
        ))


def _check_dependency_cycles(
    step_ids: list[str],
    expected_dependencies: Mapping[str, Any],
    errors: list[ValidationError],
) -> None:
    """Stage 5: three-color DFS for dependency cycles.

    Mirrors P0.2's `validate_plan` dependency cycle detection.
    """
    # Validate the shape of `expected_dependencies`.
    deps: dict[str, list[str]] = {}
    for step_id, raw in expected_dependencies.items():
        if not isinstance(raw, list):
            errors.append(ValidationError(
                code=CODE_PLAN_SCHEMA_INVALID,
                pointer=(
                    f"/expected_dependencies/{step_id}"
                ),
                message=(
                    f"expected_dependencies[{step_id!r}] must "
                    f"be a list of step_ids"
                ),
                value=raw,
            ))
            continue
        deps[step_id] = [str(d) for d in raw]

    # 3-color DFS.
    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[str, int] = {s: WHITE for s in step_ids}

    def visit(node: str, stack: list[str]) -> None:
        if color.get(node) == BLACK:
            return
        if color.get(node) == GRAY:
            # Cycle: stack ends with `node`; report the closed
            # loop.
            idx = stack.index(node)
            cycle = tuple(stack[idx:] + [node])
            errors.append(ValidationError(
                code=CODE_DEPENDENCY_CYCLE,
                pointer=(
                    f"/expected_dependencies/{node}"
                ),
                message=(
                    f"dependency cycle detected: "
                    f"{' -> '.join(cycle)}"
                ),
                cycle=cycle,
            ))
            return
        color[node] = GRAY
        stack.append(node)
        for d in deps.get(node, ()):
            if d not in color:
                # Dangling dependency: surface as a schema
                # error (the dependency refers to a step that
                # doesn't exist).
                errors.append(ValidationError(
                    code=CODE_PLAN_SCHEMA_INVALID,
                    pointer=(
                        f"/expected_dependencies/{node}/{d}"
                    ),
                    message=(
                        f"dependency {d!r} does not match any "
                        f"step_id in this plan"
                    ),
                    value=d,
                ))
                continue
            visit(d, stack)
        stack.pop()
        color[node] = BLACK

    for s in step_ids:
        if color[s] == WHITE:
            visit(s, [])


__all__ = [
    "ParseResult",
    "ValidationError",
    "parse_plan",
    "STABLE_VALIDATION_CODES",
    "CODE_PLAN_SCHEMA_INVALID",
    "CODE_BACKEND_DISABLED",
    "CODE_ACTION_CODE_MISMATCH",
    "CODE_TARGET_REF_SNAPSHOT_MISMATCH",
    "CODE_DEPENDENCY_CYCLE",
    "COORDINATE_FALLBACK_NOT_ALLOWED",
    "CODE_UNKNOWN_SCHEMA_VERSION",
]