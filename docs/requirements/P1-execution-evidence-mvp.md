# P1 — Execution And Evidence MVP

## Status

- State: requirements (no implementation yet)
- Source contract: `../architecture/01-action-trace-test-report-design.md` §24, §11, §12, §13, §14
- Mapped checkpoints: **P1.1 Single-Action Dispatcher And Idempotency**, **P1.2 Atomic Test Executor**, **P1.3 Append-Only Trace Core**, **P1.4 Screenshot Evidence And Case Trace (MVP milestone)**
- Completion state: **all four checkpoints UNDONE**
- Milestone: **P1.4 is the MVP**. At that point EDR-WD can execute atomic
  test cases, retain trustworthy step evidence, and produce a readable
  per-case record.

This document defines what P1 must deliver. P0 must be complete and
reviewed first; P2 must wait for P1.4.

---

## Checkpoint P1.1 — Single-Action Dispatcher And Idempotency

### Scope

Add `execute_action` as a single unified MCP entry point that dispatches
by semantic ID into the existing backend methods. Validate live
enablement, arguments, preconditions, and target ownership. Accept an
idempotency `request_id` and cache receipts for duplicate requests. Keep
existing MCP tools as compatibility wrappers.

### In Scope

- `target/action_dispatcher.py`: pure mapping `action_id → backend
  method` (and `tool_name → action_id`). Method resolution must be
  validated at startup; missing methods fail with `code:
  "dispatch_target_missing"`.
- New MCP tool `execute_action(action_id, action_code=None, args={},
  target_ref=None, request_id=None)`. Calls into the dispatcher.
- Argument coercion: validate `args` against `ActionSpec.input_schema`
  via the P0.2 validator before backend call.
- Live enablement check (catalog `enabled` flag + `backend`). Disabled
  action returns `code: "backend_disabled"`.
- `requires` enforcement:
  - `connected_window`: refuses without an active connect/lock.
  - `window_lock`: refuses without a verified lock.
  - `target_ref`: requires a non-null `target_ref`; for actions that
    also need `selector_hint`, fails with `code: "missing_selector_hint"`
    when absent.
- Idempotency cache: bounded in-memory LRU keyed by `request_id`,
  default cap 1024 entries (configurable). Duplicate request returns
  the original receipt without a second backend mutation.
- Server incarnation identity: generate one `server_instance_id` at target MCP
  startup and include it in `status` and every action receipt. The agent uses
  an instance change to classify only previously in-flight requests as having
  an unknown outcome; a fresh request on the new instance executes normally.
- Mutating actions invalidate observation snapshots according to catalog
  metadata after the backend call has a known result. This is the integration
  point for the invalidation primitive delivered by P0.3.
- Receipt normalization: every backend return is normalized to the
  `ActionReceipt` envelope; legacy string JSON envelopes are preserved
  verbatim for legacy tools and parsed inside the new dispatcher.
- Existing tool implementations must remain compatible wrappers: every
  legacy MCP tool still works without changes to its caller-visible
  parameters or return shape.

### Out Of Scope

