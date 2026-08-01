# Action Planning, Execution Trace, And Test Report Design

## Status

- State: proposed implementation design
- Intended implementer: Hermes or another coding agent
- Review owner: EDR-WD maintainers
- Related task list: [`../todo/llm-action-id-sequences.md`](../todo/llm-action-id-sequences.md)
- Initial protocol version: `1.0.0`

This document is the implementation contract for adding versioned action IDs,
LLM-generated action sequences, atomic test execution, chained traces,
transition-aware checkpoints, screenshot evidence, and Markdown reports to
EDR-WD. The TODO remains the progress checklist; this document defines how the
finished system should behave.

## 1. Problem Statement

EDR-WD currently exposes individual MCP tools and a Boolean
`status.action_space`. A caller can discover whether `click` or `screenshot` is
available, but cannot reliably:

- select actions through stable protocol IDs;
- validate an ordered LLM-generated action sequence before execution;
- bind an action to the exact observation in which its target was discovered;
- preserve a causal record across retries, replans, and recovery branches;
- associate atomic test steps with screenshots and assertions;
- generate a readable test trace and run-level report from execution evidence.

The implementation must add these capabilities without breaking existing MCP
tool names, profile runners, or direct callers of `target/server.py`.

## 2. Goals

1. Give every LLM-visible operation one stable semantic `action_id`.
2. Represent a test case as ordered atomic steps with explicit expectations.
3. Validate plans and test steps before GUI mutation.
4. Resolve GUI targets against a specific observation and reject stale or
   ambiguous references.
5. Record every execution as an append-only, hash-linked event chain.
6. Create recovery checkpoints around meaningful UI transitions, not every
   click.
7. Capture screenshots as test evidence with integrity and redaction metadata.
8. Generate one readable `trace.md` per case and one `report.md` per run.
9. Preserve failed attempts and recovery branches instead of rewriting history.
10. Support Windows UIA and macOS Accessibility through one backend-neutral
    protocol while retaining backend capability differences.

## 3. Non-Goals For V1

- A general workflow programming language with arbitrary loops or recursion.
- Database-style rollback of external product state.
- Autonomous arbitrary PowerShell or shell execution.
- Pixel-diff assertions as the only proof of correctness.
- A web UI for trace browsing. Markdown is the required human interface.
- Replacing pywinauto or macOS Accessibility with raw coordinate automation.
- Distributed execution of one test case across multiple target machines.
- Parsing Markdown back into executable plans or authoritative result data.

## 4. Design Principles

### 4.1 Semantic IDs Are Stable; Targets Are Observation-Local

`gui.click` describes an operation and remains stable across runs. `T0007`
describes one observed control and is valid only with its `snapshot_id`. A
numeric action code such as `A020` is an optional alias, never the canonical
identity.

### 4.2 Structured State Is Authoritative

JSON models and append-only trace events drive execution and reporting.
Markdown is generated output. Screenshots and observation files are immutable
evidence referenced by digest.

### 4.3 The LLM Proposes; Deterministic Code Decides

The LLM may choose action IDs, order, targets, expected transitions, and
recovery preferences. Deterministic code validates schemas, backend support,
ownership, stale targets, policy, retries, checkpoint requirements, and final
test outcomes.

### 4.4 Action Success Is Not Test Success

A click returning `ok=true` means only that the action executed. The atomic
test step passes only when every declared expectation passes.

### 4.5 Trace Everything; Checkpoint Selectively

Every action and observation belongs in the trace. Heavy checkpoints are
created around navigation, modal/window changes, submission, close/dismiss,
ownership changes, and irreversible effects.

## 5. Deployment And Ownership Boundaries

EDR-WD keeps the existing agent/target split.

```text
LLM or test author
  -> agent test runner / sequence executor
       -> catalog and plan validation
       -> trace store and report renderer
       -> MCP manager
            -> target/server.py
                 -> target action dispatcher
                 -> Windows UIA or macOS AX backend
                 -> target-side screenshot capture
```

### Agent Responsibilities

- load and normalize test definitions;
- obtain the live target catalog and backend status;
- validate action sequences and policy;
- schedule atomic steps and bounded retries;
- request before/after observations and screenshots;
- persist the authoritative trace, evidence, and projections;
- create recovery branches and decide whether replanning is required;
- generate `trace.md` and `report.md`;
- aggregate multiple case executions into one test run.

### Target Responsibilities

- advertise the action catalog and live enablement;
- execute one validated action at a time;
- enforce connected-window and process ownership locally;
- produce backend-neutral window/control observations;
- resolve observation-scoped target references;
- capture screenshots in the actual interactive desktop session;
- return structured receipts with no agent-local path assumptions.

Target status and receipts expose a per-process-start `server_instance_id`.
Stable catalog selection uses `backend_kind` (`windows_pywinauto` or
`macos_accessibility`); implementation details such as Windows `uia`/`win32`
are reported separately as `backend_engine` and are not catalog keys.

### Why Trace Storage Lives On The Agent

The agent already owns target selection, MCP connectivity, lifecycle, and test
dispatch. Keeping run artifacts on the agent enables one report to include
remote Windows and macOS cases, survives target redeployment, and prevents a
target-local path from becoming the public evidence API. The target may use
temporary files while capturing an image, but the agent must persist the final
evidence before a step is considered durably recorded.

