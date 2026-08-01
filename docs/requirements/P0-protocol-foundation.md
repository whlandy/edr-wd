# P0 — Stable Protocol Foundation

## Status

- State: requirements (no implementation yet)
- Source contract: `../architecture/01-action-trace-test-report-design.md` §24, §7, §8, §9, §10, §19
- Mapped checkpoints: **P0.1 Canonical Action Catalog**, **P0.2 Wire Models And Validation**, **P0.3 Observation And Target Identity**
- Completion state: **all three checkpoints UNDONE**

This document defines what P0 must deliver before any P1 work begins. It is
the only reviewable shape that lets the rest of the design hold without
rewriting wire models or trace schemas.

---

## Checkpoint P0.1 — Canonical Action Catalog

### Scope

Build one immutable catalog of every LLM-visible operation as the single
source of truth. The catalog must drive status, planning schema, dispatch,
and live enablement. The existing MCP tool names remain valid compatibility
aliases — not a second capability table.

### In Scope

- `target/action_catalog.py`: immutable `ActionSpec` records, frozen
  dataclasses preferred (Pydantic only if FastMCP already ships it; no
  target-only Pydantic dependency).
- All V1 semantic action IDs and their optional `action_code` aliases from
  `todo/llm-action-id-sequences.md` "Proposed Action Catalog V1" table
  (27 entries from `session.connect` A001 through `observe.wait_window`
  A062).
- Enums for `category` (observation/session/semantic_input/pointer_input/
  workflow), `side_effect` (none/session_mutation/gui_mutation/system_mutation),
  `risk` (low/medium/high/irreversible), `rollback_class` (reversible/
  reconstructable/logical_only/irreversible), `default_screenshot` (none/
  after/before_after/on_failure), `transition_policy` (never/possible/
  expected/required_checkpoint).
- Catalog construction must **reject**:
  - duplicate `action_id`
  - duplicate `action_code`
  - duplicate `tool_name`
  - unknown enum values
- `catalog_version` follows semver; bump rules per architecture §7.2
  (minor for backward-compatible add, patch for non-semantic metadata,
  major for removed actions or semantic change).
- `catalog_digest` is `sha256:<hex>` over canonical JSON of the full
  catalog sorted by `action_id` and excluding live `enabled` state.
- Live enablement uses stable `backend_kind` values (`windows_pywinauto`,
  `macos_accessibility`) and is preserved with `enabled: bool` plus
  `disabled_reason: str`. Runtime engines such as Windows `uia`/`win32` are
  reported separately as `backend_engine` and never used as catalog keys.
- Regenerated `status.action_space` Boolean map keyed by MCP tool name;
  matches today's per-backend `action_space` payloads exactly.
- New MCP tool `get_action_catalog(backend=None, include_disabled=False)`
  returning `{catalog_version, catalog_digest, actions: [...]}`.

### Out Of Scope

- New dispatch behavior (P0.3 covers target identity; P1.1 covers the
  dispatcher).
- Any code that calls into backend methods through the new IDs.
- `observe` is delivered by P0.3; `execute_action` is delivered by P1.1.
- Modifying `AutomationBackend` signatures or method names.

### Dependencies

- None. This is the first checkpoint in the sequence.

### Functional Requirements

