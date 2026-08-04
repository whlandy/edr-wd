# TODO: Versioned Action IDs And LLM Action Sequences

## Implementation Status

The original Phase 1–7 checklist below is retained as the design baseline.
Implementation through P3.2 is complete for the catalog, observations,
protocol models, dispatcher, executor, trace/evidence, recovery, reports,
planner, and offline evaluation pipeline.

Remaining external acceptance work is limited to live HiSec planner scenarios
(stale tree, ambiguous target, wrong ownership, and a mid-sequence dialog).
Those behaviours now have a deterministic stub-LLM E2E acceptance suite in
`test_case/test_planner_e2e/test_todo_scenarios.py`; the equivalent live tests
remain environment-gated. Packaging/PyInstaller is tracked separately under
`packaging/` and environment-snapshot restoration remains explicitly out of
scope for V1.

## Goal

Turn the current backend capability map into a versioned, machine-readable
action catalog that an LLM can use to select one action or emit an ordered
action sequence. Use stable semantic string IDs as the protocol identity;
numeric codes are optional transport compression only. The sequence must be
schema-validated, backend-aware, safe to replay, and explicit about the
observed window/control targeted by each step.

## Browser-Use Research

Browser-use separates two concepts:

1. Action types are registered under stable names and exposed to the LLM
   through a generated, validated action model.
2. Interactive page elements receive observation-local numeric indices in the
   current selector map.

For history replay, browser-use does not trust an old index alone. It stores
evidence about the interacted element and remaps it against the new page using
exact/stable hashes and selector/accessibility fallbacks.

EDR-WD should follow the same separation. A stable action ID identifies the
operation. A target ID identifies a window/control in one observation. They
must never share one ID namespace.

Primary references:

- Browser-use default actions use named operations such as `click`, `input`,
  `scroll`, and `select_dropdown`.
- Custom actions are added through the tool/action registry and validated
  parameter models.
- Click/input actions address current interactive elements by index; history
  replay attempts to remap stored element evidence when the page changes.

## OpenAI Computer-Use Research

The current built-in computer-use protocol follows an observation/action loop:

1. The model may request a screenshot before committing to an action.
2. The model returns one `computer_call` with a unique `call_id` and an ordered
   `actions[]` array.
3. Each action uses a semantic string `type`, such as `click`, `type`, `scroll`,
   `keypress`, `drag`, `move`, `wait`, or `screenshot`.
4. The harness executes the batch, captures the updated screen, and sends a
   `computer_call_output` linked by `call_id`.
5. The loop continues until no further `computer_call` is returned.

`call_id` identifies the invocation/batch, not the action type. Computer-use
therefore also does not assign fixed numeric IDs such as `A020` to action
semantics.

OpenAI explicitly supports existing custom MCP/Playwright/VNC harnesses; EDR-WD
does not need to replace its mature semantic automation with raw coordinates.
The useful pattern is the screenshot/observation feedback loop, ordered batched
actions, call/result correlation, and confirmation at the point of risk.

## Adopted Hybrid

Use the strongest part of each model:

- Browser-use: named action registry, validated parameter models,
  observation-local element references, and history target remapping.
- Computer-use: ordered action batches, invocation `call_id`, post-batch
  screenshot/observation feedback, and explicit safety/confirmation boundary.
- EDR-WD: process/window ownership, window locks, Windows UIA/macOS AX semantic
  selectors, and coordinate fallback only when semantic actions fail.

Do not make an opaque numeric code the canonical LLM vocabulary. The canonical
ID should be a stable namespaced string such as `gui.click`; it is readable in
traces, naturally debuggable, and matches the protocols that models are already
trained to emit.

## Current EDR-WD Gap

`status.action_space` currently exposes only capability booleans:

```json
{"click": true, "type_text": true, "select": false}
```

It does not expose stable IDs, parameter schemas, backend constraints,
preconditions, risk/side-effect classes, catalog version, or sequence schema.

`dump_tree.control_id` is also unsuitable as a durable plan token. It is tied
to one tree snapshot and may change after navigation, window recreation,
backend change, or another dump.

## Three Identifier Namespaces

### Stable Semantic Action ID

`action_id` identifies one catalog operation.

