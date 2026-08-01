# P2 — Recovery, Reports, And Production Hardening

## Status

- State: requirements (no implementation yet)
- Source contract: `../architecture/01-action-trace-test-report-design.md` §24, §15, §16, §17, §21
- Mapped checkpoints: **P2.1 Transition Detection And Checkpoint Policy**, **P2.2 Recovery Branches**, **P2.3 Run-Level Report And Reruns**, **P2.4 Production Hardening**
- Completion state: **all four checkpoints UNDONE**
- Milestone: **P2.4 is the production-ready milestone for human-authored
  test cases**. After P2.4, EDR-WD can run a multi-case suite, retain
  trustworthy evidence, aggregate a run report, and survive target
  restarts without silently losing or replaying work.

P2 must wait for P1.4 (MVP) to be complete and reviewed.

---

## Checkpoint P2.1 — Transition Detection And Checkpoint Policy

### Scope

Classify transitions between before/after snapshots. Build the
deterministic checkpoint decision pipeline that combines catalog
metadata, SOP metadata, step declarations, selector semantics, and
current session state. Create only meaningful checkpoints; record
unexpected transitions and stop instead of attempting blind inverse
actions.

### In Scope

- `agent/execution/transitions.py`:
  - `classify_transition(before, after) -> TransitionResult` returning
    one of `none | control_state_change | page_navigation | modal_open
    | modal_close | window_open | window_close | window_owner_change
    | application_restart | unknown_material_change`.
  - Signals: top-level window set, owner PID/process, native window
    ID, active window, modal role, navigation title, tree root,
    stable target survival, normalized tree digest. **Coordinates
    alone never establish a transition.**
- `agent/execution/checkpoints.py`:
  - `decide_checkpoint(step, snapshot, catalog, sop_index) ->
    CheckpointDecision` returning one of `none | logical | session |
    application_state | environment_snapshot` plus the
    `restore_strategy` and verification expectations.
  - Decision inputs merged: (1) catalog `transition_policy`,
    (2) step `transition` declaration, (3) known SOP transition
    metadata, (4) selector semantics (submit/close/delete),
    (5) current application/session state.
  - Checkpoint record schema per architecture §15.3 (`checkpoint_id`,
    `kind`, `snapshot_id`, `restorable`, `restore_strategy`,
    `restore_payload`, `preconditions`, `state_digest`,
    `verification_expectations`).
- Event integration: emit `checkpoint_created`, `transition_detected`,
  `unexpected_transition` events through the P1.3 trace store.
- Unexpected-transition handling: stop the current plan, append
  `unexpected_transition` + `replan_requested` (P2.2 wires the actual
  replan), capture the new observation immediately.

### Out Of Scope

- Recovery branch execution (P2.2).
- Application-state restore strategies beyond `logical` and `session`
  in this checkpoint. `application_state` and `environment_snapshot`
  strategies are declared in policy but only `logical` and `session`
  implementations land here.
- Run-level report (P2.3).
- LLM replanning (P3.1).

### Dependencies

- P1.3 trace core.
- P1.4 evidence (checkpoints must persist their evidence correctly).
- P0.1 catalog (`transition_policy`, `risk` metadata).
- Existing SOPs under `sops/` (read-only metadata source).

### Functional Requirements

| ID | Requirement |
|----|-------------|
| FR-P2.1-01 | A same-page toggle click produces `transition: none`; no `checkpoint_created` event is emitted. |
| FR-P2.1-02 | A click on a navigation control produces `page_navigation`; a `logical` checkpoint is created before the action. |
| FR-P2.1-03 | A modal-triggering action produces `modal_open`; a `logical` checkpoint is created before the action. |
| FR-P2.1-04 | A click whose after-snapshot shows an unexpected modal produces `unexpected_transition`; the executor halts without inverse action. |
| FR-P2.1-05 | `decision` is deterministic for the same `(step, snapshot, catalog_version, sop_index_version)` inputs. |
| FR-P2.1-06 | Checkpoint metadata includes which input sources contributed to the decision (catalog, sop, step, selector, state). |
| FR-P2.1-07 | `transition_policy: required_checkpoint` in the catalog forces a checkpoint even when no step transition was declared. |
| FR-P2.1-08 | An `application_state` checkpoint in V1 is declared `restorable=false` if no tested restore strategy exists; the policy gate refuses to use it. |
| FR-P2.1-09 | `transition_policy: never` (e.g. observations) never produces a checkpoint, even if the step declares one. |