| ID | Requirement |
|----|-------------|
| FR-P0.1-01 | Registry construction fails fast on duplicate IDs/codes/names with a structured error `code: "duplicate_action_id"\|"duplicate_action_code"\|"duplicate_tool_name"` plus the offending value. |
| FR-P0.1-02 | The 27-entry V1 table from `todo/llm-action-id-sequences.md` is represented 1:1 in the catalog; no action is missing or invented. |
| FR-P0.1-03 | Backend enablement for every action matches the existing per-backend `status.action_space` exactly (Windows: all 27 actions except reserved; macOS: `type_text`/`select`/`get_text` disabled). |
| FR-P0.1-04 | `catalog_version` is `1.0.0` and the digest is deterministic across two process invocations against the same source. |
| FR-P0.1-05 | `status.action_space` is generated from the catalog at request time, not duplicated in code. |
| FR-P0.1-06 | `get_action_catalog()` returns the same `catalog_digest` for two calls without process restart. |
| FR-P0.1-07 | Legacy tool names (`connect`, `lock_window`, `click`, `click_at`, `click_target`, `dump_tree`, `find_control`, `screenshot`, `type_text`, `select`, `get_text`, `activate_edr`, `restore_edr`, `activate_app`, `list_windows`, `is_window_open`, `wait_window`, `get_window_lock`, `verify_window_lock`, `unlock_window`, `hover_at`, `drag`, `scroll`, `double_click_at`, `right_click_at`, `middle_click_at`, `click_window_at`) all still resolve and return their current shapes. |

### Interface And Data Requirements

```python
# target/action_catalog.py
@dataclass(frozen=True)
class ActionSpec:
    action_id: str
    action_code: str | None
    tool_name: str
    description: str
    category: str  # enum value
    input_schema: dict[str, object]
    result_schema: dict[str, object]
    backends: tuple[str, ...]
    requires: tuple[str, ...]
    side_effect: str
    risk: str
    rollback_class: str
    default_screenshot: str
    transition_policy: str
    preferred_over: tuple[str, ...]

@dataclass(frozen=True)
class ActionEntry:
    spec: ActionSpec
    enabled: bool
    disabled_reason: str

def build_catalog() -> tuple[ActionEntry, ...]: ...
def catalog_version() -> str: ...
def catalog_digest() -> str: ...
def status_action_space(backend: str) -> dict[str, bool]: ...
def get_action_catalog_tool(backend: str | None = None,
                            include_disabled: bool = False) -> dict: ...
```

### Acceptance Criteria

1. `status.action_space` for both backends is byte-identical to today's
   hard-coded output (Windows: all-true except reserved; macOS: type_text /
   select / get_text `false`).
2. Catalog construction with one duplicated ID throws a structured error
   before any action is registered.
3. `catalog_digest()` returns the same hex value across two `python -c`
   invocations.
4. Adding/removing one entry changes `catalog_digest` and bumps the right
   semver position per §7.2.
5. `get_action_catalog(include_disabled=True)` returns the full list;
   `include_disabled=False` (default) filters disabled entries.

### Required Tests

| Test | Purpose |
|------|---------|
| `test_catalog_unique_ids_codes_names` | Duplicate detection at construction. |
| `test_catalog_v1_table_complete` | Every row from the V1 table is present. |
| `test_catalog_digest_stable` | Digest identical across two processes. |
| `test_catalog_digest_changes_on_mutation` | Adding/removing/editing one spec changes digest. |
| `test_status_action_space_unchanged` | Per-backend Boolean map equals today's output. |
| `test_get_action_catalog_filtering` | `include_disabled` toggles visibility. |
| `test_enum_values_rejected` | Unknown category/risk/etc. fails construction. |
| `test_catalog_semver_bumps` | Manual catalog edit demo in test proves minor/patch/major. |

### Deliverables

- `target/action_catalog.py` (registry, dataclasses, helpers).
- `target/server.py` edit: route `get_action_catalog` tool and replace
  the hard-coded `action_space` dicts in `status()` with a catalog call.
- Unit tests under `test_case/test_action_catalog/` (new directory).
- Windows + macOS fixtures (one snapshot of `status()` JSON each).
- Migration note in PR description only if the `status` JSON shape
  changes (it must not).

### Review Stop Point

Hermes stops after all P0.1 acceptance criteria pass and existing
`test_case/` tests remain green. Reviewer confirms:

- no MCP tool name is removed or renamed;
- `status.action_space` is byte-identical for both backends;
- digest is stable.

No P0.2 work begins until this checkpoint is approved.

---