- Format: stable namespaced string, for example `gui.click`.
- Never reuse an ID for different semantics after release.
- Keep an ID when semantics remain compatible; allocate a new semantic ID for
  breaking behavior changes.
- Include `catalog_version` and `catalog_digest` in catalogs and plans.
- Keep the MCP tool name as a compatibility alias.
- Optionally include `action_code` such as `A020` for compact storage or model
  tokens, but always validate it against the canonical `action_id`.

### Observation-Scoped Target ID

`target_id` identifies a window/control in one observation, for example
`T0007`. It is valid only with its `snapshot_id`:

```json
{
  "target_id": "T0007",
  "snapshot_id": "OBS-01J...",
  "kind": "control",
  "process_name": "EDRClient.exe",
  "selector": {
    "automation_id": "securityWidget",
    "control_type": "Button",
    "text": "安全中心"
  },
  "fingerprint": "sha256:..."
}
```

Resolve a target before execution in this order:

1. exact process/window ownership;
2. exact stable fingerprint;
3. backend-native stable selector;
4. role/type plus accessible text;
5. rectangle proximity only as a guarded final fallback.

If resolution is ambiguous, return `target_stale` or `target_ambiguous` and
re-observe/replan. Never guess a coordinate.

### Sequence Step ID

`step_id` identifies ordering/dependencies inside one plan, for example
`S001`. It is neither an action nor a target identifier.

## Proposed Action Catalog V1

These IDs cover every action currently advertised by `status.action_space`.
The compact code is optional and never authoritative.

| Canonical action ID | Code | MCP tool | Category | Target requirement |
|---|---|---|---|---|
| `session.connect` | `A001` | `connect` | session | window/process selector |
| `session.window_lock.set` | `A002` | `lock_window` | session safety | connected/specified window |
| `session.window_lock.clear` | `A003` | `unlock_window` | session safety | none |
| `observe.window_lock` | `A004` | `get_window_lock` | observation | none |
| `session.window_lock.verify` | `A005` | `verify_window_lock` | session safety | existing lock |
| `app.activate` | `A006` | `activate_app` | application | app name/bundle ID |
| `observe.control_tree` | `A010` | `dump_tree` | observation | connected window |
| `observe.find_control` | `A011` | `find_control` | observation | connected window |
| `gui.click` | `A020` | `click` | semantic input | control target |
| `gui.click_target` | `A021` | `click_target` | pointer fallback | control target |
| `pointer.click_screen` | `A022` | `click_at` | pointer fallback | screen coordinate |
| `pointer.click_window` | `A023` | `click_window_at` | pointer fallback | window + relative coordinate |
| `pointer.double_click` | `A024` | `double_click_at` | pointer fallback | screen coordinate |
| `pointer.right_click` | `A025` | `right_click_at` | pointer fallback | screen coordinate |
| `pointer.middle_click` | `A026` | `middle_click_at` | pointer fallback | screen coordinate |
| `pointer.hover` | `A027` | `hover_at` | pointer | screen coordinate |
| `pointer.drag` | `A028` | `drag` | pointer | start/end coordinates |
| `pointer.scroll` | `A029` | `scroll` | pointer | optional coordinate/window |
| `gui.type_text` | `A030` | `type_text` | semantic input | editable control target |
| `gui.select` | `A031` | `select` | semantic input | selectable control target |
| `observe.control_text` | `A032` | `get_text` | observation | control target |
| `observe.screenshot` | `A040` | `screenshot` | observation | connected window |
| `hisec.activate_edr` | `A050` | `activate_edr` | HiSec workflow | HiSec profile |
| `hisec.restore_edr` | `A051` | `restore_edr` | HiSec workflow | connected EDR window |
| `observe.windows` | `A060` | `list_windows` | observation | none |
| `observe.window_open` | `A061` | `is_window_open` | observation | window/process selector |
| `observe.wait_window` | `A062` | `wait_window` | observation | window/process selector |

Reserve `A070-A079` for guarded PowerShell operations if they are later
admitted. Do not expose arbitrary PowerShell to an autonomous planner by
default.

## Catalog Schema

Add one source of truth at `target/action_catalog.py`. Each action should carry:

```json
{
  "action_id": "gui.click",
  "action_code": "A020",
  "name": "click",
  "catalog_version": "1.0.0",
  "description": "Trigger one uniquely resolved semantic control",
  "input_schema": {},
  "backends": ["windows_pywinauto", "macos_accessibility"],
  "requires": ["connected_window", "window_lock", "target_ref"],
  "side_effect": "gui_mutation",
  "risk": "medium",
  "reversible": false,
  "preferred_over": ["gui.click_target", "pointer.click_screen", "pointer.click_window"],
  "result_schema": {},
  "enabled": true
}
```

Generate MCP metadata and `status.action_space` from this catalog. Do not keep
a second hard-coded capability table in `target/server.py`.

Add `get_action_catalog(backend?, include_disabled=false)` and return a
deterministic `catalog_digest`.

## LLM Sequence Schema

Do not use a bare list such as `["A001", "A020"]`; steps need arguments,
targets, guards, dependencies, and expected observations.

```json
{
  "schema_version": "1.0.0",
  "catalog_version": "1.0.0",
  "catalog_digest": "sha256:...",
  "plan_id": "PLAN-01J...",
  "target": "2.26-edr-win26-win11",
  "steps": [
    {
      "step_id": "S001",
      "action_id": "session.connect",
      "action_code": "A001",
      "args": {"process_name": "EDRClient.exe"},
      "on_error": "abort"
    },
    {
      "step_id": "S002",
      "action_id": "session.window_lock.set",
      "action_code": "A002",
      "args": {"process_name": "EDRClient.exe", "strict": true},
      "depends_on": ["S001"],
      "on_error": "abort"
    },
    {
      "step_id": "S003",
      "action_id": "gui.click",
      "action_code": "A020",
      "target_ref": {"target_id": "T0007", "snapshot_id": "OBS-01J..."},
      "args": {"expected_process_name": "EDRClient.exe"},
      "depends_on": ["S002"],
      "expect": {"window_text_contains": "防护中心"},
      "on_error": "reobserve_replan"
    }
  ]
}
```

The executor resolves `action_id -> catalog entry -> MCP tool`, verifies the
optional code, validates arguments, verifies ownership/preconditions, executes
one step or a safe batch, captures a receipt/observation, and determines whether
the remaining plan is still valid. Give each execution batch a `call_id` and
link every returned receipt to it. Do not blindly execute a mutating batch when
an intermediate UI transition could invalidate later targets.

## Chained Execution Trace And Checkpoints

Treat a trace as an append-only causal event chain, not merely a flat action
history. Every observation, decision, action, result, checkpoint, recovery, and
replan is an event.

### Trace Identity And Links

Use these identifiers:

- `trace_id`: one end-to-end task execution, including recovery branches.
- `branch_id`: one linear attempt within the trace.
- `event_id`: one immutable event.
- `parent_event_id`: direct causal predecessor on the current branch.
- `caused_by_event_id`: optional cross-link to the decision/action that caused
  an observation or error.
- `call_id`: one action batch/invocation.
- `plan_id` and `step_id`: link runtime evidence back to the planned sequence.
- `checkpoint_id`: one approved recovery point.

Each event should include `previous_hash` and `event_hash`, computed from a
canonical JSON representation, so corruption or accidental reordering can be
detected. A branch keeps one `head_event_id`; a trace may have multiple branch
heads after recovery or alternative attempts.

```json
{
  "trace_id": "TRACE-01J...",
  "branch_id": "BR-001",
  "event_id": "EVT-004",
  "parent_event_id": "EVT-003",
  "caused_by_event_id": "EVT-002",
  "event_type": "action_result",
  "sequence_no": 4,
  "plan_id": "PLAN-01J...",
  "step_id": "S003",
  "call_id": "CALL-002",
  "action_id": "gui.click",
  "status": "succeeded",
  "before_snapshot_id": "OBS-001",
  "after_snapshot_id": "OBS-002",
  "previous_hash": "sha256:...",
  "event_hash": "sha256:...",
  "recorded_at": "2026-08-01T10:00:01Z"
}
```

Recommended event types:

```text
trace_started
observation_recorded
plan_created
action_requested
action_started
action_result
expectation_result
checkpoint_created
recovery_requested
recovery_result
branch_created
replan_created
trace_completed
trace_aborted
```

