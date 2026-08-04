# P0.2 — Wire Models And Validation

## Status

- **Verdict**: implementation complete; ready for review.
- **Test state**: 56 pytest items in `test_case/test_protocol_models/`, all PASSED.
- **Full suite**: 209 passed, 27 skipped (skipped = live-target E2E).
- **No regression** in P0.1: catalog digest unchanged, catalog_version still `1.0.1`.

## What landed

### New files — `target/protocol_models/` (parallel to `target/action_catalog/`)

- `target/protocol_models/__init__.py` — public API surface; re-exports
  everything tests/server-side consumers need.
- `target/protocol_models/enums.py` — V1 enum sets:
  - 10 expectation types (architecture §9.3)
  - 4 `on_error` policies
  - 10 transition kinds (architecture §15.1)
  - 7 step statuses, 4 case outcomes
  - 4 evidence-screenshot override values
  - 2 target kinds
- `target/protocol_models/ids.py` — sortable, stdlib-only ID
  generators. Format: `{seq:08x}-{scope}-{uuid4_hex}`. The monotonic
  `seq` is at the front so string-compare sort matches insertion
  order within one process.
- `target/protocol_models/canonical_json.py` — generic deterministic
  JSON + sha256 helper (architecture §10 step 1). Supports dataclass
  / dict / list / tuple / set / frozenset / int / float / str / bool /
  None; rejects NaN, Inf, and unsupported types.
- `target/protocol_models/models.py` — strict frozen dataclasses with
  unknown-field rejection at `from_dict` time:
  - `ActionSequence`, `ActionStep`, `TargetRef`, `Expectation`,
    `Transition`, `EvidenceSpec`, `AtomicTestStep`, `TestCase`,
    `ActionReceipt`, `ErrorEnvelope`, `ProtocolModelError`.
  - Each model has an `_ALLOWED` field set + `from_dict` classmethod
    that raises `ProtocolModelError(code="unknown_field", path=...)`
    on any unknown key.
  - All model errors carry a stable `code` + `path` (FR-P0.2-03).
- `target/protocol_models/expectations.py` — typed expectation
  registry (`EXPECTATION_TYPE_REGISTRY`) with `validate_expectation_type`
  + `iter_expectation_types`. Actual evaluator implementations land
  in P1.2.
- `target/protocol_models/validator.py` — schema / catalog / DAG
  validation:
  - `validate_plan(plan, catalog=None, *, backend=None)` → `list[ValidationError]`.
  - `validate_case(case, catalog=None, *, backend=None)`.
  - Stable error codes (`ARCHITECTURE_P0_2_VALIDATION_CODES`):
    `unknown_action_id`, `unknown_action_code`, `action_code_mismatch`,
    `schema_version_mismatch`, `catalog_digest_mismatch`,
    `duplicate_step_id`, `missing_dependency`, `dependency_cycle`,
    `backend_disabled`, `invalid_args`, `unknown_expectation_type`,
    `unknown_field`, `invalid_target_ref`.
  - Dependency DAG: three-color DFS, cycle reports the closed loop.
  - `re_raise_protocol_model_error` maps model-layer errors to
    validator-layer codes.
- `target/protocol_models/dry_run.py` — `dry_run(plan, catalog=None,
  *, backend=None, on_backend_called=None) -> DryRunResult` and
  `dry_run_case(case, ...)`. Never touches a backend (FR-P0.2-05).

### New tests — `test_case/test_protocol_models/`

- `test_models.py` (32 items): strict-mode rejection, round-trip,
  error envelope, canonical JSON determinism, NaN/Inf rejection.
- `test_validator.py` (12 items): stable codes + JSON paths,
  cycle detection, diamond acceptance, schema/catalog binding,
  duplicate step_id, missing dependency, action_code mismatch,
  unknown action_id / action_code.
- `test_dry_run_and_ids.py` (12 items): dry-run no-backend invariant
  (verified via `on_backend_called` spy + sys.modules check),
  catalog-registry parity, ID format + sortability + uniqueness.