## 6. Proposed Module Layout

Add modules incrementally. Do not move existing backend implementations merely
to match this diagram.

```text
shared/
  action_models.py          # dependency-light wire models and enums
  canonical_json.py         # deterministic JSON bytes and SHA-256 helpers

target/
  action_catalog.py         # immutable catalog and backend enablement
  action_dispatcher.py      # action_id -> existing backend method
  observations.py           # snapshots, target IDs, fingerprints
  server.py                 # MCP wrappers; backward-compatible tools

agent/execution/
  models.py                 # plans, cases, steps, expectations, results
  validator.py              # catalog/schema/policy validation
  executor.py               # state machine and MCP calls
  transitions.py            # before/after transition classification
  checkpoints.py            # checkpoint policy and recovery strategies
  redaction.py              # argument/text/image redaction policy

agent/trace/
  events.py                 # typed event payloads
  store.py                  # crash-safe append and artifact persistence
  integrity.py              # hash-chain and evidence verification
  projections.py            # event chain -> step/case/run read models
  markdown.py               # trace.md and report.md rendering

test_case/schema/
  test-case.schema.json     # optional exported JSON Schema
```

If introducing a top-level `shared/` package creates deployment friction,
place equivalent dependency-light models under `target/protocol.py` and mirror
only agent-specific models under `agent/execution/`. Do not import the full
agent package from the target service.

## 7. Action Catalog

### 7.1 ActionSpec

Use a frozen dataclass or immutable Pydantic model. Pydantic is preferred if a
compatible version is already available through FastMCP; otherwise use
dataclasses plus `jsonschema`-free local validation to avoid adding a target
dependency solely for this feature.

```python
class ActionSpec:
    action_id: str
    action_code: str | None
    tool_name: str
    description: str
    category: str
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
```

Required enum values:

| Field | Values |
|---|---|
| `category` | `observation`, `session`, `semantic_input`, `pointer_input`, `workflow` |
| `side_effect` | `none`, `session_mutation`, `gui_mutation`, `system_mutation` |
| `risk` | `low`, `medium`, `high`, `irreversible` |
| `rollback_class` | `reversible`, `reconstructable`, `logical_only`, `irreversible` |
| `default_screenshot` | `none`, `after`, `before_after`, `on_failure` |
| `transition_policy` | `never`, `possible`, `expected`, `required_checkpoint` |

The initial catalog contains exactly the semantic IDs and code aliases listed
in the related TODO. Registry construction must reject duplicate action IDs,
codes, and MCP tool names.

### 7.2 Version And Digest

- `catalog_version` follows semantic versioning.
- Adding a backward-compatible action increments minor version.
- Fixing descriptions or non-semantic metadata increments patch version.
- Removing an action or changing accepted semantics increments major version.
- `catalog_digest` is `sha256:<hex>` over canonical JSON of the full catalog,
  sorted by `action_id` and excluding live `enabled` state.

Plans carry both version and digest. Exact digest match is required by default.
An explicit compatibility policy may accept the same major version after
revalidation, but must append a `catalog_mismatch_accepted` trace event.

### 7.3 Live Enablement

Static catalog membership and live capability are separate:

```json
{
  "action_id": "gui.type_text",
  "enabled": false,
  "disabled_reason": "not supported by macos_accessibility"
}
```

Generate the old `status.action_space` mapping from this state using MCP tool
names as keys. Existing callers continue receiving Boolean values.

### 7.4 MCP Interfaces

Add:

```text
get_action_catalog(backend=null, include_disabled=false)
observe(include_tree=true, include_screenshot=false, max_depth=10)
execute_action(action_id, action_code=null, args={}, target_ref=null,
               request_id=null)
```

V1 should execute one action per `execute_action` call. The agent may represent
a `call_id` batch, but must stop between mutating actions when the first action
can invalidate subsequent targets. Existing MCP tools call the same dispatcher
internally or remain thin compatibility wrappers around backend methods.

## 8. Observation And Target Model

### 8.1 ObservationSnapshot

```json
{
  "schema_version": "1.0.0",
  "snapshot_id": "OBS-01J...",
  "captured_at": "2026-08-01T10:00:00.123Z",
  "backend": "windows_pywinauto",
  "host": "win26",
  "active_window": {
    "target_id": "T0001",
    "process_name": "EDRClient.exe",
    "pid": 1234,
    "native_window_id": "...",
    "title": "...",
    "rect": [100, 80, 1100, 780]
  },
  "targets": [],
  "tree_digest": "sha256:...",
  "screenshot_evidence_id": "IMG-01J..."
}
```

Target IDs are assigned deterministically within a snapshot after sorting
windows and controls by a stable traversal order. They are not reused as
durable identities across snapshots.

### 8.2 TargetRef

```json
{
  "snapshot_id": "OBS-01J...",
  "target_id": "T0007",
  "expected_process_name": "EDRClient.exe",
  "fingerprint": "sha256:...",
  "selector_hint": {
    "automation_id": "securityWidget",
    "control_type": "Button",
    "text": "Security Center"
  }
}
```

Target resolution order:

1. verify process/window ownership;
2. exact native identity when still valid;
3. exact stable fingerprint;
4. backend-native selector;
5. role/type and normalized accessible text;
6. guarded rectangle proximity only for actions whose catalog permits pointer
   fallback.