### Checkpoint Model

A checkpoint is a recovery contract, not only a marker. Record what was
captured and what level of restoration is actually possible:

```json
{
  "checkpoint_id": "CP-003",
  "trace_id": "TRACE-01J...",
  "branch_id": "BR-001",
  "event_id": "EVT-010",
  "kind": "application_state",
  "snapshot_id": "OBS-004",
  "restorable": true,
  "restore_strategy": "inverse_actions",
  "restore_payload": {
    "actions": [
      {"action_id": "session.connect", "args": {"process_name": "EDRClient.exe"}},
      {"action_id": "gui.click", "target_selector": {"text": "返回"}}
    ]
  },
  "preconditions": {"process_name": "EDRClient.exe"},
  "state_digest": "sha256:..."
}
```

Support four checkpoint levels and report them honestly:

1. `logical`: restore planner/executor context and re-observe; no claim that UI
   state was reversed.
2. `session`: reconnect the application, restore/activate the expected window,
   and recreate the lock.
3. `application_state`: execute tested inverse/navigation actions to return to
   a known page or dialog state.
4. `environment_snapshot`: restore a VM/container/application snapshot when
   infrastructure provides one.

Do not label a checkpoint `restorable=true` unless its restore strategy has a
deterministic implementation and verification rule.

### When To Create A Checkpoint

Do not create a checkpoint before every click. Trace every action, but create a
recovery checkpoint only at a likely UI state boundary.

Create a checkpoint immediately before an action that may:

- open a new top-level window, child window, modal, dialog, drawer, or wizard;
- navigate to a different page/tab/view or change the application navigation
  level;
- submit a form or commit a staged UI operation;
- close/dismiss the current window or leave a page whose state is expensive to
  reconstruct;
- launch `EDRClient` from the HiSec entry window or switch ownership between
  `HisecEndpointAgent` and `EDRClient`;
- trigger an irreversible or externally visible effect.

Usually do not create a checkpoint for:

- observation actions such as `dump_tree`, `find_control`, `get_text`,
  `screenshot`, or window-status checks;
- pointer movement, hover, or scrolling that does not change navigation state;
- a semantic click known to toggle/select a control within the same stable
  page, unless that control can trigger a dialog or navigation;
- repeated field entry while the executor remains in the same form and no
  submission has occurred.

The planner may mark an expected transition:

```json
{
  "step_id": "S003",
  "action_id": "gui.click",
  "transition": {
    "expected": true,
    "kind": "modal_open",
    "checkpoint_before": true,
    "expected_window_owner": "EDRClient.exe"
  }
}
```

The deterministic executor makes the final checkpoint decision. It should use
catalog metadata, known SOP transitions, selector semantics, and risk policy;
it must not rely only on the LLM's prediction.

After the action, compare the before/after observations. Treat any of these as
an actual transition:

- top-level window set, owner PID/process, or active window changed;
- connected window handle/identity changed;
- modal/dialog appeared or disappeared;
- page signature, navigation title, or control-tree root changed materially;
- expected target controls disappeared because the application moved to a new
  view.

If no transition occurred, keep the checkpoint as a lightweight logical marker
or release its heavy screenshot/tree artifacts according to retention policy.
If an unexpected transition occurred without a pre-action checkpoint, append
an `unexpected_transition` event, capture the new observation immediately, and
stop for replan instead of attempting a blind inverse click.

### Recovery Is A New Branch

Never delete or rewrite failed events. `resume_from_checkpoint(CP-003)` should:

1. append `recovery_requested` to the failing branch;
2. create a new `branch_id` whose `forked_from_event_id` is the checkpoint's
   event;
3. execute the checkpoint restore strategy;
4. record a fresh observation and compare its digest/expectations;
5. append `recovery_result`;
6. continue the old remaining plan only if still valid, otherwise append a new
   `replan_created` event.

```text
EVT-001 -> EVT-002 -> EVT-003(CP-001) -> EVT-004 -> EVT-005(failed)
                             |
                             +-> BR-002/EVT-006(recover)
                                      -> EVT-007(observe)
                                      -> EVT-008(replan)
```

This gives the trace a chain-like feel while preserving failed attempts as
useful training/evaluation evidence.

