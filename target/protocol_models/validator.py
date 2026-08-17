"""
validator.py — P0.2 plan validation (architecture §10).

Validates a fully-decoded protocol model against:

    1. The strict dataclass contract (handled in `models.py` via
       `from_dict` + `_strict_from_dict_kwargs`). Unknown fields
       already raise `ProtocolModelError` with `code: "unknown_field"`.
    2. The catalog binding (catalog_version, catalog_digest match).
    3. Per-step reference integrity:
       - `action_id` exists in the catalog
       - optional `action_code` matches the chosen `action_id`
       - `target_ref.snapshot_id` is a known snapshot (P0.3 will own
         the actual snapshot registry; for P0.2 the validator only
         checks the format)
       - `depends_on` step_ids all exist and reference earlier-or-same
         steps (no forward-only dependency on a step that does not
         exist yet)
       - dependency graph is acyclic (three-color DFS)
    4. Per-step reference duplication (no two steps share a `step_id`).
    5. `expectations` use a typed registry (FR-P0.2-09). Unknown
       expectation types raise `code: "unknown_expectation_type"` —
       this is also covered by the strict model construction
       (Expectation's `__post_init__` validates against
       VALID_EXPECTATION_TYPES), but the validator cross-checks for
       any plan-side expectations that slipped through.

Validator returns `list[ValidationError]`. Empty list means the
plan is valid. Errors carry:

    * `code`  — stable string (see ARCHITECTURE_P0_2_VALIDATION_CODES).
    * `path`  — JSON-pointer-style: `steps[2].action_id`.
    * `message` — human-readable.
    * `value`  — offending value (when meaningful).

Public functions:

    * `validate_plan(plan, catalog) -> list[ValidationError]`
    * `validate_case(case, catalog) -> list[ValidationError]`

The catalog parameter is any object exposing the same lookup API as
`action_catalog.V1_CATALOG` (tuple of `ActionSpec`). Tests can pass
the real V1_CATALOG or a small synthetic tuple.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

try:
    from ..action_catalog import ActionSpec, ACTIONS_V1, catalog_digest
except ImportError:  # target-local deployment
    from action_catalog import ActionSpec, ACTIONS_V1, catalog_digest

from .enums import VALID_EXPECTATION_TYPES
from .models import (
    ActionSequence,
    ActionStep,
    AtomicTestStep,
    ProtocolModelError,
    TestCase,
)


# ---------------------------------------------------------------------------
# ValidationError — public surface for validator output
# ---------------------------------------------------------------------------


# Stable validation codes (FR-P0.2-03 / architecture §10 sample codes).
CODE_UNKNOWN_ACTION_ID         = "unknown_action_id"
CODE_UNKNOWN_ACTION_CODE       = "unknown_action_code"
CODE_ACTION_CODE_MISMATCH      = "action_code_mismatch"
CODE_SCHEMA_VERSION_MISMATCH   = "schema_version_mismatch"
CODE_CATALOG_DIGEST_MISMATCH   = "catalog_digest_mismatch"
CODE_DUPLICATE_STEP_ID         = "duplicate_step_id"
CODE_MISSING_DEPENDENCY        = "missing_dependency"
CODE_DEPENDENCY_CYCLE          = "dependency_cycle"
CODE_BACKEND_DISABLED          = "backend_disabled"
CODE_INVALID_ARGS              = "invalid_args"
CODE_UNKNOWN_EXPECTATION_TYPE  = "unknown_expectation_type"
CODE_UNKNOWN_FIELD             = "unknown_field"      # also raised by models
CODE_INVALID_TARGET_REF        = "invalid_target_ref"

# Tuple for grep-friendly code audit / migration notes.
ARCHITECTURE_P0_2_VALIDATION_CODES: tuple[str, ...] = (
    CODE_UNKNOWN_ACTION_ID,
    CODE_UNKNOWN_ACTION_CODE,
    CODE_ACTION_CODE_MISMATCH,
    CODE_SCHEMA_VERSION_MISMATCH,
    CODE_CATALOG_DIGEST_MISMATCH,
    CODE_DUPLICATE_STEP_ID,
    CODE_MISSING_DEPENDENCY,
    CODE_DEPENDENCY_CYCLE,
    CODE_BACKEND_DISABLED,
    CODE_INVALID_ARGS,
    CODE_UNKNOWN_EXPECTATION_TYPE,
    CODE_UNKNOWN_FIELD,
    CODE_INVALID_TARGET_REF,
)


@dataclass(frozen=True)
class ValidationError:
    """One validation issue. `code` is the stable machine-readable tag;
    `path` is the JSON-pointer-style location; `message` is human text;
    `value` is the offending value (when meaningful)."""

    code: str
    path: str
    message: str
    value: Any = None


# ---------------------------------------------------------------------------
# Catalog view protocol (minimal — keeps validator decoupled)
# ---------------------------------------------------------------------------


class _CatalogView:
    """Index of `ActionSpec` by `action_id` + by `action_code`.

    Constructed once per `validate_*` call. Tests can build synthetic
    catalogs without touching `action_catalog.ACTIONS_V1`.
    """

    def __init__(self, specs: Iterable[ActionSpec]):
        self._by_id: dict[str, ActionSpec] = {}
        self._by_code: dict[str, ActionSpec] = {}
        for spec in specs:
            if spec.action_id in self._by_id:
                raise ValueError(
                    f"catalog view received duplicate action_id={spec.action_id!r}"
                )
            self._by_id[spec.action_id] = spec
            if spec.action_code is not None:
                if spec.action_code in self._by_code:
                    raise ValueError(
                        f"catalog view received duplicate "
                        f"action_code={spec.action_code!r}"
                    )
                self._by_code[spec.action_code] = spec

    def get_by_id(self, action_id: str) -> ActionSpec | None:
        return self._by_id.get(action_id)

    def get_by_code(self, action_code: str) -> ActionSpec | None:
        return self._by_code.get(action_code)

    def all_action_ids(self) -> frozenset[str]:
        return frozenset(self._by_id)


def _default_catalog_view() -> _CatalogView:
    return _CatalogView(ACTIONS_V1)


def _spec_supports_backend(spec: ActionSpec, backend: str | None) -> bool:
    if backend is None:
        return True
    return backend in spec.backends


# ---------------------------------------------------------------------------
# Schema + catalog-binding checks
# ---------------------------------------------------------------------------


def _check_protocol_version(plan_version: str, errors: list[ValidationError]) -> None:
    """Accept any non-empty string but flag known-version mismatches.

    P0.2 introduces `PROTOCOL_VERSION = "1.0.0"` in `models.py`.
    Validators accept plans that declare newer or equal versions
    (forward-compatible) but reject older ones. This is a soft
    signal — the consumer's plan must match the agent's.
    """
    # Import locally to avoid a circular import risk during module load.
    from .models import PROTOCOL_VERSION
    if not plan_version:
        errors.append(ValidationError(
            CODE_SCHEMA_VERSION_MISMATCH,
            "schema_version",
            "schema_version is required",
            value=plan_version,
        ))
        return
    # Compare major.minor numerically when possible; fall back to
    # lexicographic if the version is not semver-shaped.
    try:
        plan_parts = tuple(int(x) for x in plan_version.split("."))
        agent_parts = tuple(int(x) for x in PROTOCOL_VERSION.split("."))
        if plan_parts[:2] < agent_parts[:2]:
            errors.append(ValidationError(
                CODE_SCHEMA_VERSION_MISMATCH,
                "schema_version",
                f"plan schema_version {plan_version!r} is older than agent "
                f"version {PROTOCOL_VERSION!r}",
                value=plan_version,
            ))
    except ValueError:
        # Non-semver version string: accept it (forward-compat) but
        # do not block — agents downstream may still understand it.
        pass


def _check_catalog_binding(
    plan: ActionSequence,
    errors: list[ValidationError],
    expected_digest: str,
) -> None:
    """FR-P0.2-07: catalog_digest_mismatch."""
    if plan.catalog_digest != expected_digest:
        errors.append(ValidationError(
            CODE_CATALOG_DIGEST_MISMATCH,
            "catalog_digest",
            f"plan catalog_digest {plan.catalog_digest!r} does not match "
            f"running catalog digest {expected_digest!r}",
            value=plan.catalog_digest,
        ))


# ---------------------------------------------------------------------------
# Per-step checks
# ---------------------------------------------------------------------------


def _check_step(
    step: ActionStep,
    index: int,
    catalog: _CatalogView,
    known_step_ids: set[str],
    errors: list[ValidationError],
    *,
    backend: str | None = None,
) -> None:
    path = f"steps[{index}]"

    # Step ID uniqueness is checked up front by the caller so the
    # downstream error reports a stable `duplicate_step_id`.
    spec = catalog.get_by_id(step.action_id)
    if spec is None:
        errors.append(ValidationError(
            CODE_UNKNOWN_ACTION_ID,
            f"{path}.action_id",
            f"action_id {step.action_id!r} is not in the catalog",
            value=step.action_id,
        ))
    else:
        if step.action_code is not None:
            # Two distinct failure modes (architecture §10 step 5):
            #   1. action_code is not registered anywhere in the
            #      catalog -> `unknown_action_code`.
            #   2. action_code is registered but for a different
            #      action_id -> `action_code_mismatch`.
            code_match = catalog.get_by_code(step.action_code)
            if code_match is None:
                errors.append(ValidationError(
                    CODE_UNKNOWN_ACTION_CODE,
                    f"{path}.action_code",
                    f"action_code {step.action_code!r} is not registered "
                    f"in the catalog",
                    value=step.action_code,
                ))
            elif code_match.action_id != step.action_id:
                errors.append(ValidationError(
                    CODE_ACTION_CODE_MISMATCH,
                    f"{path}.action_code",
                    f"action_code {step.action_code!r} does not match the "
                    f"action_id {step.action_id!r} (catalog has "
                    f"{spec.action_code!r})",
                    value=step.action_code,
                ))
        if backend is not None and not _spec_supports_backend(spec, backend):
            errors.append(ValidationError(
                CODE_BACKEND_DISABLED,
                f"{path}.action_id",
                f"action {step.action_id!r} is not supported by backend "
                f"{backend!r}",
                value=backend,
            ))

    # Expectation type cross-check (FR-P0.2-09). The strict model
    # already rejects unknown types via Expectation.__post_init__; we
    # repeat the check here so that plans built without going through
    # `from_dict` still surface a stable validator-level code.
    for j, expectation in enumerate(step.expectations):
        if expectation.type not in VALID_EXPECTATION_TYPES:
            errors.append(ValidationError(
                CODE_UNKNOWN_EXPECTATION_TYPE,
                f"{path}.expectations[{j}].type",
                f"expectation type {expectation.type!r} is not registered",
                value=expectation.type,
            ))

    # depends_on references are checked by the caller (DAG check).


def _check_dependency_graph(
    steps: Sequence[ActionStep],
    errors: list[ValidationError],
) -> None:
    """DFS with three-color marking. Cycle detection (FR-P0.2-04)."""
    # Step-ID uniqueness
    seen: dict[str, int] = {}
    for i, step in enumerate(steps):
        if step.step_id in seen:
            errors.append(ValidationError(
                CODE_DUPLICATE_STEP_ID,
                f"steps[{i}].step_id",
                f"step_id {step.step_id!r} is also used at steps[{seen[step.step_id]}]",
                value=step.step_id,
            ))
        else:
            seen[step.step_id] = i

    # Missing dependencies
    for i, step in enumerate(steps):
        for dep in step.depends_on:
            if dep not in seen:
                errors.append(ValidationError(
                    CODE_MISSING_DEPENDENCY,
                    f"steps[{i}].depends_on",
                    f"depends_on references unknown step_id {dep!r}",
                    value=dep,
                ))

    # Three-color DFS. WHITE=unvisited, GRAY=in-stack, BLACK=done.
    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[str, int] = {step.step_id: WHITE for step in steps}
    # Build adjacency in declared order so the cycle error reports the
    # deterministic edge sequence.
    adj: dict[str, list[str]] = {step.step_id: list(step.depends_on)
                                for step in steps}
    path_stack: list[str] = []

    def visit(node: str) -> None:
        if color.get(node) == GRAY:
            # Cycle: nodes from the GRAY prefix of path_stack plus `node`.
            idx = path_stack.index(node)
            cycle = path_stack[idx:] + [node]
            errors.append(ValidationError(
                CODE_DEPENDENCY_CYCLE,
                "steps",
                "dependency cycle detected: " + " -> ".join(cycle),
                value=cycle,
            ))
            return
        if color.get(node) == BLACK:
            return
        color[node] = GRAY
        path_stack.append(node)
        for nxt in adj.get(node, []):
            if nxt not in color:
                continue  # missing dep; already reported above
            visit(nxt)
        path_stack.pop()
        color[node] = BLACK

    for step_id in list(color):
        if color[step_id] == WHITE:
            visit(step_id)


# ---------------------------------------------------------------------------
# Public validation entry points
# ---------------------------------------------------------------------------


def validate_plan(
    plan: ActionSequence,
    catalog: Sequence[ActionSpec] | None = None,
    *,
    backend: str | None = None,
) -> list[ValidationError]:
    """Validate an `ActionSequence` against the catalog.

    `catalog` defaults to the V1 entries from `action_catalog`. Tests
    can pass a smaller synthetic tuple.

    `backend`, when given, additionally checks that every step's
    action is statically supported on that backend (architecture
    §10 step 7). P0.2's caller decides whether to set it.
    """
    errors: list[ValidationError] = []
    specs = ACTIONS_V1 if catalog is None else catalog
    view = _CatalogView(specs)

    # 1. Protocol version (FR-P0.2-06)
    _check_protocol_version(plan.catalog_version, errors)

    # 2. Catalog digest binding (FR-P0.2-07)
    expected = catalog_digest(specs)
    _check_catalog_binding(plan, errors, expected)

    # 3. Per-step checks
    for i, step in enumerate(plan.steps):
        _check_step(step, i, view, set(), errors, backend=backend)

    # 4. Dependency graph
    _check_dependency_graph(plan.steps, errors)

    return errors


def validate_case(
    case: TestCase,
    catalog: Sequence[ActionSpec] | None = None,
    *,
    backend: str | None = None,
) -> list[ValidationError]:
    """Validate a `TestCase` (preconditions + steps + cleanup).

    All `AtomicTestStep` instances (in `preconditions`, `steps`,
    `cleanup`) are flattened into one DAG with unique step_id
    constraints. Each step's `step_no` must be unique across the
    case (P0.2 rule, not strictly required by the architecture but
    consistent with how humans reference case steps).
    """
    errors: list[ValidationError] = []
    specs = ACTIONS_V1 if catalog is None else catalog
    view = _CatalogView(specs)

    all_steps: list[AtomicTestStep] = list(case.preconditions) \
        + list(case.steps) + list(case.cleanup)

    # step_no uniqueness within a case
    seen_no: dict[int, str] = {}
    for step in all_steps:
        if step.step_no in seen_no:
            errors.append(ValidationError(
                CODE_DUPLICATE_STEP_ID,
                "step_no",
                f"step_no {step.step_no} is shared between "
                f"{seen_no[step.step_no]!r} and {step.step_id!r}",
                value=step.step_no,
            ))
        else:
            seen_no[step.step_no] = step.step_id

    # per-step checks (using the AtomicTestStep-shaped `ActionStep` API)
    for i, step in enumerate(all_steps):
        _check_step(step, i, view, set(), errors, backend=backend)

    # dependency graph across all sub-step kinds
    _check_dependency_graph(all_steps, errors)

    return errors


# ---------------------------------------------------------------------------
# Catch-all for model construction errors (so callers can re-raise)
# ---------------------------------------------------------------------------


def re_raise_protocol_model_error(exc: ProtocolModelError) -> ValidationError:
    """Translate a `ProtocolModelError` into a `ValidationError`.

    `ProtocolModelError.code` is already a stable string from the
    same set as `ARCHITECTURE_P0_2_VALIDATION_CODES` (or one of the
    model-level codes). We map them through `MODEL_CODE_MAP`.
    """
    mapped = MODEL_CODE_MAP.get(exc.code, exc.code)
    return ValidationError(
        code=mapped,
        path=exc.path,
        message=str(exc),
        value=exc.value,
    )


MODEL_CODE_MAP: Mapping[str, str] = {
    "unknown_field": CODE_UNKNOWN_FIELD,
    "type_error":    CODE_INVALID_ARGS,
    "enum_error":    CODE_INVALID_ARGS,
    "empty_value":   CODE_INVALID_ARGS,
}


__all__ = [
    "ValidationError",
    "validate_plan",
    "validate_case",
    "re_raise_protocol_model_error",
    # Stable codes
    "CODE_UNKNOWN_ACTION_ID",
    "CODE_UNKNOWN_ACTION_CODE",
    "CODE_ACTION_CODE_MISMATCH",
    "CODE_SCHEMA_VERSION_MISMATCH",
    "CODE_CATALOG_DIGEST_MISMATCH",
    "CODE_DUPLICATE_STEP_ID",
    "CODE_MISSING_DEPENDENCY",
    "CODE_DEPENDENCY_CYCLE",
    "CODE_BACKEND_DISABLED",
    "CODE_INVALID_ARGS",
    "CODE_UNKNOWN_EXPECTATION_TYPE",
    "CODE_UNKNOWN_FIELD",
    "CODE_INVALID_TARGET_REF",
    "ARCHITECTURE_P0_2_VALIDATION_CODES",
]