### Interface And Data Requirements

```python
# agent/execution/transitions.py
class TransitionKind(str, Enum):
    NONE = "none"
    CONTROL_STATE_CHANGE = "control_state_change"
    PAGE_NAVIGATION = "page_navigation"
    MODAL_OPEN = "modal_open"
    MODAL_CLOSE = "modal_close"
    WINDOW_OPEN = "window_open"
    WINDOW_CLOSE = "window_close"
    WINDOW_OWNER_CHANGE = "window_owner_change"
    APPLICATION_RESTART = "application_restart"
    UNKNOWN_MATERIAL_CHANGE = "unknown_material_change"

def classify_transition(before: ObservationSnapshot,
                        after: ObservationSnapshot) -> TransitionResult: ...

@dataclass(frozen=True)
class TransitionResult:
    kind: TransitionKind
    signals: dict[str, object]  # debug aid
    confidence: str             # "high" | "medium" | "low"

# agent/execution/checkpoints.py
class CheckpointKind(str, Enum):
    NONE = "none"
    LOGICAL = "logical"
    SESSION = "session"
    APPLICATION_STATE = "application_state"
    ENVIRONMENT_SNAPSHOT = "environment_snapshot"

@dataclass(frozen=True)
class CheckpointDecision:
    kind: CheckpointKind
    restorable: bool
    restore_strategy: str | None
    rationale: tuple[str, ...]  # which inputs triggered it

def decide_checkpoint(step: AtomicTestStep,
                      snapshot: ObservationSnapshot,
                      catalog: CatalogView,
                      sop_index: SopIndex) -> CheckpointDecision: ...
```

### Acceptance Criteria

1. Same-page toggle fixture yields zero `checkpoint_created` events.
2. Navigation fixture yields exactly one `checkpoint_created` event
   with `kind: logical` immediately before the action.
3. Forced-modal mid-flow yields one `unexpected_transition` event and
   the case stops.
4. Decision tests cover the matrix:
   `{transition_policy ∈ {never, possible, expected,
   required_checkpoint}} × {step.transition ∈ {none, expected,
   required_checkpoint}} × {selector semantics ∈ {none, submit,
   close, delete}}`.
5. `transition_policy: required_checkpoint` in catalog overrides
   `step.transition: none`.
6. `restorable=false` `application_state` checkpoints are recorded
   but never selected for restore.

### Required Tests

| Test | Purpose |
|------|---------|
| `test_transition_classify_same_page` | Coordinates differ, tree unchanged → `none`. |
| `test_transition_classify_page_nav` | Tree digest + nav title change → `page_navigation`. |
| `test_transition_classify_modal_open` | Modal role appears → `modal_open`. |
| `test_transition_classify_window_owner_change` | PID changes → `window_owner_change`. |
| `test_transition_classify_application_restart` | Process restart detected → `application_restart`. |
| `test_checkpoint_policy_matrix` | All enum combinations from FR-2.1-04. |
| `test_catalog_required_checkpoint_overrides_step` | Catalog wins. |
| `test_unexpected_transition_halts_executor` | No inverse click attempted. |
| `test_decision_determinism` | Same inputs → same decision. |
| `test_application_state_unrestorable_logged` | Recorded but not selected. |

### Deliverables

- `agent/execution/transitions.py`, `agent/execution/checkpoints.py`.
- Event-type integration in `agent/trace/events.py` (re-export
  schemas).
- Transition fixture bank under `test_case/fixtures/transitions/`.
- Unit tests under `test_case/test_transitions/` and
  `test_case/test_checkpoints/`.

### Review Stop Point

Stop after all P2.1 acceptance criteria pass. Reviewer confirms:

- ordinary clicks never create checkpoints;
- expected transitions do;
- unexpected transitions stop rather than guess;
- decisions are reproducible from inputs.