Return typed errors: `target_stale`, `target_not_found`, `target_ambiguous`,
`ownership_mismatch`, or `fallback_not_allowed`. Never choose an arbitrary
candidate.

### 8.3 Fingerprints

Fingerprints should use stable fields only. Exclude transient list position,
screen coordinates, and generated numeric control IDs unless no better native
identity exists. Store the fields used to compute a fingerprint so resolution
can explain a mismatch.

## 9. Test Definition Model

### 9.1 TestCase

```json
{
  "schema_version": "1.0.0",
  "case_id": "TC-PROTECTION-001",
  "title": "Open the protection center",
  "description": "Verify that the EDR protection center can be opened",
  "profiles": ["windows_hisec", "macos_hisec"],
  "tags": ["smoke", "navigation"],
  "preconditions": [],
  "steps": [],
  "cleanup": [],
  "timeout_seconds": 120
}
```

`case_id` is stable in source control. `trace_id` identifies one execution of
that case. Reruns never reuse a trace ID.

### 9.2 AtomicTestStep

```json
{
  "step_id": "S003",
  "step_no": 3,
  "title": "Click Protection Center",
  "description": "Open the protection page from the EDR home page",
  "action_id": "gui.click",
  "action_code": "A020",
  "args": {"expected_process_name": "EDRClient.exe"},
  "target": {
    "selector": {"text": "Protection Center", "control_type": "Button"}
  },
  "expectations": [
    {"type": "window_text_contains", "value": "Protection Center", "timeout_seconds": 10}
  ],
  "transition": {"expected": true, "kind": "page_navigation"},
  "evidence": {"screenshot": "before_after"},
  "on_error": "capture_and_abort"
}
```

Atomic means one user-visible intent, not necessarily one transport call. A
step may include target discovery, one action, observation polling, screenshot
capture, and assertion evaluation. It must not contain two independent GUI
mutations such as "open settings and enable protection".

### 9.3 Supported Expectations

Implement a typed registry rather than arbitrary expressions:

| Type | Purpose |
|---|---|
| `action_ok` | action receipt reports success |
| `window_open` | matching process/title/class window exists |
| `window_closed` | matching window no longer exists |
| `active_window_owner` | active window belongs to expected process |
| `control_exists` | unique matching control exists |
| `control_absent` | no matching control exists |
| `control_text_equals` | normalized text equals expected value |
| `control_text_contains` | normalized text contains expected value |
| `window_text_contains` | observed window/tree contains expected text |
| `visual_evidence_captured` | required screenshot was persisted and verified |

Each evaluator returns `passed`, `failed`, or `error`, plus expected, actual,
duration, observation ID, and a safe diagnostic. Unknown expectation types fail
validation before execution.

### 9.4 Case And Step Outcomes

Step statuses: `pending`, `running`, `passed`, `failed`, `blocked`, `skipped`.

- `failed`: action or expectation completed with an incorrect result.
- `blocked`: infrastructure, unavailable backend action, stale prerequisite, or
  recovery failure prevented evaluation.
- `skipped`: declared profile/policy condition excluded the step.
- `passed`: action requirements and all expectations passed.

Case outcome is determined from required steps after cleanup:

1. any required failed step -> `failed`;
2. otherwise any required blocked step -> `blocked`;
3. otherwise all required steps skipped -> `skipped`;
4. otherwise -> `passed`.

Cleanup failure is reported separately and changes a passed case to `failed`
only when the test definition marks cleanup as outcome-critical.

## 10. Sequence Validation Pipeline

Before the first mutation:

1. parse strict JSON and reject unknown fields for protocol models;
2. verify schema version;
3. fetch target status and catalog;
4. verify catalog version/digest;
5. resolve action IDs and validate optional code aliases;
6. validate input arguments against the action schema;
7. check backend enablement and profile constraints;
8. validate dependency references and ensure the graph is acyclic;
9. apply risk, confirmation, and screenshot/checkpoint policy;
10. dry-resolve selectors when an observation is already available;
11. write `plan_validated` or `plan_rejected` before mutation.

Validation errors use stable codes and JSON paths, for example:

```json
{
  "code": "unknown_action_id",
  "path": "steps[2].action_id",
  "message": "Action 'gui.press' is not in catalog 1.0.0"
}
```

## 11. Executor State Machine

```text
CREATED
  -> VALIDATING
  -> OBSERVING
  -> PREPARING_STEP
  -> CAPTURING_BEFORE (policy-dependent)
  -> EXECUTING
  -> OBSERVING_AFTER
  -> CAPTURING_AFTER
  -> ASSERTING
  -> RECORDING
  -> PREPARING_STEP | RECOVERING | FINALIZING
  -> COMPLETED | ABORTED
```

Only the executor mutates runtime state. Report rendering and projections are
pure consumers of persisted events/artifacts.

### Per-Step Algorithm