### Rollback Limits

GUI automation cannot promise database-style rollback. Classify every action:

- `reversible`: a tested inverse action exists and can be verified.
- `reconstructable`: no inverse, but session/application state can be rebuilt
  from a known checkpoint.
- `logical_only`: only planner context can be rewound; UI must be re-observed.
- `irreversible`: external submission, deletion, policy change, password
  change, or another action whose effects cannot be safely undone.

Before an `irreversible` action, force a checkpoint, record explicit user
authorization, and understand that returning to the checkpoint does not undo
the external effect.

### Trace Storage

Use append-only JSON Lines for the initial implementation:

```text
artifacts/traces/<trace_id>/events.jsonl
artifacts/traces/<trace_id>/observations/<snapshot_id>.json
artifacts/traces/<trace_id>/screenshots/<snapshot_id>.png
artifacts/traces/<trace_id>/manifest.json
artifacts/traces/<trace_id>/trace.md
```

Store large screenshots/control trees by content digest and reference them from
events. Redact secrets from action arguments and text observations before
persistence. Preserve enough selector/window evidence to reproduce target
resolution without storing unnecessary sensitive screen content.

## Atomic Test Steps And Visual Evidence

The primary execution unit for a test is an atomic test step. An atomic step
describes one user-visible intent and may resolve to one action or a small
bounded action batch. Keep setup, interaction, and verification separate so a
failure can be attributed precisely.

```json
{
  "case_id": "TC-PROTECTION-001",
  "case_title": "Open the protection center",
  "step_id": "S003",
  "step_no": 3,
  "title": "Click Protection Center",
  "description": "Open the Protection Center from the EDR home page",
  "action_id": "gui.click",
  "target": {"text": "Protection Center", "control_type": "Button"},
  "expected": {
    "window_text_contains": "Protection Center",
    "screenshot": "after"
  },
  "on_error": "capture_and_abort"
}
```

Each executed step must produce a `step_result` projection linked to the
underlying trace events. It includes status (`passed`, `failed`, `blocked`, or
`skipped`), start/end timestamps, duration, resolved action and target,
sanitized arguments, actual result, expectation results, error details,
checkpoint/branch references, and evidence references. The projection is a
read model; `events.jsonl` remains the authoritative append-only history.

### Screenshot Policy

Screenshots are first-class evidence rather than incidental debug output.

- Capture a baseline screenshot before the first executable test step.
- Capture an `after` screenshot for every mutating atomic step.
- Capture both `before` and `after` for navigation, modal/window transitions,
  form submission, destructive actions, and steps with a checkpoint.
- Capture a screenshot when a visual expectation is evaluated, even for an
  observation-only step.
- On failure, capture the current screen immediately and, when useful, the
  active window plus a full-screen context image.
- Permit `screenshot: none|after|before_after|on_failure` overrides, but safety
  policy may increase evidence capture and must not silently reduce it.

Every screenshot reference should contain its relative path, SHA-256 digest,
capture timestamp, window/process identity, dimensions, redaction status, and
the `snapshot_id`, `event_id`, and `step_id` that produced it. Use deterministic
names that remain readable outside the renderer:

```text
screenshots/003-S003-before.png
screenshots/003-S003-after.png
screenshots/003-S003-failure.png
```

Redact configured secret regions and sensitive text before persistence. Record
that redaction occurred; never embed an unredacted temporary image in the
Markdown report.

### One-File-Entry Markdown Trace

Generate `trace.md` incrementally after every completed step using atomic file
replacement. It is the human-readable entry point for one execution and embeds
screenshots using relative Markdown paths, so opening this one file shows the
ordered steps and visual evidence without requiring a separate viewer.

```markdown
# TC-PROTECTION-001 - Open the protection center

- Result: FAILED
- Target: 2.26-edr-win26-win11
- Trace: TRACE-01J...
- Started: 2026-08-01T10:00:00Z

## Step 3 - Click Protection Center

**Result:** FAILED  
**Action:** `gui.click`  
**Expected:** Window text contains `Protection Center`  
**Actual:** Click succeeded, expected window did not appear within 10 s.

Before:

![Step 3 before](screenshots/003-S003-before.png)

After/failure:

![Step 3 failure](screenshots/003-S003-failure.png)

Evidence: `EVT-010` -> `EVT-014`, checkpoint `CP-003`, branch `BR-001`
```