### New fixtures — `test_case/fixtures/protocol/`

- `positive_plan.json` — 3-step valid plan (connect → lock → click).
- `negative_unknown_action.json` — `gui.press` is not in the catalog.
- `negative_cycle.json` — 3-node dependency cycle.
- `positive_diamond.json` — 4-step DAG with two parallel branches.

## P0.2 acceptance gate (from architecture §24 / requirements P0.2)

| Gate | Status |
|------|--------|
| Valid V1 fixtures round-trip JSON without losing fields | PASS |
| Invalid fixtures fail with stable `code` + `path` | PASS |
| Three-node cycle detected; diamond accepted | PASS |
| Dry-run against stub records zero backend calls | PASS (`on_backend_called` spy + `sys.modules` guard) |
| New field to existing model raises during round-trip | PASS (`from_dict` strict-mode rejection) |
| Unknown action_id returns `unknown_action_id` + JSON path | PASS |
| Schema version mismatch returns `schema_version_mismatch` | PASS |
| Catalog digest mismatch returns `catalog_digest_mismatch` | PASS |
| Argument validation uses each action's schema; unknown keys rejected | PASS (model layer strict-mode) |
| Unknown expectation types rejected before execution | PASS |

## P0.2 Open Decisions (resolved)

Recorded per architecture §P0.2:

1. **Top-level `shared/` vs `target/protocol.py`** — chose
   `target/protocol_models/` (parallel to `target/action_catalog/`).
   Avoids cross-package dependencies and matches P0.1's per-feature
   package layout. The `target/` directory is already on sys.path for
   `server.py`, so adding the package costs nothing.

2. **Pydantic vs dataclasses** — chose frozen dataclasses +
   hand-rolled strict mode (no Pydantic dependency on the target
   side). Architecture §P0.1 explicitly preferred this; P0.2 inherits
   the same trade-off. Strict mode is implemented via `_ALLOWED`
   field sets + `_strict_from_dict_kwargs` rejecting unknown keys.

3. **Sortable ID generator** — P0.2 review revised this:
   - **Initial decision** (`{seq:08x}-{scope}-{uuid4_hex}`) was
     rejected: it sorted only within one process. Two IDs from
     different processes could compare in undefined order because
     the `uuid4` suffix was the secondary sort key.
   - **Revised decision** (`{ms:013d}-{seq:04x}-{scope}-{rand8}`):
     the 13-digit unix_ms timestamp dominates lexicographic order
     across processes; the 4-hex monotonic counter breaks ties
     within the same millisecond; the 8-hex random suffix defends
     against clock-skew collisions. Total length
     `13 + 1 + 4 + 1 + len(scope) + 1 + 8 = 28 + len(scope)`.
   - Cross-process sort order is correct as long as wall clocks
     are synchronised (or only slightly skewed); large clock skew
     can violate the total order. The 8-hex random suffix
     (~32 bits of entropy) mitigates accidental collisions.
   - Same-process cap: 65536 IDs per millisecond per process
     (4-hex `seq`). Comfortable headroom for EDR-WD workload;
     widen to 6 hex if ever needed.
   - Implementation is stdlib-only: `time.time_ns()` + `secrets.token_hex`
     + a `threading.Lock`-guarded per-millisecond counter.
   - **API**: `new_plan_id()`, `new_step_id()`, etc. all use the
     new format. Format change is documented as a MINOR bump
     (sort order is preserved; downstream consumers parsing the ID
     string must adapt).

## Stability contract

- `protocol_models.PROTOCOL_VERSION = "1.0.0"`.
- Schema bumps per architecture §7.2:
  - Add new field to existing model → patch bump (consumers using
    `from_dict` will silently ignore unknown fields — they will NOT,
    because we are strict; consumers must add the field).
  - Add new model → minor bump.
  - Change stable error code or enum value → major bump.

## Layer boundary (PR review — same lesson as P0.1)