1. append `step_started`;
2. ensure session and window lock preconditions;
3. obtain/refresh the target observation;
4. resolve `TargetRef` or selector to exactly one target;
5. decide checkpoint and screenshot policy;
6. create pre-action checkpoint when required;
7. capture and durably persist the before image when required;
8. append `action_requested` and `action_started`;
9. invoke `execute_action` with a unique idempotency `request_id`;
10. append `action_result` with the sanitized receipt;
11. capture a fresh observation and classify the transition;
12. capture/persist after or failure screenshots according to policy;
13. evaluate every expectation against fresh observations;
14. append expectation events and one `step_completed` projection source;
15. atomically refresh `step-results.json` and `trace.md`;
16. continue, recover/replan, or abort according to the result and policy.

### Idempotency And Lost Responses

Mutating target calls accept `request_id`. The target keeps a bounded in-memory
receipt cache keyed by request ID for the server lifetime. A duplicate request
returns the original receipt and does not execute the action again. After a
target restart, `server_instance_id` changes. The agent treats only a request
that was in flight on the prior instance and has no receipt as
`action_outcome_unknown`/`blocked`; it must not blindly repeat that mutation.
Genuinely new requests execute normally on the new instance.

## 12. Trace Event Model

### 12.1 Event Envelope

Every line in `events.jsonl` is one complete JSON object:

```json
{
  "schema_version": "1.0.0",
  "trace_id": "TRACE-01J...",
  "branch_id": "BR-001",
  "event_id": "EVT-01J...",
  "sequence_no": 14,
  "parent_event_id": "EVT-01J...",
  "caused_by_event_id": "EVT-01J...",
  "event_type": "action_result",
  "recorded_at": "2026-08-01T10:00:01.123Z",
  "plan_id": "PLAN-01J...",
  "case_id": "TC-PROTECTION-001",
  "step_id": "S003",
  "call_id": "CALL-01J...",
  "payload": {},
  "previous_hash": "sha256:...",
  "event_hash": "sha256:..."
}
```

Use sortable unique IDs such as UUIDv7 or ULID. Do not make correctness depend
on lexical ordering; `sequence_no` and parent links are authoritative.

### 12.2 Event Types

Required V1 event types:

```text
trace_started, environment_recorded, plan_created, plan_validated,
plan_rejected, observation_recorded, step_started, checkpoint_created,
action_requested, action_started, action_result, transition_detected,
unexpected_transition, screenshot_captured, screenshot_persisted,
expectation_result, step_completed, recovery_requested, branch_created,
recovery_result, replan_created, cleanup_started, cleanup_completed,
trace_completed, trace_aborted
trace_recovered
```

Schemas are versioned by event type. Consumers must ignore unknown optional
payload fields but reject an unsupported envelope major version.

### 12.3 Hash Chain

Compute `event_hash` over canonical UTF-8 JSON containing every event field
except `event_hash` itself. The first event uses `previous_hash=null`. A branch
fork event references its checkpoint parent and records the source branch head
in the payload. Verify both parent linkage and hashes when opening/resuming a
trace or finalizing a report.

The hash chain detects corruption; it is not a signature. Authenticity would
require a future signing key and is outside V1.

## 13. Crash-Safe Trace Store

### 13.1 Directory Layout

```text
artifacts/test-runs/<run_id>/
  manifest.json
  report.md
  cases/<safe_case_id>/attempt-<attempt_no>-<trace_id>/
    manifest.json
    trace.md
    events.jsonl
    step-results.json
    observations/<snapshot_id>.json
    screenshots/<step_no>-<step_id>-before.png
    screenshots/<step_no>-<step_id>-after.png
    screenshots/<step_no>-<step_id>-failure.png
```

Sanitize case and step IDs for filenames while retaining original IDs inside
metadata. Reject `..`, separators, control characters, and absolute paths.

### 13.2 Write Rules

- Open `events.jsonl` in append mode and flush after every event.
- Call `fsync` for mutating action results, checkpoint creation, failures, and
  terminal events. Other observation events may use configurable batching.
- Write JSON projections and Markdown to a sibling temporary file, flush it,
  then replace the destination atomically.
- Write evidence bytes to a temporary file, compute and verify digest, then
  atomically rename to the final path before `screenshot_persisted`.
- Never edit an existing event line.
- On startup, diagnose an incomplete final line, retain its byte count and
  digest, truncate to the last complete newline, flush and `fsync`, then append
  `trace_recovered` from the last valid event hash. Reject corruption in any
  earlier line. Only an incomplete, uncommitted crash tail may be truncated.

### 13.3 Manifest

The run manifest records run ID, timestamps, requested targets, environment,
case attempts, renderer version, schema versions, and aggregate status. The
case manifest records trace ID, branch heads, catalog digest, evidence counts,
terminal status, and integrity verification result.

## 14. Screenshot Evidence

### 14.1 Evidence Record

```json
{
  "evidence_id": "IMG-01J...",
  "kind": "screenshot",
  "role": "after",
  "relative_path": "screenshots/003-S003-after.png",
  "media_type": "image/png",
  "sha256": "...",
  "bytes": 184220,
  "width": 1024,
  "height": 700,
  "captured_at": "2026-08-01T10:00:02.010Z",
  "snapshot_id": "OBS-...",
  "event_id": "EVT-...",
  "step_id": "S003",
  "process_name": "EDRClient.exe",
  "pid": 1234,
  "window_title": "EDRClient",
  "redaction": {"applied": false, "rule_ids": []}
}
```

### 14.2 Capture Policy

Default behavior:

| Situation | Evidence |
|---|---|
| case start | baseline screenshot |
| observation-only step | on failure, unless expectation requires visual evidence |
| stable same-page mutation | after screenshot |
| expected navigation/modal/window change | before and after screenshots |
| checkpointed action | before and after screenshots |
| irreversible action | before and after screenshots plus authorization reference |
| any failed/blocked action or expectation | immediate failure screenshot |

The catalog default, test-step override, and runtime safety policy are merged by
choosing the strongest capture level. A test cannot weaken mandatory policy.

### 14.3 Transport

For modest images, the target returns base64 PNG bytes and metadata in the MCP
receipt. For larger images, the existing `screenshot(path=...)` behavior may
write a target-local temporary file, but the agent must retrieve it through the
existing managed transfer layer and verify the digest before recording it.
Never place target-local absolute paths directly in `trace.md`.

Set a configurable maximum image size. If exceeded, preserve the original when
policy allows and create a report preview separately; evidence digest always
refers to the retained original. A capture failure is visible as an expectation
error when the screenshot is required.

### 14.4 Redaction

Apply configured rectangle, window-title, control-selector, and text-pattern
rules before persistence. Store only the redacted image for V1. Action
arguments and observed text use the same secret-pattern registry. Reports show
`[REDACTED]` and list applied redaction rule IDs without exposing matched data.

## 15. Transition Detection And Checkpoints

### 15.1 Transition Detector

Compare before and after snapshots and classify:

- `none`;
- `control_state_change`;
- `page_navigation`;
- `modal_open` or `modal_close`;
- `window_open` or `window_close`;
- `window_owner_change`;
- `application_restart`;
- `unknown_material_change`.

Signals include top-level window set, owner PID/process, native window ID,
active window, modal role, navigation title, tree root, stable target survival,
and normalized tree digest. Coordinates alone never establish a transition.

### 15.2 Checkpoint Decision

Before execution, combine:

1. action catalog transition/risk metadata;
2. test step transition declaration;
3. known SOP transition metadata;
4. selector semantics such as submit/close/delete;
5. current application/session state.

Create a checkpoint before likely navigation, modal/window changes, submit,
close/dismiss, HiSec/EDR ownership switching, or irreversible effects. Ordinary
hover, scroll, observation, and stable same-page toggles do not receive heavy
checkpoints.

### 15.3 Checkpoint Record And Recovery

Every checkpoint declares `logical`, `session`, `application_state`, or
`environment_snapshot`, its restore strategy, verification expectations, and
whether it is genuinely restorable. Recovery always creates a new branch:

```text
failed branch -> recovery_requested
              -> branch_created
              -> restore actions
              -> fresh observation
              -> recovery_result
              -> continue validated remainder or replan
```

Do not use a blind inverse click. If an unexpected transition occurred without
a suitable checkpoint, record evidence, stop the current plan, and re-observe
for a new plan.

## 16. Markdown Rendering

### 16.1 Case `trace.md`

The case trace is regenerated after each completed step and contains:

1. case identity, target/profile, attempt, trace/plan/catalog IDs;
2. start/end time, duration, current/final status;
3. environment and application versions when available;
4. a compact step result table;
5. one section per atomic step in execution order;
6. action, sanitized target/arguments, expectations, and actual results;
7. embedded before/after/failure screenshots using relative paths;
8. retry, branch, checkpoint, and recovery information;
9. errors and cleanup result;
10. integrity/evidence verification status.

Example:

```markdown
## Step 3 - Click Protection Center

**Result:** FAILED  
**Action:** `gui.click` (`A020`)  
**Duration:** 10.42 s  
**Expected:** Window text contains `Protection Center`  
**Actual:** Click succeeded; expected window did not appear within 10 s.

### Before

![Step 3 before](screenshots/003-S003-before.png)

### Failure

![Step 3 failure](screenshots/003-S003-failure.png)

Evidence: `EVT-010` -> `EVT-014`; checkpoint `CP-003`; branch `BR-001`.
```

HTML-escape and Markdown-escape untrusted UI strings, and configure the renderer
to disallow raw HTML. If an image is missing or its digest fails, render a
visible warning with the evidence ID and expected path.

### 16.2 Run `report.md`

The run report contains:

- run identity, command/source, target set, environment, start/end/duration;
- passed/failed/blocked/skipped case counts;
- first-attempt and final-attempt results when reruns exist;
- a table of every case attempt linking to its `trace.md`;
- failed and blocked step summaries with thumbnail/full screenshot links;
- recovery/replan counts;
- evidence and hash-chain integrity summary;
- cleanup failures and infrastructure warnings.

Default headline outcome uses the final attempt, while the report always shows
first-attempt stability separately. This prevents reruns from hiding flaky
behavior.

### 16.3 Rendering Determinism

Given identical manifests, projections, and evidence metadata, the renderer
must produce byte-identical Markdown except for explicitly volatile generation
timestamps. Sort cases by declaration order then attempt number, steps by
`step_no`, and events by branch plus sequence number.

## 17. Security And Safety

- Validate all filesystem components and keep artifacts under the configured
  run root.
- Never persist credentials, tokens, passwords, or raw secret text.
- Require verified window ownership for every GUI mutation.
- Prefer semantic selectors over coordinates.
- Require explicit authorization evidence for irreversible actions.
- Treat screen text as untrusted input to the LLM and report renderer.
- Limit screenshot dimensions, decoded byte size, event payload size, and trace
  length to prevent resource exhaustion.