---

## Checkpoint P2.2 — Recovery Branches

### Scope

Resume from `logical` and `session` checkpoints by creating a new
branch in the trace. Add verified `application_state` restore only for
known/tested SOP strategies. Preserve failed branches. Support
`reobserve_replan` without arbitrary loops. Update `step-results.json`
and `trace.md` to reflect branches.

### In Scope

- `agent/execution/recovery.py`:
  - `resume_from_checkpoint(checkpoint_id, remaining_plan) -> Branch`.
  - Branch lifecycle: `recovery_requested` → `branch_created` →
    `restore_actions_executed` → `observation_after_restore` →
    `recovery_result` → either continue with validated remainder or
    emit `replan_created` and stop.
  - Restore strategies implemented:
    - `logical`: reset planner context, re-observe, validate
      remaining plan against the new observation.
    - `session`: reconnect/activate the locked window, restore the
      window lock, re-observe.
    - `application_state` (only for SOPs explicitly registered with
      a tested inverse strategy): execute the registered inverse
      actions, verify against the SOP's expectations, re-observe.
  - `reobserve_replan` policy: when the original plan is no longer
    valid after restore, emit `replan_created` and stop the current
    case with status `blocked` (a planner may then run again on the
    new observation; arbitrary loops remain out of scope).
- Branch metadata in events: `branch_id`, `forked_from_event_id`,
  `forked_from_checkpoint_id`, plus the source branch head recorded
  in the `branch_created` payload.
- `step-results.json` projection gains a `branches: [...]` view
  preserving each branch's terminal status.
- `trace.md` gains a "Recovery" subsection inside each affected step
  (branch IDs, restore actions, recovery_result event_id, replan
  decision).
- `restorable=false` checkpoints trigger `recovery_result.status =
  blocked` with a structured `code: "restore_not_available"`.

### Out Of Scope

- `environment_snapshot` checkpoints (only declared; not implemented
  unless an infrastructure provider supplies tested snapshots).
- Run-level report (P2.3).
- LLM replan generation (P3.1).

### Dependencies

- P2.1 transition/checkpoint policy.
- P1.3 trace core (branch support).
- P1.4 evidence (recovery screenshots).
- SOP registry under `sops/` for `application_state` strategy.

### Functional Requirements

| ID | Requirement |
|----|-------------|
| FR-P2.2-01 | Recovery never deletes or modifies events on the failed branch. |
| FR-P2.2-02 | Branch ancestry (parent_event_id / forked_from_event_id) verifies on chain validation. |
| FR-P2.2-03 | `recovery_requested` remains on the failed branch. The new branch begins with `branch_created`, whose `forked_from_event_id` references the checkpoint event and whose payload records the failed branch head; restore/observation events use `branch_created` (then each prior new-branch event) as `parent_event_id`. |
| FR-P2.2-04 | After a `session` restore, the lock state is verified before any further mutation. |
| FR-P2.2-05 | After an `application_state` restore, the SOP's expectations are checked before continuing; failure halts and the branch is preserved. |
| FR-P2.2-06 | `restorable=false` checkpoint recovery emits `recovery_result.status: blocked` with `code: "restore_not_available"`. |
| FR-P2.2-07 | `reobserve_replan` succeeds when at least one remaining step becomes valid; otherwise emits `replan_created` and stops. |
| FR-P2.2-08 | Replans do not loop — at most one recovery + one replan per failing step. |
| FR-P2.2-09 | `step-results.json` keeps the first-attempt status visible alongside the recovery-attempt status. |
| FR-P2.2-10 | `trace.md` renders one subsection per branch with its own terminal outcome. |

### Interface And Data Requirements