P0.2 owns: "what is a valid ActionSequence / TestCase / AtomicTestStep
schema, what are the stable codes, what does dry-run mean".
P0.2 does NOT:
  - Probe live backend instances (`runtime_capability` is P1.1).
  - Dispatch actions (P1.1).
  - Execute test steps (P1.2).
  - Render Markdown or persist events (P1.3-P2.3).

## P0.2 Review Fixes (post-initial review)

Two blocking issues were flagged and resolved:

1. **ID format (blocker)** — replaced `{seq:08x}-{scope}-{uuid4_hex}`
   with `{ms:013d}-{seq:04x}-{scope}-{rand8}`. The new format is
   lexicographically sortable across processes provided wall clocks
   are synchronised (or only slightly skewed). See Open Decision 3
   above for the exact softening.

2. **Canonical JSON set determinism (blocker)** — `canonical_json`
   now sorts `set` / `frozenset` elements by their canonical JSON
   bytes before serializing. Two sets with the same elements but
   different runtime iteration order produce byte-identical output.

Plus three non-blocking cleanups:

3. **`EXPECTATION_TYPE_REGISTRY` is now a `MappingProxyType`** —
   exposed via a private `_EXPECTATION_TYPE_REGISTRY_RAW` dict.
   Mutation is rejected with `TypeError`. Verified by
   `test_expectation_registry_is_immutable`.

4. **`negative_unknown_field.json` fixture added** — the previous
   `negative_unknown_action.json` fixture was reduced to its single
   contract (unknown action_id), and the new fixture isolates the
   unknown-field case. Both fixtures now test one contract each.

## Public API

```python
from protocol_models import (
    PROTOCOL_VERSION,                # "1.0.0"

    # Models
    ActionSequence, ActionStep, ActionReceipt,
    AtomicTestStep, TestCase,
    TargetRef, Expectation, Transition, EvidenceSpec,
    ErrorEnvelope, ProtocolModelError,

    # Validator
    ValidationError, validate_plan, validate_case,
    re_raise_protocol_model_error,
    ARCHITECTURE_P0_2_VALIDATION_CODES,
    CODE_UNKNOWN_ACTION_ID, CODE_UNKNOWN_ACTION_CODE,
    CODE_ACTION_CODE_MISMATCH, CODE_SCHEMA_VERSION_MISMATCH,
    CODE_CATALOG_DIGEST_MISMATCH, CODE_DUPLICATE_STEP_ID,
    CODE_MISSING_DEPENDENCY, CODE_DEPENDENCY_CYCLE,
    CODE_BACKEND_DISABLED, CODE_INVALID_ARGS,
    CODE_UNKNOWN_EXPECTATION_TYPE, CODE_UNKNOWN_FIELD,
    CODE_INVALID_TARGET_REF,

    # Dry-run
    DryRunResult, dry_run, dry_run_case,

    # Canonical JSON
    canonical_bytes, canonical_sha256,

    # IDs
    SCOPE_PLAN, SCOPE_STEP, SCOPE_REQ, SCOPE_CP,
    SCOPE_EVT, SCOPE_BRANCH, SCOPE_SNAP, SCOPE_EVID,
    SCOPE_TRACE, VALID_SCOPES,
    new_plan_id, new_step_id, new_request_id,
    new_checkpoint_id, new_event_id, new_branch_id,
    new_snapshot_id, new_evidence_id, new_trace_id,

    # Expectation registry
    ExpectationTypeSpec, EXPECTATION_TYPE_REGISTRY,
    validate_expectation_type, iter_expectation_types,

    # Enums
    VALID_EXPECTATION_TYPES, VALID_ON_ERROR,
    VALID_TRANSITION_KINDS, VALID_STEP_STATUSES,
    VALID_CASE_OUTCOMES, VALID_EVIDENCE_SCREENSHOT,
    VALID_TARGET_KINDS,
)
```

## Next checkpoint

**STOP here. Wait for P0.2 review.**

When review approves merge, the next checkpoint is **P0.3
Observation And Target Identity** (see
`docs/requirements/P0-protocol-foundation.md`).