## Checkpoint P0.2 — Wire Models And Validation

### Scope

Define strict typed models for the action sequence, test case, atomic step,
target reference, expectation, error envelope, and action receipt. Add
canonical-JSON helpers and shared ID generators. Build the validator that
runs before any GUI mutation.

### In Scope

- A dependency-light models module (prefer `target/protocol.py` to avoid
  forcing a new top-level `shared/` package). Mirror agent-only models
  under `agent/execution/models.py`.
- Canonical JSON serializer (`agent/canonical_json.py` or
  `target/canonical_json.py`) producing deterministic UTF-8 bytes — sorted
  keys, no insignificant whitespace, stable number/None handling.
- ID generators for sortable IDs (UUIDv7 if `uuid` supports it, else ULID
  via stdlib `uuid.uuid4()` + timestamp sort key — not the visible id, only
  the sort key).
- Models:
  - `ActionSequence` (`plan_id`, `catalog_version`, `catalog_digest`,
    `target`, `steps: list[ActionStep]`).
  - `ActionStep` (`step_id`, `action_id`, optional `action_code`, `args`,
    optional `target_ref`, optional `depends_on`, optional `transition`,
    optional `expectations`, `on_error`).
  - `TargetRef` (`snapshot_id`, `target_id`, `expected_process_name`,
    optional `fingerprint`, optional `selector_hint`).
  - `Expectation` (`type`, `value`, `timeout_seconds`).
  - `ActionReceipt` (`ok`, `error?`, `request_id`, `server_instance_id`,
    `observed_at`, optional `result`). `server_instance_id` changes on every
    target MCP server start and lets the agent detect lost in-flight outcomes.
  - `TestCase` (`case_id`, `title`, `description`, `profiles`, `tags`,
    `preconditions`, `steps`, `cleanup`, `timeout_seconds`).
  - `AtomicTestStep` (extends `ActionStep` with `step_no`, `title`,
    `evidence`, plus `TestCase` integration).
  - Error envelope per §19 (`code`, `message`, `retryable`, `details`,
    `request_id`, `observed_at`).
- Validator producing structured errors with stable codes (`unknown_action_id`,
  `unknown_action_code`, `unknown_expectation_type`, `schema_version_mismatch`,
  `catalog_digest_mismatch`, `duplicate_step_id`, `missing_dependency`,
  `dependency_cycle`, `backend_disabled`, `invalid_args`, etc.) and JSON
  paths (`steps[2].action_id`).
- Dependency-graph check: reject duplicate `step_id`, missing dependency,
  and any cycle (DFS with three-color marking).
- Dry-run entry point that runs validation only, without any backend call
  or GUI mutation. Returns `{ok, errors?, validated_steps: int}`.

### Out Of Scope

- The executor (P1.2).
- Transition detection (P2.1).
- Checkpoint creation (P2.1).
- Screenshots, evidence persistence, Markdown (P1.4, P2.3).

### Dependencies

- P0.1 catalog must be importable.

### Functional Requirements

| ID | Requirement |
|----|-------------|
| FR-P0.2-01 | Models reject unknown fields for protocol models (strict mode). |
| FR-P0.2-02 | Models round-trip through JSON without losing semantic content (catalog_version, enums, optional fields). |
| FR-P0.2-03 | Validator returns the first error per step with stable `code` and `path`; multi-error mode is also supported for batch reporting. |
| FR-P0.2-04 | Dependency cycle detection works for at least 3 nodes; smoke-tested with linear and diamond shapes. |
| FR-P0.2-05 | Dry-run never invokes any backend method (covered by a no-mutation guard test that monkey-patches backend methods to record calls). |
| FR-P0.2-06 | Schema version mismatch returns `code: "schema_version_mismatch"` with the offending version. |
| FR-P0.2-07 | Catalog digest mismatch returns `code: "catalog_digest_mismatch"`. |
| FR-P0.2-08 | Argument validation uses each action's `input_schema`; unknown keys and type mismatches are rejected before dispatch. |
| FR-P0.2-09 | Unknown expectation types are rejected before execution with `code: "unknown_expectation_type"`. |