- Exclude arbitrary system commands from the autonomous catalog.
- Do not claim that a logical checkpoint reversed external state.

## 18. Backward Compatibility And Migration

### Phase A: Catalog Without Behavior Change

1. add catalog models and registry;
2. generate `status.action_space` from the registry;
3. test that every legacy Boolean and MCP tool name is unchanged;
4. expose `get_action_catalog`.

### Phase B: Observation And Single-Action Dispatch

1. add snapshot/target IDs to new observation endpoints;
2. leave legacy `dump_tree`/`find_control` return fields intact;
3. add `execute_action` and route it through existing backend methods;
4. add request-ID receipt caching.

### Phase C: Agent Executor And Trace

1. add strict test/plan models and dry-run validation;
2. execute one action at a time;
3. persist event chains and structured step results;
4. add transition detection and logical/session checkpoints first.

### Phase D: Evidence And Reports

1. add screenshot evidence persistence and redaction;
2. generate incremental case traces;
3. aggregate run reports;
4. add application/environment recovery only where a tested strategy exists.

Each phase must be independently releasable. Existing profile runners may opt
into the new runner case by case; they must not all be rewritten before the
catalog is usable.

## 19. Error Model

All new APIs return a stable envelope:

```json
{
  "ok": false,
  "error": {
    "code": "target_ambiguous",
    "message": "Selector matched 2 controls",
    "retryable": true,
    "details": {"candidate_count": 2}
  },
  "request_id": "REQ-...",
  "observed_at": "2026-08-01T10:00:00Z"
}
```

Do not expose stack traces, credentials, raw selectors containing secrets, or
unbounded backend output. Preserve legacy string JSON envelopes at existing MCP
tools; normalize them inside the new agent executor.

## 20. Testing Strategy

### Unit Tests

- catalog uniqueness, versioning, code alias validation, and digest stability;
- schema rejection of unknown actions/arguments/expectations;
- canonical JSON and event hash-chain verification;
- deterministic target fingerprinting and ambiguity behavior;
- transition classification fixtures;
- checkpoint policy matrix;
- outcome precedence and rerun aggregation;
- path sanitization, Markdown escaping, and redaction;
- deterministic Markdown snapshots;
- evidence digest success, missing file, and corruption handling.

### Integration Tests

- fake MCP target executing a successful multi-step case;
- action success plus expectation failure;
- stale target causing re-observation rather than a coordinate guess;
- duplicate request ID returning one receipt without a second mutation;
- expected navigation producing before/after evidence and a checkpoint;
- unexpected modal causing stop and replan;
- crash after action result but before Markdown refresh, followed by recovery;
- recovery branch preserving the failed branch;
- run report totals reconciling with case attempts.

### Live E2E Tests

- Windows HiSec entry -> EDRClient navigation with screenshots;
- macOS HiSec equivalent flow;
- wrong process/window ownership rejection;
- modal transition and application-state recovery where supported;
- target restart producing an honest unknown/blocked outcome;
- generated `trace.md` renders every retained screenshot through relative links.

Live tests remain skipped when the configured MCP target is unavailable. Unit
and fake-target integration tests must run in ordinary CI without a GUI.

## 21. Observability And Metrics

Record run-level metrics in the manifest:

- action selections and validation failures by action ID;
- semantic versus coordinate action ratio;
- step/case success rate;
- expectation failure categories;
- stale/ambiguous target counts;
- transition and unexpected-transition counts;
- retries, replans, recoveries, and recovery success;
- screenshot count, bytes, capture failures, and redactions;
- execution, observation, assertion, persistence, and rendering durations.

Metrics must not contain raw screen text or secret arguments.

## 22. Definition Of Done

Implementation is complete only when:

1. one immutable catalog drives status, validation, and dispatch;
2. all current action-space entries map to canonical semantic IDs;
3. plans and atomic test cases reject invalid IDs, codes, arguments, and
   expectations before mutation;
4. target references carry observation and ownership evidence;
5. every test execution produces a valid append-only trace;
6. recovery creates a branch and preserves failed history;
7. every mutating atomic step has required visual evidence;
8. case `trace.md` opens with ordered steps and relative screenshot embeds;
9. run `report.md` reconciles with structured case results and exposes reruns;
10. integrity failures and missing screenshots are visible in the report;
11. legacy MCP tools and profile tests remain compatible;
12. unit/fake-target tests pass on non-GUI CI and live E2E passes on supported
    Windows/macOS targets.

## 23. Required Hermes Delivery Shape

Hermes should implement this in small reviewable commits, preferably one phase
per pull request or commit series. Every delivery must include:

- code and focused tests for that phase;
- any schema fixtures and golden Markdown outputs;
- compatibility tests for existing MCP names and `status.action_space`;
- a short migration note when a persisted schema changes;
- no generated run artifacts committed to source control;
- updated TODO checkboxes only for behavior that is implemented and tested.

Review should reject a change that generates attractive Markdown but lacks
authoritative structured evidence, executes stale targets, silently loses failed
branches, stores unredacted secrets, or reports action success as test success.

## 24. Priority Levels And Review Checkpoints

Use the priorities below as implementation gates rather than severity labels:

- `P0`: protocol foundation. Later work must not start until this is stable.
- `P1`: minimum usable test-execution and evidence loop.
- `P2`: robust recovery, aggregation, and production hardening.
- `P3`: LLM autonomy, optimization, and extended evaluation.

Each numbered item is a review checkpoint. Hermes should stop after completing
one checkpoint, provide the listed review package, and wait for review before
starting the next checkpoint. A checkpoint is complete only when its tests pass
and existing tests remain green.

### P0: Stable Protocol Foundation

#### P0.1: Canonical Action Catalog

Scope:

- add `ActionSpec`, enums, all V1 semantic action IDs, and optional codes;
- reject duplicate IDs, codes, and tool names at registry construction;
- implement deterministic catalog version and digest;
- expose backend-specific live enablement;
- generate legacy `status.action_space` from the catalog.

Dependencies: none.

Acceptance gate:

- every current action-space key has exactly one catalog entry;
- Windows/macOS enablement matches current behavior;
- existing `status.action_space` compatibility tests pass unchanged;
- catalog output and digest are deterministic across process runs.

Review package:

- catalog source, unit tests, one Windows fixture, one macOS fixture;
- before/after `status` JSON showing no compatibility regression.

#### P0.2: Wire Models And Validation

Scope:

- add strict models for `ActionSequence`, `ActionStep`, `TargetRef`, test case,
  atomic step, expectation, error envelope, and action receipt;
- reject unknown fields, IDs, codes, parameters, and expectation types;
- add canonical JSON and shared ID generation helpers;
- add dry-run validation without GUI mutation.

Dependencies: `P0.1`.

Acceptance gate:

- valid models round-trip through JSON;
- invalid action/code pairs and catalog digests are rejected before dispatch;
- dependency cycles and missing step references are rejected;
- dry-run cannot call a mutating backend method.

Review package:

- models, exported/example schemas, positive and negative fixtures;
- validation error snapshots containing stable codes and JSON paths.

#### P0.3: Observation And Target Identity

Scope:

- add `snapshot_id`, deterministic observation-local `target_id`, target
  evidence, and fingerprints;
- add backend-neutral `observe` output;
- implement exact/stable/selector/fallback resolution order;
- return typed stale, missing, ambiguous, ownership, and fallback errors;
- preserve legacy observation fields.

Dependencies: `P0.2`.

Acceptance gate:

- the same fixture produces deterministic target ordering/fingerprints;
- stale or ambiguous targets never result in a coordinate click;
- ownership mismatch is rejected on the target side;
- existing `dump_tree` and `find_control` callers still work.

Review package:

- Windows UIA and macOS AX observation fixtures;
- target-remapping and ambiguity tests;
- a compatibility diff for legacy observation responses.

### P1: Minimum Usable Execution And Evidence Loop

#### P1.1: Single-Action Dispatcher And Idempotency

Scope:

- add `execute_action` with semantic ID dispatch into existing backend methods;
- validate live enablement, arguments, preconditions, and target ownership;
- accept `request_id` and cache bounded receipts for duplicate requests;
- retain existing MCP tools as compatibility wrappers.

Dependencies: all `P0` checkpoints.

Acceptance gate:

- every enabled action ID dispatches to the intended existing backend method;
- a duplicate request ID causes one mutation and returns the same receipt;
- disabled/unsupported actions fail with structured errors;
- direct legacy MCP calls remain behaviorally compatible.

Review package:

- dispatcher mapping tests for every action;
- fake-backend call-count tests and representative receipts.

#### P1.2: Atomic Test Executor

Scope:

- implement the agent-side state machine through step assertion and recording;
- execute one mutating action at a time;
- support `abort`, bounded `retry`, and `capture_and_abort`;
- implement core expectations: action result, window open/closed, control
  exists/absent, control text, and window text;
- distinguish failed, blocked, skipped, and passed outcomes.

Dependencies: `P1.1`.

Acceptance gate:

- a fake MCP target can execute a multi-step case end to end;
- `action_ok=true` plus failed expectation produces a failed step;
- infrastructure failure produces blocked rather than failed;
- retry limits and timeouts are deterministic and tested.

Review package:

- executor state transition tests;
- one passing, one assertion-failing, and one blocked case fixture;
- sample structured step results, without Markdown yet.

#### P1.3: Append-Only Trace Core

Scope:

- implement typed event envelopes, canonical hashes, parent links, and branch
  metadata;
- add crash-safe JSONL append and terminal events;
- project events into `step-results.json`;
- verify chains when opening or finalizing a trace.

Dependencies: `P1.2`.

Acceptance gate:

- every executor state change needed for diagnosis is represented by an event;
- event modification, removal, insertion, or reordering is detected;
- an incomplete final line is recovered according to the documented rule;
- projections can be rebuilt entirely from events and immutable evidence.

Review package:

- golden `events.jsonl`, hash-integrity tests, crash fixtures;
- proof that projections are disposable and reproducible.

#### P1.4: Screenshot Evidence And Case Trace

Scope:

- capture baseline, after-action, transition before/after, and failure images;
- transfer target screenshots to the agent artifact store;
- record image dimensions, digest, ownership, timestamps, and redaction state;
- implement minimum secret-pattern/rectangle redaction before persistence;
- incrementally generate one `trace.md` with relative image links.