```python
# agent/execution/recovery.py
class RestoreStrategy(str, Enum):
    NONE = "none"
    LOGICAL_REOBSERVE = "logical_reobserve"
    SESSION_RECONNECT = "session_reconnect"
    APPLICATION_INVERSE = "application_inverse"

@dataclass
class Branch:
    branch_id: str
    forked_from_event_id: str
    forked_from_checkpoint_id: str
    head_event_id: str

def resume_from_checkpoint(checkpoint_id: str,
                           remaining_plan: list[AtomicTestStep],
                           trace: TraceContext) -> Branch: ...

def execute_restore(branch: Branch,
                    strategy: RestoreStrategy,
                    payload: dict,
                    trace: TraceContext) -> RestoreResult: ...

@dataclass
class RestoreResult:
    ok: bool
    code: str | None        # restore_not_available, lock_verify_failed, ...
    snapshot_id: str | None
    expectation_results: list[ExpectationResult]
```

### Acceptance Criteria

1. A failed step on `BR-001` followed by a successful recovery
   produces two branches in the trace; both verify; events on
   `BR-001` are untouched.
2. `application_state` recovery uses only SOPs registered in
   `sops/` with tested inverse actions; unregistered strategies are
   refused with `restore_not_available`.
3. After a recovery, `step-results.json` shows the original
   failure and the recovery outcome side by side.
4. `trace.md` shows two subsections for the affected step — the
   failed attempt and the recovery attempt — both with their
   screenshots.
5. The chain remains valid after recovery (no event rewritten).

### Required Tests

| Test | Purpose |
|------|---------|
| `test_recovery_preserves_failed_branch` | BR-001 events unchanged after BR-002 forked. |
| `test_branch_ancestry_verifies` | Chain verification walks branches correctly. |
| `test_logical_recovery_reobserves` | Fresh snapshot_id on recovery branch. |
| `test_session_recovery_recreates_lock` | Lock state verified before further mutation. |
| `test_application_state_only_registered_sops` | Unregistered SOP refused. |
| `test_restore_not_available_blocks` | `restorable=false` → blocked. |
| `test_replan_single_recovery_per_step` | No loops. |
| `test_step_results_show_both_attempts` | Projection side-by-side. |
| `test_trace_md_two_subsections` | Markdown rendered for both branches. |

### Deliverables

- `agent/execution/recovery.py`, branch metadata in
  `agent/trace/events.py`, projection update in
  `agent/trace/projections.py`, renderer update in
  `agent/trace/markdown.py`.
- SOP inverse-action registry (small) under `sops/inverse/` for
  tested strategies; existing SOPs opt-in by adding a tested inverse
  block.
- Unit + integration tests under `test_case/test_recovery/`.

### Review Stop Point

Stop after all P2.2 acceptance criteria pass. Reviewer confirms:

- failed branches are preserved;
- restoration is honest about its actual level (`logical_only` is not
  misrepresented as UI rollback);
- replanning is bounded.

---

## Checkpoint P2.3 — Run-Level Report And Reruns

### Scope

Aggregate case attempts into a run-level `report.md` and `manifest.json`.
Show pass/fail/blocked/skipped totals and durations. Show both
first-attempt and final-attempt outcomes. Link failures to case traces
and screenshot evidence. Reconcile report totals against structured
results. Make reruns additive.

### In Scope

- `agent/trace/runs.py`:
  - `RunContext` managing `artifacts/test-runs/<run_id>/` with one
    `manifest.json` plus `cases/<safe_case_id>/attempt-<n>-<trace_id>/`.
  - Sanitize case IDs and step IDs for filenames; reject `..`,
    separators, control characters, absolute paths.
- `agent/trace/render_report.py`:
  - `report.md` content per architecture §16.2:
    - run identity, command/source, target set, environment,
      start/end/duration
    - pass/fail/blocked/skipped totals
    - first-attempt stability totals + final-attempt headline totals
      (separately)
    - per-case attempt table linking to each `trace.md`
    - failed/blocked step summaries with thumbnail/full screenshot
      links
    - recovery/replan counts
    - evidence and hash-chain integrity summary
    - cleanup failures and infrastructure warnings
  - Reconciliation: totals must equal the sum of per-case step
    outcomes; a mismatch raises and refuses to emit the report.
- Rerun handling: a rerun creates a new attempt directory; previous
  attempts are retained and linked from the report. Each attempt has
  its own `trace_id`.
- `manifest.json` schema (run-level): `run_id`, `started_at`,
  `ended_at`, `requested_targets`, `environment`, `case_attempts`,
  `renderer_version`, `schema_versions`, `aggregate_status`,
  `metrics_summary`.