The report renderer must escape untrusted UI text, use relative artifact links,
show placeholders for missing/corrupt evidence, and verify screenshot digests
before marking a report complete. The Markdown is generated from structured
results and must never be parsed back as execution state.

Recommended per-execution layout:

```text
artifacts/test-runs/<run_id>/
  manifest.json
  report.md
  cases/<case_id>/
    trace.md
    step-results.json
    events.jsonl
    observations/
    screenshots/
```

`trace.md` documents one test case execution. `report.md` summarizes the whole
run with environment metadata, pass/fail/blocked/skipped counts, durations,
failed-step summaries, and links to each case trace. A case rerun receives a
new `trace_id`/attempt directory; previous evidence is retained.

### Test Execution Flow

```text
test case -> normalize atomic steps -> validate action IDs and expectations
          -> start trace -> execute one step -> capture evidence -> assert
          -> append events/result -> refresh trace.md -> continue or recover
          -> finalize case trace -> aggregate run report.md
```

The executor helps perform each atomic step, but the test definition owns the
expected result. An action succeeding does not make the test step pass: all
declared expectations must pass. Recovery attempts remain visible in the same
case trace as separate branches, while the final step result states which
attempt determined the outcome.

## Planner/Executor Boundary

```text
observe -> target map -> LLM plan -> schema/policy validation
        -> execute one or bounded steps -> observe/verify -> continue/replan
```

The LLM may choose IDs, arguments, target references, and order. Deterministic
code owns catalog lookup, schema/backend validation, ownership checks,
stale-target detection, safety policy, retries/timeouts, and redaction.

## Safety Policy

- `observation`: no GUI mutation; safe to batch.
- `session`: connection/lock state only.
- `gui_mutation`: click/type/select/drag/scroll; require target ownership.
- `system_mutation`: PowerShell/lifecycle changes; excluded by default or gated
  by explicit policy/approval.

HiSec mutations require `expected_process_name` and a verified window lock.
Coordinate actions have higher risk than semantic actions and should not be
selected while a unique semantic target exists.

## Implementation Plan