Dependencies: `P1.3`.

Acceptance gate:

- opening only `trace.md` displays ordered steps and retained screenshots;
- every mutating step has an after screenshot;
- failure and transition steps have evidence required by policy;
- missing or corrupt images are visibly reported;
- untrusted UI text is HTML/Markdown-escaped, raw HTML is disabled, and
  configured secrets are absent.

Review package:

- one complete sample artifact directory from a fake target;
- golden Markdown, screenshot policy matrix, digest and redaction tests;
- visual inspection of `trace.md` on at least one real target when available.

`P1.4` is the MVP milestone. At this point EDR-WD can execute atomic test cases,
retain trustworthy step evidence, and produce a readable per-case record.

### P2: Recovery, Run Reports, And Hardening

#### P2.1: Transition Detection And Checkpoint Policy

Scope:

- classify page, modal, window, owner, restart, and material tree transitions;
- combine catalog, SOP, test declaration, selector, and risk signals;
- create logical/session checkpoints only around meaningful boundaries;
- record unexpected transitions and stop instead of blind inverse actions.

Dependencies: `P1.4`.

Acceptance gate:

- ordinary same-page clicks do not create heavy checkpoints;
- expected navigation and modal actions create pre-action checkpoints;
- unexpected transitions capture evidence and stop/replan;
- policy decisions are deterministic for fixture inputs.

Review package:

- transition fixtures and checkpoint decision table tests;
- trace examples for no-transition, expected, and unexpected transitions.

#### P2.2: Recovery Branches

Scope:

- resume from logical and session checkpoints on a new branch;
- add verified application-state restore only for known/tested SOP strategies;
- preserve failed branches and identify the attempt determining final outcome;
- support `reobserve_replan` without arbitrary loops.

Dependencies: `P2.1`.

Acceptance gate:

- recovery never modifies or deletes failed branch events;
- branch ancestry and hashes verify;
- restored state is freshly observed and expectations are checked;
- unsupported restoration is labeled logical-only or blocked, never successful.

Review package:

- recovery success/failure traces and branch diagrams;
- application restore tests for each strategy admitted to the registry.

#### P2.3: Run-Level Report And Reruns

Scope:

- aggregate case attempts into `report.md` and run manifest;
- show pass/fail/blocked/skipped totals and durations;
- show both first-attempt and final-attempt outcomes;
- link failures to case traces and screenshot evidence;
- reconcile report totals against structured results.

Dependencies: `P2.2`.

Acceptance gate:

- reruns never overwrite previous attempts;
- headline totals use final attempts and stability totals retain first attempts;
- every included case/attempt link resolves within the artifact tree;
- report integrity warnings surface missing/corrupt evidence and trace failures.

Review package:

- golden report covering pass, fail, blocked, skipped, rerun, and recovery;
- reconciliation and deterministic-rendering tests.

#### P2.4: Production Hardening

Scope:

- enforce artifact, image, payload, event-count, and timeout limits;
- complete argument/text/image redaction policy;
- add target restart and unknown-action-outcome handling;
- record metrics without sensitive values;
- run Windows and macOS live E2E evidence flows.

Dependencies: `P2.3`.

Acceptance gate:

- limits fail safely with structured blocked/error outcomes;
- no configured secret appears in events, JSON projections, images, or reports;
- unknown result after target restart is not blindly retried;
- unit/integration suite passes and supported live E2E artifacts render.

Review package:

- security/adversarial tests, redaction audit, resource-limit tests;
- Windows/macOS E2E trace and report samples.

`P2.4` is the production-ready milestone for human-authored test cases.

### P3: LLM Planning And Optimization

#### P3.1: Structured LLM Planner

Scope:

- expose only live enabled actions to the planner;
- require exact `ActionSequence` structured output;
- add planner guidance preferring semantic actions;
- persist sanitized plans and planning outcomes;
- re-observe/replan after invalidating transitions.

Dependencies: all `P2` checkpoints.

Acceptance gate:

- invalid model output cannot reach dispatch;
- coordinate fallback is not selected when a unique semantic target exists;
- catalog mismatch and stale target produce revalidation/replan;
- high-risk actions remain behind deterministic confirmation policy.

#### P3.2: Evaluation And Metrics

Scope:

- add action-selection and sequence-success evaluation datasets;
- measure semantic/coordinate ratio, replans, stale targets, safety rejection,
  recovery success, screenshot cost, and execution latency;
- add regression thresholds without making live GUI availability a unit-CI
  requirement.

Dependencies: `P3.1`.

Acceptance gate:

- evaluation is reproducible from versioned fixtures;
- metrics contain no sensitive text;
- regressions are reported by action/backend/profile and trace back to evidence.

### Recommended Delivery Order

```text
P0.1 -> P0.2 -> P0.3
     -> P1.1 -> P1.2 -> P1.3 -> P1.4 (MVP)
     -> P2.1 -> P2.2 -> P2.3 -> P2.4 (production-ready)
     -> P3.1 -> P3.2
```

Do not parallelize checkpoints that modify the same wire models or trace
schemas. After `P0.2`, platform-specific observation fixture work may proceed
in parallel, and after `P1.3`, Markdown rendering tests may be prepared in
parallel with target screenshot transport, provided both use the frozen
evidence model.