- Metrics recorded per architecture §21: action selections,
  validation failures, semantic/coordinate ratio, step/case success,
  expectation failure categories, stale/ambiguous counts,
  transition/unexpected-transition counts, retries, replans,
  recoveries, screenshot counts and bytes, durations. **Metrics must
  not contain raw screen text or secret arguments.**

### Out Of Scope

- Replans generated by the LLM (P3.1).
- Production hardening (P2.4) — limits, adversarial handling, target
  restart policy.

### Dependencies

- All P1 checkpoints.
- P2.1 transition/checkpoint.
- P2.2 recovery branches.

### Functional Requirements

| ID | Requirement |
|----|-------------|
| FR-P2.3-01 | Reruns never overwrite previous attempts; each attempt is a separate directory under its `case_id`. |
| FR-P2.3-02 | Report reconciliation raises if any per-case totals do not sum to the headline totals. |
| FR-P2.3-03 | First-attempt and final-attempt outcomes are both visible; the headline defaults to the final attempt. |
| FR-P2.3-04 | Every case attempt link in the report resolves within the artifact tree; missing links cause the report to render a warning. |
| FR-P2.3-05 | `report.md` is deterministic given frozen `recorded_at` / `generated_at` timestamps. |
| FR-P2.3-06 | Sanitization rejects filenames containing `..`, separators, or absolute paths; offending inputs raise before any write. |
| FR-P2.3-07 | Cleanup failures change a passed case to `failed` only when the test definition marks cleanup as outcome-critical. |
| FR-P2.3-08 | Metrics are persisted without screen text or secret arguments. |
| FR-P2.3-09 | Report rendering tolerates missing/corrupt evidence — it surfaces warnings, never fails silently. |

### Interface And Data Requirements

```python
# agent/trace/runs.py
class RunContext:
    def __init__(self, root: Path, run_id: str): ...
    def add_case_attempt(self, case: TestCase,
                         attempt_no: int,
                         trace_id: str) -> CaseAttemptRef: ...
    def finalize(self) -> Manifest: ...

# agent/trace/render_report.py
def render_report(run: RunContext, *,
                  attempts: list[CaseAttemptRef],
                  frozen_generated_at: str | None = None) -> str: ...

def reconcile_totals(attempts: list[CaseAttemptRef],
                     headline_totals: Totals) -> None: ...  # raises on mismatch
```

### Acceptance Criteria

1. A 3-case run (one pass, one fail, one blocked) yields a
   `report.md` with the correct counts and links to each
   `trace.md`.
2. Rerunning the same run creates a second attempt directory per
   case; the first attempt is intact and linked.
3. Headline totals equal final-attempt totals; first-attempt totals
   are visible separately.
4. Forcing a totals mismatch raises and refuses to emit the report.
5. A case whose `trace.md` is missing from disk renders a visible
   warning in the report rather than a broken link.
6. Cleanup-outcome-critical flag flips a passed case to `failed`
   when cleanup fails.

### Required Tests

| Test | Purpose |
|------|---------|
| `test_run_three_cases_totals` | Headline totals correct. |
| `test_rerun_additive` | New attempt dir; old intact. |
| `test_first_vs_final_attempt_split` | Headline uses final; both shown. |
| `test_reconciliation_raises_on_mismatch` | Tampered attempt → raise. |
| `test_missing_trace_md_warning` | Warning block in report. |
| `test_cleanup_outcome_critical` | Flag flips status. |
| `test_deterministic_rendering` | Frozen timestamps → byte-identical report. |
| `test_metrics_no_secret_text` | Fuzz attempt: a password in args never appears. |
| `test_filename_sanitization` | `..` rejected. |

### Deliverables

- `agent/trace/runs.py`, `agent/trace/render_report.py`,
  `agent/trace/render_metrics.py`.
- CLI entry point `scripts/run_suite.py` (or extension of
  `test_case/run_tests.py`) that produces a run directory.
- Golden report fixture under `test_case/fixtures/run_report/`.

### Review Stop Point