### Interface And Data Requirements

```python
# target/protocol.py  (or agent/execution/models.py if shared/ avoided)
class ActionSequence: ...
class ActionStep: ...
class TargetRef: ...
class Expectation: ...
class ActionReceipt: ...
class TestCase: ...
class AtomicTestStep: ...

# target/canonical_json.py
def canonical_bytes(obj: object) -> bytes: ...
def canonical_sha256(obj: object) -> str: ...  # "sha256:<hex>"

# agent/execution/validator.py (or target-side mirror)
def validate_plan(plan: ActionSequence, catalog) -> list[ValidationError]: ...
def dry_run(plan: ActionSequence, catalog) -> DryRunResult: ...
```

### Acceptance Criteria

1. Valid V1 fixtures round-trip JSON without losing fields.
2. Invalid fixtures fail with the documented `code` and `path`.
3. Three-node cycle is detected; diamond is accepted.
4. Dry-run against a stub backend records zero backend calls.
5. Adding a new field to an existing model raises during round-trip
   unless the model is updated.
6. Unknown action ID returns `code: "unknown_action_id"` and the JSON
   path of the offending step.

### Required Tests

| Test | Purpose |
|------|---------|
| `test_models_round_trip` | JSON in → model → JSON out equivalence. |
| `test_strict_unknown_field` | Extra keys are rejected. |
| `test_validator_unknown_action` | Code/path for `unknown_action_id`. |
| `test_validator_unknown_action_code_with_known_id` | Code mismatch between action_id and action_code returns `unknown_action_code_for_action`. |
| `test_dependency_cycle` | 3-node cycle detected; 4-node cycle detected. |
| `test_dependency_diamond` | Diamond dependency graph accepted. |
| `test_dry_run_no_backend_call` | Stub backend records zero calls. |
| `test_expectation_type_registry` | Unknown expectation types rejected. |
| `test_canonical_json_determinism` | Same dict in different insertion order → identical bytes. |
| `test_validation_error_snapshot` | Stable code + path for representative bad inputs. |

### Deliverables

- `target/protocol.py` (or `shared/action_models.py` if package policy
  allows — defer to whatever the deploy path picks).
- `target/canonical_json.py` (or `shared/canonical_json.py`).
- `agent/execution/validator.py` and `agent/execution/models.py`.
- Unit tests under `test_case/test_protocol_models/`.
- One positive and one negative fixture per model in
  `test_case/fixtures/protocol/`.
- Migration note stub: schema_version is `1.0.0` for all new types.

### Review Stop Point

Stop after all P0.2 acceptance criteria pass. Reviewer confirms:

- no existing MCP tool is affected;
- error codes are stable and discoverable;
- validator does not call any backend.

---

## Checkpoint P0.3 — Observation And Target Identity

### Scope

Give every observed window/control a deterministic observation-local
identity, plus backend-neutral fingerprinting and a typed resolution
pipeline. Preserve every legacy observation field.

### In Scope

- `target/observations.py`:
  - `ObservationSnapshot` model (`schema_version`, `snapshot_id`,
    `captured_at`, `backend`, `host`, `active_window`, `targets`,
    `tree_digest`, `screenshot_evidence_id?`).
  - Deterministic target ID assignment: walk top-level windows sorted by
    `(process_name, pid, native_window_id)`, then controls per backend's
    stable traversal order; assign `T0001`, `T0002`, …
  - `Target` model (`target_id`, `kind`, `process_name`, `pid`,
    `native_window_id`, `title`, `control_type?`, `automation_id?`,
    `text?`, `rect?`, `fingerprint`, `fingerprint_fields`).
  - `compute_fingerprint(target)` uses stable fields only (no screen
    coordinates, no transient list position, no auto-generated numeric
    control IDs unless no better identity exists). Records which fields
    were used.
  - Backend-neutral `observe(include_tree=True, include_screenshot=False,
    max_depth=10)` tool returning an `ObservationSnapshot`.
  - Typed errors: `target_stale`, `target_not_found`, `target_ambiguous`,
    `ownership_mismatch`, `fallback_not_allowed`.