- Trace events (P1.3).
- Screenshots and evidence persistence (P1.4).
- Checkpoints, transition detection (P2.1).
- Recovery branches (P2.2).
- Multi-action batching (architecture §7.4: "V1 should execute one
  action per `execute_action` call"; `call_id` is a future addition).

### Dependencies

- All P0 checkpoints.

### Functional Requirements

| ID | Requirement |
|----|-------------|
| FR-P1.1-01 | `execute_action(action_id="gui.click", args={...}, request_id="R1")` runs the exact same backend call as the legacy `click` tool with equivalent args (verified by call-count test on a stub backend). |
| FR-P1.1-02 | `request_id` collision returns the original receipt; backend mutation counter does not increase. |
| FR-P1.1-03 | Unknown `action_id` returns `code: "unknown_action_id"`. |
| FR-P1.1-04 | Disabled action for the current backend returns `code: "backend_disabled"` with `disabled_reason`. |
| FR-P1.1-05 | `requires: connected_window` violated returns `code: "precondition_failed"` with `requires: "connected_window"`. |
| FR-P1.1-06 | Mismatch between `action_id` and `action_code` returns `code: "action_code_mismatch"`. |
| FR-P1.1-07 | Target ownership mismatch returns `code: "ownership_mismatch"` before any click/pointer method is invoked. |
| FR-P1.1-08 | Receipt cache eviction policy is LRU; entries older than the cap are evicted deterministically. |
| FR-P1.1-09 | Every receipt/status exposes `server_instance_id`. If the agent observes an instance change while a request has no receipt, it records `action_outcome_unknown` and does not replay that in-flight mutation. A genuinely new request on the new instance executes normally. |
| FR-P1.1-10 | All 27 catalog actions have a working dispatcher mapping; if a backend lacks a method, `execute_action` returns `code: "dispatch_target_missing"` without crashing the server. |

### Interface And Data Requirements

```python
# target/action_dispatcher.py
def dispatch(action_id: str, action_code: str | None,
             args: dict, target_ref: TargetRef | None,
             request_id: str | None) -> ActionReceipt: ...

def request_cache_lookup(request_id: str) -> ActionReceipt | None: ...
def request_cache_store(request_id: str, receipt: ActionReceipt,
                        max_entries: int = 1024) -> None: ...

# New MCP tool
@mcp.tool(name="execute_action", ...)
def execute_action(action_id: str,
                   action_code: str | None = None,
                   args: dict | None = None,
                   target_ref: dict | None = None,
                   request_id: str | None = None) -> str: ...
```

### Acceptance Criteria

1. Stub backend call counter increases by exactly one per unique
   `request_id`; duplicate `request_id` increases by zero.
2. Every catalog entry resolves to a backend method or returns
   `dispatch_target_missing` cleanly.
3. Direct legacy calls (`click`, `dump_tree`, etc.) are unchanged.
4. After simulated restart, `server_instance_id` changes. An agent-side
   in-flight request without a receipt becomes `action_outcome_unknown`; a new
   request on the restarted target still executes.
5. Disabled actions on macOS (`type_text`, `select`, `get_text`) return
   `backend_disabled` with `disabled_reason: "not supported by
   macos_accessibility"`.

### Required Tests

| Test | Purpose |
|------|---------|
| `test_dispatch_all_actions_resolve` | Walk the catalog; assert every entry has a method or returns `dispatch_target_missing`. |
| `test_idempotency_one_mutation` | Stub backend counts calls; same request_id twice → count == 1. |
| `test_request_id_cache_lru_eviction` | Fill cache, add one more, oldest evicted. |
| `test_server_instance_id_changes_on_restart` | Restart creates a distinct instance identity. |
| `test_inflight_request_blocked_after_instance_change` | Agent does not replay an unconfirmed mutation from the prior instance. |
| `test_new_request_executes_after_restart` | Fresh requests are not confused with prior-instance IDs. |
| `test_action_code_mismatch` | `action_id="gui.click"`, `action_code="A030"` → `action_code_mismatch`. |
| `test_precondition_window_lock` | `gui.click` without lock → `precondition_failed`. |
| `test_ownership_mismatch_pre_dispatch` | Wrong `expected_process_name` → `ownership_mismatch`, no call. |
| `test_legacy_click_still_works` | Direct legacy call shape unchanged. |
| `test_disabled_action_returns_backend_disabled` | macOS `type_text` returns `backend_disabled`. |

### Deliverables

- `target/action_dispatcher.py`.
- `target/server.py` edit: register `execute_action` tool.
- Unit tests under `test_case/test_dispatcher/` using a stub backend.
- Compatibility snapshot tests proving legacy tool responses are
  unchanged.

### Review Stop Point

Hermes stops after all P1.1 acceptance criteria pass and legacy
behavior is preserved. Reviewer confirms:

- legacy MCP tools are byte-compatible;
- dispatcher does not import from agent-side packages;
- idempotency cache does not grow unbounded.

---

## Checkpoint P1.2 — Atomic Test Executor

### Scope

Implement the agent-side executor state machine through step assertion
and recording. Execute one mutating action at a time. Support bounded
`retry` and `capture_and_abort` (no arbitrary recovery yet). Implement
the core expectation types. Distinguish failed / blocked / skipped /
passed outcomes.

### In Scope

- `agent/execution/executor.py`: state machine per architecture §11.
  States: CREATED → VALIDATING → OBSERVING → PREPARING_STEP →
  CAPTURING_BEFORE (policy-dependent) → EXECUTING → OBSERVING_AFTER →
  CAPTURING_AFTER → ASSERTING → RECORDING → (loop or RECOVERING or
  FINALIZING) → COMPLETED | ABORTED.
- Per-step algorithm (architecture §11 "Per-Step Algorithm") strictly
  follows the 16 numbered steps, except:
  - screenshots are no-ops until P1.4 (events are still emitted with
    `screenshot: null`).
  - transition detection is deferred to P2.1.
  - recovery is deferred to P2.2.
- Core expectation evaluators (architecture §9.3):
  - `action_ok`, `window_open`, `window_closed`,
    `active_window_owner`, `control_exists`, `control_absent`,
    `control_text_equals`, `control_text_contains`,
    `window_text_contains`. `visual_evidence_captured` is reserved for P1.4;
    declaring it before P1.4 fails pre-execution validation with
    `expectation_not_available` and blocks the step.
- `on_error` policies supported in this checkpoint: `abort`,
  `retry` (bounded), `capture_and_abort` (capture only flagged for
  P1.4). `reobserve_replan` is a no-op stub until P2.2.
- Step status precedence: `failed > blocked > skipped > passed`.
- Case outcome precedence: required-step `failed` → case `failed`;
  else required-step `blocked` → case `blocked`; else all required
  steps `skipped` → case `skipped`; else `passed`.
- `step-results.json` projection written after each step (structure
  per architecture §9.2, minus evidence refs which become meaningful
  in P1.4).

### Out Of Scope

- Trace events as a chain (P1.3 introduces them; P1.2 writes a
  temporary local log only).
- Screenshot evidence (P1.4).
- Recovery branches, replans (P2.2).
- Transition-aware checkpoints (P2.1).
- Run-level report (P2.3).

### Dependencies

- P1.1 dispatcher.
- P0.2 models (TestCase, AtomicTestStep, Expectation).
- P0.3 resolver.

### Functional Requirements

| ID | Requirement |
|----|-------------|
| FR-P1.2-01 | The executor never executes two mutating actions back-to-back without an intervening `OBSERVING_AFTER`. |
| FR-P1.2-02 | `retry` policy respects a per-step bound (default 2) and a per-step timeout; both are configurable via TestCase metadata. |
| FR-P1.2-03 | An expectation that times out returns `failed` with the actual value, not `error`, unless the evaluator itself raised. |
| FR-P1.2-04 | A backend unavailable at executor start produces `blocked` for every step, not `failed`. |
| FR-P1.2-05 | `capture_and_abort` preserves the current step's real `failed` or `blocked` status, emits/records the case or trace terminal state `aborted`, and leaves remaining steps `skipped`. `aborted` is not a step status. |
| FR-P1.2-06 | `step-results.json` is overwritten atomically (temp file + `os.replace`) after each step. |
| FR-P1.2-07 | Before P1.4, declaring `visual_evidence_captured` returns `expectation_not_available` during pre-execution validation and the affected step is `blocked`; it is never silently skipped. |
| FR-P1.2-08 | The executor refuses to start a step whose `target_ref.snapshot_id` is older than the latest snapshot (returns `target_stale` and surfaces it on the step). |
| FR-P1.2-09 | One expected transition tag (`transition.expected=true`) is honored by skipping pre-action observation optimization, but no checkpoint is created yet (P2.1). |

### Interface And Data Requirements

```python
# agent/execution/executor.py
class AtomicExecutor:
    def __init__(self, catalog, dispatcher, validator,
                 observation_provider, expectations_registry): ...
    def run_case(self, case: TestCase,
                 target_selector: str) -> CaseRunResult: ...
    def run_step(self, case: TestCase, step: AtomicTestStep,
                 snapshot: ObservationSnapshot) -> StepResult: ...

@dataclass
class StepResult:
    step_id: str
    step_no: int
    status: str  # passed | failed | blocked | skipped
    started_at: str
    ended_at: str
    duration_ms: int
    action: ActionReceipt | None
    expectation_results: list[ExpectationResult]
    error: dict | None
    # P1.4 will add: evidence_refs, checkpoint_ref, branch_ref

# agent/execution/expectations.py
EXPECTATION_REGISTRY = {
    "action_ok": evaluate_action_ok,
    "window_open": evaluate_window_open,
    "window_closed": evaluate_window_closed,
    "active_window_owner": evaluate_active_window_owner,
    "control_exists": evaluate_control_exists,
    "control_absent": evaluate_control_absent,
    "control_text_equals": evaluate_control_text_equals,
    "control_text_contains": evaluate_control_text_contains,
    "window_text_contains": evaluate_window_text_contains,
    # Registered in P1.4. Before then validation reports expectation_not_available.
}
```

### Acceptance Criteria

1. A fake MCP target executes a 3-step case end to end; step statuses
   are `passed`.
2. `action_ok=true` plus a failing `window_text_contains` produces a
   `failed` step (not `passed`).
3. Backend unavailable at start produces `blocked` for every step.
4. Retry policy retries exactly N times on `failed`, then stops.
5. `capture_and_abort` stops the case after the failing step and
   leaves remaining steps in `skipped`.
6. `step-results.json` overwrites atomically — readers always see a
   complete file or the prior complete file.

### Required Tests

| Test | Purpose |
|------|---------|
| `test_executor_pass_three_steps` | Happy path with stub backend. |
| `test_executor_action_ok_but_expectation_failed` | Step is `failed`. |
| `test_executor_backend_unavailable` | Every step `blocked`. |
| `test_executor_retry_bound` | Retry exactly N times. |
| `test_executor_capture_and_abort_stops_case` | Remaining steps `skipped`. |
| `test_executor_step_results_atomic_write` | Write fails mid-write → previous file intact. |
| `test_executor_target_stale_blocks_step` | Old snapshot_id → `blocked`. |
| `test_visual_evidence_unavailable_blocks_before_p1_4` | Required visual evidence cannot silently pass or skip. |

### Deliverables

- `agent/execution/executor.py`, `agent/execution/expectations.py`,
  `agent/execution/state_machine.py` (or similar split).
- `agent/execution/models.py` extended with `StepResult`,
  `ExpectationResult`, `CaseRunResult`.
- Stub fake MCP target under `test_case/fake_target/` for integration
  tests.
- Integration tests under
  `test_case/test_executor_integration/`.

### Review Stop Point

Stop after P1.2 acceptance criteria pass. Reviewer confirms:

- state machine has no undocumented transitions;
- retry/abort policies are bounded and deterministic;
- step-results.json is durable across crashes.

---

## Checkpoint P1.3 — Append-Only Trace Core

### Scope

Implement typed event envelopes with canonical hashes, parent links,
and branch metadata. Add crash-safe JSONL append. Project events into
`step-results.json` from the chain (projections become disposable
read models). Verify chains on open and finalize.

### In Scope

- `agent/trace/events.py`: typed event payloads for the V1 event types
  (architecture §12.2 — `trace_started`, `environment_recorded`,
  `plan_created`, `plan_validated`, `plan_rejected`,
  `observation_recorded`, `step_started`, `checkpoint_created`,
  `action_requested`, `action_started`, `action_result`,
  `transition_detected`, `unexpected_transition`, `screenshot_captured`,
  `screenshot_persisted`, `expectation_result`, `step_completed`,
  `recovery_requested`, `branch_created`, `recovery_result`,
  `replan_created`, `cleanup_started`, `cleanup_completed`,
  `trace_completed`, `trace_aborted`). Branch support is **metadata
  only** in P1.3 — no recovery execution (P2.2).
- `agent/trace/store.py`:
  - `TraceStore` class managing `events.jsonl`, `manifest.json`,
    `observations/<snapshot_id>.json`, `step-results.json`.
  - Append mode with `fsync` after mutating action results, checkpoint
    creation, failures, and terminal events.
  - Crash-safe recovery on open: locate the last complete newline, retain the
    incomplete tail's byte count and SHA-256 in recovery metadata, truncate the
    file to the last complete event, flush and `fsync`, then append a
    `trace_recovered` event based on the last valid hash. Reject corruption in
    any earlier line. Truncating only an incomplete crash tail is repair of an
    uncommitted write, not mutation of a valid historical event.
- `agent/trace/integrity.py`:
  - Canonical-bytes hash chain: `event_hash = sha256(canonical_bytes({...event without event_hash}))`.
  - First event uses `previous_hash = null`; subsequent use the prior
    event's `event_hash`.
  - Verify chain on open and on `finalize_report`.
- `agent/trace/projections.py`:
  - Pure read-model projection from `events.jsonl` + evidence →
    `step-results.json`. Demonstrates that projections are disposable
    and reproducible.
- `manifest.json` schema for the case attempt:
  `trace_id`, branch_heads, catalog_digest, evidence_counts,
  terminal_status, integrity_verification_result.

### Out Of Scope

- Screenshot persistence (P1.4).
- Markdown rendering (P1.4 minimal; P2.3 full).
- Transition detection (P2.1).
- Recovery execution (P2.2).
- Run-level aggregation (P2.3).

### Dependencies

- P1.2 executor (events are produced from executor state changes).
- P0.2 canonical_json (shared).

### Functional Requirements

| ID | Requirement |
|----|-------------|
| FR-P1.3-01 | Every executor state change documented in architecture §11 produces exactly one event with the documented `event_type`. |
| FR-P1.3-02 | `event_hash` is computed over canonical bytes of the event with `event_hash` itself excluded. |
| FR-P1.3-03 | Appending one event then truncating the file at an arbitrary byte boundary leaves the prior events verifiable on reopen. |
| FR-P1.3-04 | Reordering or removing any prior event fails chain verification. |
| FR-P1.3-05 | Inserting an event mid-stream fails chain verification. |
| FR-P1.3-06 | An incomplete final line is recovered by hashing its bytes for diagnostics, truncating to the last complete newline, `fsync`ing, and appending `trace_recovered` whose parent/hash follows the last valid event; the resulting JSONL and chain verify. |
| FR-P1.3-07 | Corruption in any earlier line raises and refuses to open. |
| FR-P1.3-08 | `step-results.json` is fully derivable from `events.jsonl` + observation evidence; deleting and rebuilding it produces byte-identical output. |
| FR-P1.3-09 | `manifest.json` records `catalog_digest`, `branch_heads`, evidence counts, terminal status, and the integrity verification result. |
| FR-P1.3-10 | Event IDs are sortable unique identifiers (UUIDv7 or ULID, per P0 Open Decision). |

### Interface And Data Requirements

```python
# agent/trace/events.py
class EventType(str, Enum):
    TRACE_STARTED = "trace_started"
    # ... full V1 list from architecture §12.2
    TRACE_RECOVERED = "trace_recovered"  # crash-tail repair event

@dataclass(frozen=True)
class TraceEvent:
    schema_version: str
    trace_id: str
    branch_id: str
    event_id: str
    sequence_no: int
    parent_event_id: str | None
    caused_by_event_id: str | None
    event_type: EventType
    recorded_at: str
    plan_id: str | None
    case_id: str | None
    step_id: str | None
    call_id: str | None
    payload: dict
    previous_hash: str | None
    event_hash: str

# agent/trace/store.py
class TraceStore:
    def __init__(self, root: Path): ...
    def open(self, trace_id: str, *, create: bool) -> TraceContext: ...
    def append(self, event: TraceEvent) -> TraceEvent: ...  # computes hash
    def finalize(self, terminal: EventType, payload: dict) -> None: ...

# agent/trace/integrity.py
def verify_chain(events: Iterable[TraceEvent]) -> IntegrityReport: ...
```

### Acceptance Criteria

1. Running a 3-step case produces a sequence of events whose count
   matches the documented state changes (plus 2 trace start/end and
   one environment_recorded).
2. Two `TraceStore` instances opening the same directory verify the
   same chain.
3. Manual JSON tampering of one historical event fails verification.
4. Crash mid-write: re-open, recover, append a new event, verify.
5. Deleting `step-results.json` and re-projecting reproduces the
   previous content exactly.

### Required Tests

| Test | Purpose |
|------|---------|
| `test_event_chain_hash_determinism` | Same events → same hash for same inputs. |
| `test_tamper_detection_remove_event` | Removing one event fails verification. |
| `test_tamper_detection_reorder` | Reordering fails verification. |
| `test_tamper_detection_insert` | Inserting one event fails verification. |
| `test_incomplete_final_line_recovery` | Partial tail is diagnosed, truncated, fsynced, then followed by a valid `trace_recovered` event. |
| `test_corruption_in_earlier_line_rejected` | Edit byte 100; reopen fails. |
| `test_projection_byte_identical_replay` | Delete step-results.json; rebuild; equals prior. |
| `test_manifest_includes_catalog_digest` | Manifest schema spot-check. |

### Deliverables

- `agent/trace/{events,store,integrity,projections}.py`.
- `agent/execution/executor.py` rewired to produce events.
- Unit + crash-fixture tests under `test_case/test_trace/`.
- A sample artifact directory
  (`test_case/fixtures/trace_minimal/`) with `events.jsonl`,
  `step-results.json`, and `manifest.json`.

### Review Stop Point

Stop after all P1.3 acceptance criteria pass. Reviewer confirms:

- no event type is omitted from the V1 list;
- projections are disposable;
- chain verification detects every documented tampering mode.

---

## Checkpoint P1.4 — Screenshot Evidence And Case Trace (MVP)

### Scope

Capture baseline, after-action, transition before/after, and failure
images. Transfer screenshots from the target to the agent artifact
store. Record image metadata (dimensions, digest, ownership, timestamps,
redaction state). Implement minimum secret-pattern redaction before
persistence. Incrementally generate one `trace.md` per case with
relative image links.

### In Scope

- `target/automation/{windows_pywinauto,macos_accessibility}.py`
  optional `screenshot_bytes()` returning raw PNG bytes + window
  metadata; existing `screenshot(path=...)` remains the fallback.
- Agent-side `agent/trace/evidence.py`:
  - Persist screenshot bytes to a temp file, compute SHA-256, verify
    digest, then atomically rename to
    `screenshots/<step_no>-<step_id>-<role>.png` (role ∈
    `{before, after, failure, baseline}`).
  - Build `EvidenceRecord` per architecture §14.1.
  - Reject absolute paths, `..`, control characters, and separators
    in `relative_path`.
  - Redaction hook `apply_text_redaction(text, rules)` and
    `apply_image_redaction(png_bytes, rules)` — V1 minimum: text
    pattern registry + opaque rectangle rules. Image redaction
    records `rule_ids` without storing the original.
- Capture policy (architecture §14.2 table):
  - case start → baseline
  - observation-only step → only on failure or when a
    `visual_evidence_captured` expectation is declared
  - stable same-page mutation → after
  - expected navigation/modal/window change → before+after
  - checkpointed action → before+after
  - irreversible action → before+after + authorization reference
    (authorization is policy metadata, not implemented action gating
    in P1.4)
  - any failed/blocked → immediate failure image
  - merged policy: catalog default + step override + runtime safety
    → strongest level; tests cannot weaken mandatory policy.
- `trace.md` renderer:
  - Header: case identity, target/profile, attempt, trace/plan/
    catalog IDs, start/end, duration, status.
  - Step result table.
  - One section per atomic step in execution order with embedded
    `screenshots/<file>.png` (relative).
  - Retry/branch/checkpoint/recovery section (branches shown but not
    exercised; checkpoint section reserved for P2.1).
  - Integrity/evidence verification status.
- Escape both HTML and Markdown special characters in untrusted UI strings and
  configure the renderer to disallow raw HTML. Missing/corrupt images render a
  visible warning block with evidence ID plus expected path.
- `visual_evidence_captured` expectation becomes fully functional in
  this checkpoint (was a stub in P1.2).

### Out Of Scope

- Transition-aware checkpoints (P2.1).
- Recovery execution (P2.2).
- Run-level report aggregation (P2.3).
- Application-state or environment-snapshot recovery (P2.2).
- LLM-driven planning (P3.1).
- Multi-target runs (still one target per case here; P2.3 aggregates).

### Dependencies

- All P1.1 / P1.2 / P1.3 checkpoints.

### Functional Requirements

| ID | Requirement |
|----|-------------|
| FR-P1.4-01 | Every mutating step persists an `after` screenshot whose digest matches the bytes on disk. |
| FR-P1.4-02 | Steps declared as expected-transition or checkpoint receive `before` and `after` images. |
| FR-P1.4-03 | After failure is detected, persist the `failure` image before retries, recovery, report refresh, or other follow-up work. Events needed to durably record the action/failure may precede it. |
| FR-P1.4-04 | `trace.md` embeds every retained screenshot via a relative path; opening only `trace.md` shows the ordered steps and images. |
| FR-P1.4-05 | Missing or digest-mismatching screenshots render a visible Markdown warning, never silently disappear. |
| FR-P1.4-06 | HTML-escape and Markdown-escape untrusted UI text, disable raw HTML rendering, and ensure elements such as `<script>` and `<img onerror=...>` appear only as inert text. |
| FR-P1.4-07 | Configured text-pattern redaction rules replace matches with `[REDACTED]` in any text observed or printed; configured image rules apply before persistence; rule IDs are recorded in the evidence record. |
| FR-P1.4-08 | `visual_evidence_captured` returns `passed` when the required screenshot exists with a matching digest; `failed` when missing or corrupt. |
| FR-P1.4-09 | `trace.md` overwrites atomically after each completed step using temp + `os.replace`. |
| FR-P1.4-10 | Path validation rejects filenames containing `..`, `/`, `\`, control chars, or absolute paths; offending inputs raise before any write. |

### Interface And Data Requirements

```python
# agent/trace/evidence.py
def persist_screenshot(png_bytes: bytes, role: str, *,
                       step_no: int, step_id: str,
                       snapshot_id: str, event_id: str,
                       process_name: str, pid: int,
                       window_title: str,
                       redaction_rules: list[str]) -> EvidenceRecord: ...

def apply_image_redaction(png_bytes: bytes,
                          rules: list[RedactionRule]) -> tuple[bytes, list[str]]: ...

def apply_text_redaction(text: str,
                         rules: list[RedactionRule]) -> tuple[str, list[str]]: ...

# agent/trace/markdown.py (case trace subset)
def render_case_trace(trace_dir: Path, *,
                      manifest: Manifest,
                      projections: Projections,
                      evidence_index: dict[str, EvidenceRecord]) -> str: ...
```

### Acceptance Criteria

1. A 3-step happy-path case opens its `trace.md` and displays
   baseline + per-step after images.
2. A forced failure case shows the failure screenshot inside the
   failing step section.
3. A case with one expected-transition step shows before+after for
   that step only.
4. Removing a screenshot file from disk and regenerating `trace.md`
   yields a visible warning, not an empty image.
5. Text and image redaction are visible in the persisted image and
   in any captured text.
6. `visual_evidence_captured` returns `passed` for present-and-good,
   `failed` for missing-or-bad.
7. `trace.md` is byte-identical across two renders with frozen
   `recorded_at` and `generated_at`.

### Required Tests

| Test | Purpose |
|------|---------|
| `test_after_screenshot_persists_with_digest` | Each mutating step has a matching-digest file. |
| `test_expected_transition_yields_before_after` | Transition step → both files. |
| `test_failure_step_persists_failure_image_first` | Failure image written before other I/O. |
| `test_trace_md_renders_relative_links` | Relative paths, not absolute. |
| `test_missing_screenshot_renders_warning` | Visible warning text. |
| `test_markdown_escape` | `<script>`-style content escaped. |
| `test_redaction_text_pattern` | `[REDACTED]` in text + rule id recorded. |
| `test_redaction_image_rectangle` | Image region masked; rule ids recorded. |
| `test_visual_evidence_captured_passed_then_failed` | Both branches. |
| `test_renderer_escapes_raw_html_and_markdown_links` | Script/image tags and malicious links remain inert text. |
| `test_path_validation_rejects_traversal` | `..` rejected. |
| `test_trace_md_byte_identical_rerender` | Frozen timestamps → byte-identical output. |

### Deliverables

- `agent/trace/evidence.py` (screenshot persistence, redaction).
- `agent/trace/markdown.py` (case trace renderer).
- Updates to executor and dispatcher to honor capture policy.
- Integration test under `test_case/test_evidence/` covering capture
  policy matrix (one test per row of §14.2 table).
- Golden `trace.md` fixture under `test_case/fixtures/trace_mvp/`.

### Review Stop Point (MVP Milestone)

Stop after all P1.4 acceptance criteria pass. Reviewer confirms:

- opening only `trace.md` displays ordered steps + retained screenshots;
- untrusted text is escaped;
- evidence integrity is end-to-end verifiable;
- existing MCP tool names are unchanged.

This checkpoint is the MVP. After approval, P2 may begin.

---

## Status Of Existing Code

| Area | Existing piece | Reusable in P1 |
|------|----------------|-----------------|
| Backend screenshot methods | `target/automation/{windows_pywinauto,macos_accessibility}.py` | Yes — `screenshot_bytes()` becomes an optional backend method. |
| Executor stub | none | New code only — `agent/execution/` is greenfield. |
| Trace store | none | New code only — `agent/trace/` is greenfield. |
| Markdown renderer | none | New code only — `agent/trace/markdown.py` is greenfield. |

No P1 piece is implemented. P1 is entirely new code that consumes P0
artifacts and the existing backend methods.

## Open Decisions Deferred From P0

These remain open until the relevant P1 checkpoint starts:

1. UUIDv7 vs ULID (resolved once, applied to `event_id`,
   `trace_id`, `branch_id`, `snapshot_id`, `evidence_id`,
   `checkpoint_id`).
2. Where `target/screenshot_bytes()` lives when both backends share
   identical bodies (decision: small interface in
   `target/automation/base.py`).
3. Whether to ship a thin `target/transport.py` for moving screenshot
   bytes when they exceed a threshold, or to keep base64 in receipts
   for V1.