Stop after all P2.3 acceptance criteria pass. Reviewer confirms:

- totals reconcile;
- reruns are additive;
- missing/corrupt evidence surfaces, not silently lost;
- deterministic output is reproducible.

---

## Checkpoint P2.4 — Production Hardening

### Scope

Enforce artifact, image, payload, event-count, and timeout limits.
Complete argument/text/image redaction policy. Add target restart and
unknown-action-outcome handling. Record metrics without sensitive
values. Run Windows + macOS live E2E flows.

### In Scope

- Limits (configuration; defaults listed):
  - max image bytes (`8 MiB`); above this, return
    `evidence_too_large` but keep the original under a configurable
    retention policy and emit a downsampled preview if policy
    permits.
  - max event payload bytes (`256 KiB`).
  - max events per trace (`50000`).
  - max trace duration (`24 h`); `trace_duration_exceeded` is a
    terminal status.
  - max screenshots per case (`100`).
- Redaction completeness:
  - text-pattern rules applied to action arguments and observed
    text;
  - image-rectangle rules applied before persistence;
  - rule registry externalized to a config file; secrets
    registration test verifies no configured secret string appears
    in any persisted artifact.
- Target restart policy:
  - target-side receipt cache is empty after restart; agent must
    treat unknown action outcome as `blocked`, not retry
    irreversible actions.
  - target-side post-restart heartbeat is verified at the start of
    every case.
- Unknown-action-outcome handling: when `execute_action` returns no
  receipt within timeout, executor records `action_undelivered`
  and surfaces `blocked`; never silently retries.
- Adversarial inputs: malformed `args`, oversize strings, unicode
  edge cases; all rejected with structured errors before reaching
  the backend.
- Metrics hardening: confirmed metrics writers do not include
  `args`, observed text, or selector hints.
- Live E2E (recorded as samples, not enforced in CI):
  - Windows HiSec entry → EDRClient navigation with screenshots
  - macOS HiSec equivalent flow
  - wrong process/window ownership rejection
  - modal transition with recovery (when SOP supports it)
  - target restart producing honest unknown/blocked outcome

### Out Of Scope

- New capability (P3).
- Arbitrary shell execution (architecture §3).
- Web trace UI.

### Dependencies

- All P1 + P2.1–P2.3 checkpoints.

### Functional Requirements

| ID | Requirement |
|----|-------------|
| FR-P2.4-01 | Image exceeding the configured limit is recorded with `code: "evidence_too_large"` and the policy decides whether to retain the original or only a downsampled preview; evidence digest always refers to the retained bytes. |
| FR-P2.4-02 | Event payload exceeding the limit raises at append time and refuses the event. |
| FR-P2.4-03 | Trace exceeding the max event count terminates with `trace_event_limit_exceeded`. |
| FR-P2.4-04 | Trace exceeding the max duration terminates with `trace_duration_exceeded`. |
| FR-P2.4-05 | A configured secret string never appears in events, projections, images, or reports — verified by a negative test that scans persisted artifacts. |
| FR-P2.4-06 | A target restart changes `server_instance_id`. Previously acknowledged receipts remain authoritative in the trace; an unacknowledged in-flight mutation is marked `action_outcome_unknown` and blocked without replay, while genuinely new requests execute on the new instance. |
| FR-P2.4-07 | An unknown action outcome (timeout, no receipt) surfaces `action_undelivered` → `blocked`; no automatic retry. |
| FR-P2.4-08 | Adversarial inputs (oversize strings, control chars, NUL bytes, unicode normalization) are rejected with structured errors before any backend call. |
| FR-P2.4-09 | Metrics serialized to disk contain no screen text, no secret arguments, no selector hints. |
| FR-P2.4-10 | Live E2E traces from at least one Windows target and one macOS target render their `trace.md` and `report.md` without warnings on a clean run. |

### Interface And Data Requirements