- Resolution pipeline in `target/resolver.py` (or `observations.py`):
  1. verify process/window ownership;
  2. exact native identity if still valid;
  3. exact stable fingerprint;
  4. backend-native selector (selector_hint);
  5. role/type + normalized accessible text;
  6. guarded rectangle proximity **only** for actions whose catalog
     permits pointer fallback.
- Each legacy observation endpoint (`list_windows`, `dump_tree`,
  `find_control`) gains `snapshot_id` and per-control target IDs in its
  response while preserving existing fields.

### Out Of Scope

- Plan/execute integration (P1.1/P1.2).
- `execute_action` (P1.1).
- Screenshot transport and redaction (P1.4).
- Transition detection (P2.1).

### Dependencies

- P0.1 catalog (action metadata controls fallback permission).
- P0.2 models (TargetRef type reused by resolver).

### Functional Requirements

| ID | Requirement |
|----|-------------|
| FR-P0.3-01 | Same `(backend, host, snapshot)` input → identical `target_id` ordering and identical fingerprint digest across two process runs. |
| FR-P0.3-02 | `list_windows`, `dump_tree`, `find_control` responses gain `snapshot_id` and target IDs; legacy keys (`title`, `process_id`, `class_name`, `handle`, `rect`, `control_id`, `text`, `is_visible`, `is_enabled`) remain present and unchanged. |
| FR-P0.3-03 | Stale `target_id` returns `code: "target_stale"`; never falls through to coordinate guessing. |
| FR-P0.3-04 | Ambiguous selector returns `code: "target_ambiguous"` with the count of candidates; no action is dispatched. |
| FR-P0.3-05 | Ownership mismatch returns `code: "ownership_mismatch"` with `expected_process_name` and `actual_process_name`. |
| FR-P0.3-06 | Pointer fallback for actions whose catalog `requires` does not permit it returns `code: "fallback_not_allowed"`. |
| FR-P0.3-07 | Fingerprint fields list is exposed so resolution can explain a mismatch (which fields matched, which did not). |
| FR-P0.3-08 | P0.3 exposes snapshot comparison and `invalidate_snapshot()` primitives. Wiring every mutating action to invalidation is deferred to P1.1, where dispatch exists. |

### Interface And Data Requirements

```python
# target/observations.py
@dataclass(frozen=True)
class ObservationSnapshot:
    schema_version: str
    snapshot_id: str
    captured_at: str
    backend: str
    host: str
    active_window: Target | None
    targets: tuple[Target, ...]
    tree_digest: str
    screenshot_evidence_id: str | None

@dataclass(frozen=True)
class Target:
    target_id: str  # T0001, T0002, ...
    kind: str       # "window" | "control"
    process_name: str
    pid: int | None
    native_window_id: str
    title: str
    control_type: str | None
    automation_id: str | None
    text: str | None
    rect: tuple[int, int, int, int] | None
    fingerprint: str          # sha256:<hex>
    fingerprint_fields: tuple[str, ...]

def snapshot_for_connected_window(backend, include_tree=True,
                                  include_screenshot=False,
                                  max_depth=10) -> ObservationSnapshot: ...

def resolve_target(ref: TargetRef, snapshot: ObservationSnapshot,
                   catalog: ActionSpec) -> Target: ...  # raises typed errors
```

### Acceptance Criteria

1. Snapshotting the same window twice produces identical target ordering,
   IDs, and fingerprints.
2. Removing one transient field (e.g. list position) does not change the
   fingerprint.