Detailed priority gates, per-checkpoint acceptance criteria, and Hermes review
packages are defined in
[`../architecture/01-action-trace-test-report-design.md`](../architecture/01-action-trace-test-report-design.md#24-priority-levels-and-review-checkpoints)
and the per-level requirements documents under
[`../requirements/`](../requirements/). Use `P0.1` through `P3.2` as the
delivery and review sequence; the phases below remain the capability
checklist.

### Phase → Checkpoint Mapping

The phases below are the **capability** checklist. Each phase is decomposed
into one or more review checkpoints from architecture §24. Every checkpoint
must be reviewed and approved before the next begins.

| Phase (capability) | Checkpoint (review gate) | Requirement doc | Status |
|--------------------|--------------------------|------------------|--------|
| Phase 1: Catalog Foundation | P0.1 Canonical Action Catalog | [`../requirements/P0-protocol-foundation.md`](../requirements/P0-protocol-foundation.md#checkpoint-p01--canonical-action-catalog) | UNDONE |
| Phase 2: Observation Target Map | P0.2 Wire Models And Validation + P0.3 Observation And Target Identity | [`../requirements/P0-protocol-foundation.md`](../requirements/P0-protocol-foundation.md) | UNDONE |
| Phase 3: Sequence Models And Executor | P0.2 (models) + P1.1 (dispatcher) + P1.2 (atomic executor) | [`../requirements/P0-protocol-foundation.md`](../requirements/P0-protocol-foundation.md), [`../requirements/P1-execution-evidence-mvp.md`](../requirements/P1-execution-evidence-mvp.md) | UNDONE |
| Phase 4: Trace And Recovery | P1.3 Append-Only Trace Core + P2.1 Transition/Checkpoint + P2.2 Recovery Branches | [`../requirements/P1-execution-evidence-mvp.md`](../requirements/P1-execution-evidence-mvp.md), [`../requirements/P2-recovery-production.md`](../requirements/P2-recovery-production.md) | UNDONE |
| Phase 5: Test Evidence And Reports | P1.4 Screenshot Evidence And Case Trace + P2.3 Run-Level Report And Reruns | [`../requirements/P1-execution-evidence-mvp.md`](../requirements/P1-execution-evidence-mvp.md), [`../requirements/P2-recovery-production.md`](../requirements/P2-recovery-production.md) | UNDONE |
| Phase 6: LLM Planning | P3.1 Structured LLM Planner | [`../requirements/P3-llm-evaluation.md`](../requirements/P3-llm-evaluation.md) | UNDONE |
| Phase 7: Evaluation | P2.4 Production Hardening + P3.2 Evaluation And Metrics | [`../requirements/P2-recovery-production.md`](../requirements/P2-recovery-production.md), [`../requirements/P3-llm-evaluation.md`](../requirements/P3-llm-evaluation.md) | UNDONE |

Notes on the mapping:

- **P0.1 alone covers Phase 1.** Phase 1's deliverables (typed
  `ActionSpec`, catalog version/digest, `status.action_space` regenerated
  from the registry) are exactly P0.1.
- **Phase 2 splits across P0.2 and P0.3.** Adding `snapshot_id` /
  `target_id` to legacy observation tools is P0.3; the `TargetRef`
  model is P0.2; deterministic fingerprints are P0.3.
- **Phase 3 is the gateway to execution.** Strict models land in P0.2,
  the `execute_action` dispatcher in P1.1, and the atomic executor in
  P1.2.
- **Phase 4 is mostly P1.3 + P2.1 + P2.2.** The trace chain is P1.3;
  checkpoint policy and recovery execution are P2.1 / P2.2.
- **Phase 5 closes the user-facing loop.** Per-case `trace.md` is the
  P1.4 MVP; run-level `report.md` is P2.3.
- **Phase 6 is P3.1.** Structured-output LLM planner with confirmation
  gate.
- **Phase 7 covers production hardening (P2.4) and evaluation (P3.2).**

### Status Of Existing Code (Not Counted As Progress)

The existing `target/server.py` MCP tools, `target/automation/*`
backend implementations, and `test_case/` profile runners are the
**migration baseline**, not checkpoint progress. They remain
backward-compatible while each P-level checkpoint lands. In
particular:

- The hard-coded `status.action_space` maps in `target/server.py`
  are replaced by a catalog call in **P0.1**.
- The legacy observation endpoints (`list_windows`, `dump_tree`,
  `find_control`) gain `snapshot_id` and per-control target IDs in
  **P0.3**.
- Every legacy MCP tool remains a valid caller after **P1.1**.

Marking any of these existing pieces as "done" against a checkpoint
without the corresponding new code and tests would be incorrect.

### Phase 1: Catalog Foundation

- [ ] Add typed `ActionSpec` and immutable V1 semantic ID registry.
- [ ] Treat numeric action codes as optional aliases and test ID/code agreement.
- [ ] Encode backend support and JSON input schema for every action.
- [ ] Generate the compatibility `status.action_space` map from the registry.
- [ ] Add catalog version/digest to `status` and expose catalog retrieval.
- [ ] Test duplicate IDs/names, ID stability, schemas, and deterministic digest.

### Phase 2: Observation Target Map

- [ ] Add `snapshot_id` to `list_windows`, `dump_tree`, and `find_control`.
- [ ] Return target IDs and backend-neutral window/control fingerprints.
- [ ] Preserve native selectors without treating `control_id` as durable.
- [ ] Implement exact/stable/fallback resolution and ambiguity errors.
- [ ] Invalidate target IDs after window/tree-changing actions.

### Phase 3: Sequence Models And Executor

- [ ] Add Pydantic `ActionSequence`, `ActionStep`, `TargetRef`, guard,
      expectation, and error-policy models.
- [ ] Validate catalog version/digest and input schemas.
- [ ] Add a dry-run endpoint that resolves steps without GUI mutation.
- [ ] Add deterministic execution with per-step receipts and bounded retries.
- [ ] Initially support `abort`, `retry`, and `reobserve_replan`, not arbitrary
      branches/loops.

### Phase 4: Trace And Recovery

- [ ] Add typed trace events, canonical serialization, and hash-chain checks.
- [ ] Persist append-only JSONL plus content-addressed observation artifacts.
- [ ] Add checkpoint creation policies before risky/state-changing steps.
- [ ] Detect window/page/modal transitions from before/after observations and
      avoid checkpoints for ordinary same-page clicks.
- [ ] Implement logical, session, application, and environment restore
      strategies with verification.
- [ ] Resume from a checkpoint by creating a new branch; never rewrite history.
- [ ] Add trace queries by trace/branch/plan/step/call/checkpoint ID.
- [ ] Capture baseline, per-step, transition, checkpoint, and failure
      screenshots with digests and redaction metadata.

### Phase 5: Test Evidence And Reports

- [ ] Add typed test case, atomic step, expectation, step-result, and evidence
      models with stable case/step IDs.
- [ ] Project trace events into `step-results.json` without making the
      projection authoritative execution state.
- [ ] Generate and incrementally refresh one readable `trace.md` per case with
      relative screenshot embeds.
- [ ] Generate one run-level `report.md` with environment, totals, durations,
      failures, retries, and links to case traces.
- [ ] Verify artifact digests and surface missing/corrupt screenshots in the
      report instead of silently omitting them.
- [ ] Preserve every rerun/branch and identify the attempt used for the final
      test outcome.

### Phase 6: LLM Planning

- [ ] Give the LLM only actions enabled for the live backend/profile/policy.
- [ ] Require structured output matching `ActionSequence` exactly.
- [ ] Prefer semantic actions over coordinate fallbacks in planner guidance.
- [ ] Re-observe/replan after state-changing steps.
- [ ] Persist plans/results with sensitive argument redaction.

### Phase 7: Evaluation

- [ ] Unit-test catalog stability, target remapping, stale references, schemas,
      and JSON round trips.
- [ ] Test event hash integrity, branch ancestry, checkpoint recovery,
      irreversible-action guards, and crash-safe append/resume.
- [ ] Add simulated Windows UIA and macOS AX sequence tests.
- [ ] Add live HiSec E2E for successful plans, stale trees, ambiguity, wrong
      ownership, and mid-sequence dialogs.
- [ ] Measure valid action selection, sequence success, replans/task,
      semantic-vs-coordinate ratio, and unsafe-action rejection.
- [ ] Snapshot-test Markdown rendering, screenshot linkage, redaction, partial
      crash recovery, rerun aggregation, and report totals.

## Acceptance Criteria

- Every LLM-visible action has exactly one immutable semantic action ID.
- Numeric action codes, when present, resolve to that semantic ID and are never
  accepted alone without catalog-version validation.
- One catalog drives status, planning schema, validation, and dispatch.
- Every mutating target reference includes snapshot and ownership evidence.
- Stale/ambiguous targets never cause unverified coordinate clicks.
- Plans round-trip through JSON/Pydantic and reject unknown IDs/parameters.
- Existing MCP names and `status.action_space` remain backward compatible.
- Equivalent Windows/macOS semantics share an ID while capability flags remain
  backend-specific.
- Trace events form a verifiable parent/hash chain and remain append-only.
- Recovery creates a new branch from a checkpoint and preserves failed events.
- Every checkpoint declares its actual restoration level; logical rewind is not
  misrepresented as UI or external-state rollback.
- Every executed atomic test step has a structured result linked to its trace
  events and declared expectations.
- Every mutating step has after-action visual evidence; transitions, failures,
  and checkpoints retain before/after evidence according to policy.
- Opening a case's `trace.md` shows ordered steps, outcomes, expectations, and
  screenshots through relative links; no custom UI is required.
- A run-level `report.md` is generated from structured results and its totals
  reconcile with all included case attempts.

## Open Decisions

- Semantic versioning or integer epoch for catalog compatibility?
- Store selector evidence inline or by immutable observation digest?
- Which GUI mutations require confirmation in autonomous mode?
- Add branches/loops later, or rely on replan after each observation?
- How long may a target snapshot remain valid?
- Which actions receive automatic checkpoints, and what storage budget applies
  to screenshots/control trees?
- Which HiSec pages have tested inverse actions versus logical-only recovery?
- Which UI regions/text patterns require default screenshot redaction?
- Should run reports use the latest attempt, first attempt, or both for the
  headline pass rate?