```python
# agent/limits.py
@dataclass(frozen=True)
class Limits:
    max_image_bytes: int = 8 * 1024 * 1024
    max_event_payload_bytes: int = 256 * 1024
    max_events_per_trace: int = 50_000
    max_trace_duration_seconds: int = 24 * 3600
    max_screenshots_per_case: int = 100

# agent/redaction.py
class RedactionRegistry:
    def __init__(self, rules_path: Path): ...
    def apply_text(self, text: str) -> tuple[str, list[str]]: ...
    def apply_image(self, png: bytes) -> tuple[bytes, list[str]]: ...
    def audit_artifacts(self, run_root: Path) -> AuditReport: ...

# target/restart.py (or agent-side handler)
def on_target_restart(trace: TraceContext) -> None: ...
```

### Acceptance Criteria

1. Image over the limit produces `evidence_too_large` and a
   retained/downsampled artifact per policy.
2. Oversize event payload is rejected at append time.
3. Events exceeding the cap or duration trigger the documented
   terminal codes.
4. Secret-string audit finds zero matches across a run directory.
5. After a simulated restart, submitting an old `request_id` is
   refused; no replay happens.
6. Action with no receipt within timeout surfaces `blocked`, not
   automatic retry.
7. Metrics file passes the no-secret / no-screen-text audit.
8. Live E2E runs produce complete traces on at least one Windows
   and one macOS target.

### Required Tests

| Test | Purpose |
|------|---------|
| `test_image_too_large_policy` | `evidence_too_large` and retention behavior. |
| `test_event_payload_oversize_rejected` | Append raises. |
| `test_event_limit_terminates_trace` | Terminal code. |
| `test_duration_limit_terminates_trace` | Terminal code. |
| `test_secret_audit_zero_matches` | Negative persistence test. |
| `test_target_restart_changes_instance_id` | Restart identity distinguishes prior in-flight work from new requests. |
| `test_target_restart_does_not_replay_inflight_action` | Unknown prior-instance outcome is blocked without mutation replay. |
| `test_target_restart_accepts_new_request` | New-instance requests remain executable. |
| `test_action_undelivered_blocks_step` | No automatic retry. |
| `test_adversarial_inputs_rejected` | Oversize / NUL / control chars / unicode. |
| `test_metrics_no_screen_text` | Fuzz attempt against metrics writer. |
| `test_metrics_no_secret_arguments` | Same. |
| `test_live_e2e_render_clean` | Manual sample check; not enforced in CI. |

### Deliverables

- `agent/limits.py`, `agent/redaction.py`, restart handler.
- Configuration schema for limits and redaction rules.
- Live E2E sample runs (artifacts only; not committed to source
  control).
- Security/adversarial tests under `test_case/test_security/`.

### Review Stop Point (Production-Ready Milestone)

Stop after all P2.4 acceptance criteria pass. Reviewer confirms:

- limits fail safely;
- no configured secret leaks anywhere;
- target restart produces honest unknown/blocked outcomes;
- live E2E traces render cleanly on supported targets.

This checkpoint is the production-ready milestone for human-authored
test cases. After approval, P3 may begin.

---

## Status Of Existing Code

| Area | Existing piece | Reusable in P2 |
|------|----------------|-----------------|
| SOPs | `sops/*.md` | Yes — read-only metadata source for checkpoint/inverse policies. P2.2 must add tested inverse blocks where applicable. |
| Profile tests | `test_case/run_*.py`, `test_case/test_e2e/*` | Yes — E2E samples for P2.4 reuse the existing live-target harness, but P2 does not modify their test scripts. |
| Backend ownership | `target/automation/{windows_pywinauto,macos_accessibility}.py` | Yes — ownership checks already enforce `expected_process_name` for HiSec mutations; P2.4 layers restart policy on top. |

No P2 piece is implemented. P2 is entirely new code that consumes the
P0/P1 artifacts and the existing backend/SOP/test infrastructure.

## Open Decisions Deferred From P0 / P1

1. UUIDv7 vs ULID (still open; resolved once for all IDs).
2. Where `target/screenshot_bytes()` lives (resolved by P1.4
   interface; P2 just consumes it).
3. Image transport (base64 vs temp file) for over-threshold images —
   P2.4 must make this decision because the limit policy depends on
   it. Recommendation: small/medium base64 inline; large via
   existing managed transfer layer; never embed target-local
   absolute paths in `trace.md` / `report.md`.