3. A resolved `target_id` from snapshot N is rejected as `target_stale`
   after a `dump_tree` that changed the tree digest.
4. Two controls matching the same selector produce `target_ambiguous`.
5. Resolver never falls through to coordinates for `gui.click`.
6. Ownership mismatch is rejected before any backend method call.
7. Calling `invalidate_snapshot()` makes references to that snapshot resolve as
   `target_stale`; P0.3 does not modify legacy action wrappers to invoke it.

### Required Tests

| Test | Purpose |
|------|---------|
| `test_snapshot_id_determinism` | Same inputs → identical target IDs. |
| `test_fingerprint_stable_fields` | Stable-field fingerprint recomputed equals original. |
| `test_fingerprint_excludes_transient` | Coordinates/list position removed → fingerprint unchanged. |
| `test_resolver_ownership_mismatch` | Wrong process → `ownership_mismatch`. |
| `test_resolver_ambiguous` | Two matches → `target_ambiguous`. |
| `test_resolver_stale` | Tree digest changed → `target_stale`. |
| `test_resolver_no_coordinate_fallback_for_gui_click` | `gui.click` with only rect hint → `fallback_not_allowed`. |
| `test_legacy_dump_tree_fields_preserved` | Existing keys still present. |
| `test_snapshot_marks_tree_changed` | Two snapshots compared; diff metadata available. |
| `test_invalidate_snapshot_marks_refs_stale` | Explicit invalidation rejects old target refs without action integration. |

### Deliverables

- `target/observations.py` (snapshot + target + fingerprint).
- `target/resolver.py` (typed error pipeline).
- `target/server.py` edits: legacy tools add `snapshot_id`/target IDs;
  new `observe` tool exposed.
- Unit tests under `test_case/test_observations/`.
- Windows UIA fixture and macOS AX fixture (small synthetic snapshots
  committed under `test_case/fixtures/observation/`).

### Review Stop Point

Stop after all P0.3 acceptance criteria pass and existing observation
tests are unchanged. Reviewer confirms:

- legacy fields and key names are preserved;
- no resolver ever picks a coordinate for a non-pointer action;
- typed errors are returned, never bare exceptions.

---

## Open Decisions For P0

These were raised while drafting the requirement and need a decision before
the corresponding checkpoint begins. They are recorded here per the
project rule that any real gap goes to Open Decisions.

1. **Top-level `shared/` package or `target/protocol.py`?** Architecture
   §6 allows both. P0.2 must pick one based on packaging friction;
   record the choice in `packaging/DESIGN.md`.
2. **Pydantic vs frozen dataclasses for catalog and protocol models?**
   P0.1 prefers frozen dataclasses unless FastMCP already requires
   Pydantic. P0.2 may differ if FastMCP types demand Pydantic.
3. **Sortable ID: UUIDv7 vs ULID vs stdlib fallback?** UUIDv7 is in
   Python 3.14 only; ULID needs a 26-char encoding helper. Pick once
   and apply to all new IDs (`snapshot_id`, `event_id`, `trace_id`,
   `branch_id`, `checkpoint_id`, `evidence_id`, `plan_id`).

## Status Of Existing Code (for reference only — not counted as P0 progress)

| Area | Existing piece | Reusable in P0 |
|------|----------------|----------------|
| Backend methods | `target/automation/{base,windows_pywinauto,macos_accessibility}.py` | Yes — `target_id` computation, ownership checks, fingerprint fields reuse backend output. |
| MCP tool dispatch | `target/server.py` tool registrations | Yes — `get_action_catalog` and `observe` are thin wrappers. |
| `status()` payload | `target/server.py` hard-coded `action_space` | Becomes catalog-driven in P0.1; no shape change. |
| Live test profile dispatch | `test_case/run_*.py` | Untouched by P0. |

Existing tools (`connect`, `click`, etc.) are **not** P0 deliverables; they
are the migration baseline. Catalog/Models/Observations are new code that
must coexist with every existing call.
