# P2.2 — Recovery Planner Design

## Status

- State: **proposed implementation design** — Round 1 reviewed
  (APPROVED WITH DESIGN CONDITIONS); awaiting doc patch for
  Round 2 review
- Review owner: EDR-WD maintainers (edr-test)
- Round 1 review: APPROVED WITH 5 CONDITIONS + 3 ADDITIONAL CHANGES
  (see §11 for the decision log)
- Implements requirements: [`../requirements/P2-recovery-production.md`](../requirements/P2-recovery-production.md) §P2.2 (lines 188-340)...
- Predecessor: [`P2.1 — Transition Detection And Checkpoint Policy`](#p2.1-carry-over), closed at commit `6107cf3` on `feature/p2-1-transitions` (pushed, not yet merged to main)
- Successor (this doc feeds): P2.3 (run-level report), P2.4 (production hardening)
- Review stop point: design-review APPROVED → implementation phase

---

## 1. Problem Statement

P2.1 (`agent/execution/checkpoints.py::decide_checkpoint`) answers a
**declarative** question:

> *Should this step be checkpointed, and if so, at what level?*

P2.2 must answer a **procedural** question:

> *Given a checkpoint, how do we recover, what is the retry budget,
> and when do we declare terminal failure?*

The boundary between the two is deliberate. Conflating "decide a
checkpoint" with "execute a recovery" is the most common architectural
mistake in test-recovery systems; it produces checkpoints that
silently mix decision logic with side effects, making them
unverifiable in isolation. This document keeps them strictly
separated.

---

## 2. Goals

1. Resume execution from a `logical` or `session` checkpoint by
   forking a new branch (P2.2 spec FR-01..FR-03).
2. Apply tested restore strategies: `redrive_prior_steps`,
   `reconnect_session`, and the SOP-registered `process_restart`
   inverse (FR-04, FR-05).
3. Honor `restorable=false` by emitting `recovery_result.status =
   blocked` with `code: "restore_not_available"` (FR-06).
4. Support `reobserve_replan` exactly once per failing step
   (FR-07, FR-08 — no loops).
5. Project both first-attempt and recovery-attempt outcomes into
   `step-results.json` and `trace.md` (FR-09, FR-10).
6. Bound the recovery procedure with explicit retry budget and
   terminal-failure codes (NEW — not in spec, derived in §5.3).

## 3. Non-Goals

- `environment_snapshot` checkpoints (only declared; not implemented).
- LLM replan generation (deferred to P3.1).
- Run-level aggregation (P2.3).
- New transition kinds or topology rules (P2.1 surface is frozen).
- Cross-case recovery sharing (each case has its own recovery state).

---

## 4. Architecture

### 4.1 Module Layout

```
agent/execution/
├── checkpoints.py        # P2.1 — DECIDE (frozen at 6107cf3)
├── recovery.py           # P2.2 — EXECUTE  (new)
│   ├── RecoveryBudget
│   ├── RestoreExecutor
│   ├── ReplanPolicy
│   └── resume_from_checkpoint()  # spec FR signature
├── recovery_inverse.py   # P2.2 — SOP inverse registry  (new)
│   └── InverseRegistry / SOPInverseAction
└── ...

agent/trace/
├── events.py             # +3 events: branch_created, recovery_result, replan_created
├── projections.py        # step-results.json: branches: [...] view
└── markdown.py           # trace.md: "Recovery" subsection per branch
```

### 4.2 Recovery Lifecycle (BR-002 example)

```
BR-001 (failed)
  ├── step-3 ok
  ├── step-4 ok
  ├── step-5 click   ──── unexpected transition ────► checkpoint decision
  │      │                                                   │
  │      ▼                                                   ▼
  │   result: error                              decide_checkpoint() →
  │                                                CheckpointDecision(
  │                                                  kind=SESSION,
  │                                                  restorable=True,
  │                                                  restore_strategy=RECONNECT)
  │
  │   recovery_requested (on BR-001, terminal event)
  │
  ▼
BR-002 (new branch, forked from BR-001's recovery_requested event)
  ├── branch_created (forked_from_event_id, source_branch_head)
  ├── restore_actions_executed (strategy=RECONNECT)
  ├── observation_after_restore (snapshot_id)
  ├── recovery_result (status=ok | blocked | replanned)
  └── replan_created (optional; one-shot)
```

### 4.3 P2.1 / P2.2 Interface Boundary

```
                  decide_checkpoint()              resume_from_checkpoint()
P2.1 layer:       (PURE, no side effects)         (reads CheckpointDecision)
                                                            │
                                                            ▼
P2.2 layer:                                                (SIDE EFFECTS:
                                                            new branch,
                                                            trace events,
                                                            target mutation)
```

- P2.1 stays pure. `decide_checkpoint` returns a frozen
  `CheckpointDecision`. No I/O, no trace mutation, no target mutation.
- P2.2 consumes the decision but never modifies it. It can request
  more `decide_checkpoint()` calls during recovery (e.g., for the
  restored state), but each call is independent.
- Recovery errors never reach back into P2.1. If P2.1 throws, P2.2
  treats that as `recovery_result.status = blocked, code =
  "decision_pipeline_failed"` and preserves the failed branch.

**Q5 decision (Round 1)** — `RecoveryPlanner` is a **pure function**
and does NOT call `decide_checkpoint()` from inside its own
runtime. The executor owns checkpoint policy; the planner only
emits a `RecoveryPlan`:

```
Executor
  |
  +-- checkpoint policy        (P2.1)
  |
  +-- recovery planner         (P2.2; pure; input -> output)
  |
  +-- resume                   (Executor orchestrates)
```

`RecoveryPlanner` signature:

```python
def plan_recovery(
    failure: FailureContext,            # what failed, where, why
    checkpoint: CheckpointDecision,     # P2.1 decision
    budget: RecoveryBudget,             # two-layer cap (Q3)
    inverse_registry: InverseRegistry,  # SOP inverse lookup
) -> RecoveryPlan:
    """Pure: input -> output. No I/O. No trace mutation."""
```

The executor decides when to call `decide_checkpoint()` (e.g., after
an `application_state` restore — per Q1).

---

## 5. Design Decisions (Round 1 closed)

### 5.1 Checkpoint → Restore Strategy Mapping

The mapping table below is what the spec leaves implicit. Each row
must be enforced by `RestoreExecutor` and unit-tested.

| CheckpointKind         | restorable | Default strategy         | Notes                                |
|------------------------|------------|--------------------------|--------------------------------------|
| `none`                 | n/a        | `none`                   | No recovery semantics.               |
| `logical`              | True       | `redrive_prior_steps`    | Re-observe; re-validate remaining.   |
| `session`              | True       | `reconnect_session`      | Lock state verified (FR-04).         |
| `application_state`    | **per SOP**| `process_restart`        | Only if SOP inverse registered (FR-05, FR-06). |
| `environment_snapshot` | False      | `none` (declared only)   | Out of scope for P2.2.               |

**Q1 decision (Round 1)** — `decide_checkpoint()` is re-run **only
at state-change boundaries**, NOT on every retry:

```
FAILED
  |
  v
RecoveryPlanner (pure; see §4.3)
  |
  +-- restore
  |
  +-- observe
  |
  +-- validate expectations
  |
  +-- if state changed -> decide_checkpoint()    <-- only here
```

Reasoning: `decide_checkpoint()` is a state-boundary decision, not
a retry-loop primitive. Calling it on every retry would couple the
recovery loop to the decision pipeline, violating §4.3's purity
guarantee. The re-trigger happens exactly once per `application_state`
restore (after the SOP expectation check, per FR-05) and zero times
for `logical` / `session` restores (their restore IS the state
change; the next checkpoint decision belongs to the *next* step).

### 5.2 Multi-Strategy Conflict Resolution

**Q2 decision (Round 1)** — `more-restrictive wins`, expressed as
an IntEnum (not string comparison):

```python
class RecoverySeverity(IntEnum):
    """Recovery aggressiveness ordering — higher value = more invasive.

    Tiebreaker when two sources propose the same priority:
    the more-restrictive (higher severity) strategy wins.
    """
    NONE        = 0
    CONTROL     = 10
    PAGE        = 20
    WINDOW      = 30
    SESSION     = 40
    APPLICATION = 50
```

Rules:

1. Each input (catalog, step, transition, SOP) maps a strategy to a
   `RecoverySeverity` (see §7).
2. The planner computes `severity = max(proposed_severities)`.
3. Tiebreaker on equal severity: the **first registered source**
   wins (catalog > step > transition > SOP hint).

### 5.3 Retry Budget (NEW — not in spec; Round 1 layered)

**Q3 decision (Round 1)** — **two-layer budget**, not per-step OR
per-attempt but BOTH:

```python
@dataclass(frozen=True)
class RecoveryBudget:
    """Two-layer recovery budget (Round 1 design Q3).

    Layer 1 — per-step attempt cap:
        max_attempts bounds retry storm within one recovery.

    Layer 2 — per-step replan cap (FR-08):
        max_replans = 1 enforces "at most one recovery + one
        replan per failing step".

    Layer 3 — global deadline:
        deadline_ms bounds the total time spent on recovery
        for one step (cross-strategy, cross-replan).
    """
    max_attempts: int = 3
    max_replans:  int = 1    # FR-08 mapping
    deadline_ms:  int = 30000  # 30s ceiling per step
```

Why layered:

- `max_attempts` only: a runaway loop can consume the step budget
  in milliseconds and we lose the ability to detect a bad strategy.
- `max_replans` only (per-attempt): FR-08 says "at most one
  replan", but if replan itself retries, no outer bound.
- `deadline_ms`: independent of attempts; bounds both fast-fail
  loops and pathological slow-fail cases.

Terminal codes (unchanged from Round 0 proposal):

| Code                          | When                                          |
|-------------------------------|-----------------------------------------------|
| `restore_not_available`       | `restorable=false` (FR-06)                    |
| `restore_lock_verify_failed`  | SESSION restore, lock not re-established      |
| `restore_sop_unregistered`    | application_state, no inverse in registry     |
| `restore_expectation_failed`  | SOP expectation check failed after inverse    |
| `restore_attempts_exhausted`  | `attempts >= max_attempts`                    |
| `restore_deadline_exceeded`   | `elapsed_ms >= deadline_ms`                   |
| `replan_unable_to_revalidate` | replan ran, no step became valid (FR-07)      |
| `recovery_loop_detected`      | FR-08 — `replans >= max_replans`              |

---

## 6. P2.1 Carry-Over (Round 3 deferred items)

These three items were deferred from P2.1 and explicitly earmarked
for P2.2.

### 6.1 `CheckpointPriority` IntEnum (R3 #1)

Replace the implicit rule list order with explicit priorities.
Implementation:

```python
class CheckpointPriority(IntEnum):
    APPLICATION_RESTART = 100  # catalog:required wins
    STEP_APPLICATION_RESTART = 80
    STEP_CHECKPOINT_BEFORE = 50
    TRANSITION_SIGNAL = 30
    SOP_HINT = 10
    DEFAULT = 0

@dataclass(frozen=True)
class RuleResult:
    priority: CheckpointPriority
    decision: CheckpointDecision
```

`_DECISION_RULES` becomes `_DECISION_RULES: tuple[tuple[
CheckpointPriority, Callable], ...]` — each rule returns
`(priority, decision | None)`, the resolver picks `max(priority)`.

This change is **backward-compatible** at the public API
(`decide_checkpoint()` still returns a `CheckpointDecision`). The
priority tag is exposed via `CheckpointDecision.rationale` so
existing tests do not break.

### 6.2 `Target.native_index` Decision (R3 #2)

**Q4 decision (Round 1)** — `native_index` is **observation-local
metadata**, NOT identity, NOT in fingerprint:

```
Target identity
  |
  +-- stable fields
       (process_name, native_window_id, control_type, automation_id)

Observation
  |
  +-- native_index   <-- positional, runtime-dependent,
                        diagnostic only
```

Forbidden:

```
fingerprint(native_index)        # NEVER
identity_signature(native_index) # NEVER
```

Allowed:

```
diagnostic_metadata(native_index)        # logs
ordering_within_snapshot(native_index)   # for stable display
```

Why: `native_index` is backend-dependent.

- Windows UIA: positional in the accessibility tree.
- macOS AX: positional in the AX hierarchy (but **not** guaranteed
  stable across queries — Apple event traversal order can shift
  if window state changes mid-traversal).
- X11: positional in the X server's child list.

Conclusion: it cannot be part of any cross-snapshot identity
computation. The `-1` fallback in `_topology_signature` is
**correct** and should stay.

Action: update `target/observations/models.py::Target` docstring
with this explicit contract once P2.2 implementation starts
(deferred to code phase; design decision is locked now).

### 6.3 Topology Behavioral Test Migration (R3 #3)

Current test:

```python
def test_regression_topology_signature_includes_process_name():
    from agent.execution.transitions import _topology_signature
    ...
```

Target state (P2.2):

```python
def test_regression_topology_behavioral_via_classify():
    # Construct two snapshots that exercise the same hwnd,
    # different process_name. Assert classify_transition()
    # emits APPLICATION_RESTART.
    ...
```

Keep the private test as a docstring example; the public test
becomes the contract.

---

## 7. Interface Contracts (proposed signatures)

```python
# agent/execution/recovery.py

# --- Public enums (Round 1 additional changes) -----------------

class RestoreStrategy(str, Enum):
    """P2.2 restore strategy enumeration — replaces string constants.

    Each value carries a default ``RecoverySeverity`` (see Q2).
    Severity is co-located on the enum to keep the planner's
    severity lookup a single attribute access.
    """
    NONE              = ("none",                RecoverySeverity.NONE)
    REDRIVE_PRIOR_STEPS = ("redrive_prior_steps", RecoverySeverity.PAGE)
    RECONNECT_SESSION  = ("reconnect_session",   RecoverySeverity.SESSION)
    REOPEN_APPLICATION = ("process_restart",     RecoverySeverity.APPLICATION)
    REOBSERVE_REPLAN   = ("reobserve_replan",    RecoverySeverity.WINDOW)
    BLOCKED            = ("blocked",             RecoverySeverity.NONE)

class RecoveryStatus(str, Enum):
    """Top-level outcome of a recovery attempt."""
    SUCCESS = "success"     # restore + expectation check passed
    FAILED  = "failed"      # restore ran but expectation check failed
    BLOCKED = "blocked"     # restorable=false or budget exhausted
    REPLANNED = "replanned" # FR-08: emit replan_created, stop

class ReplanState(str, Enum):
    """Per-step replan budget tracker (Q3 / FR-08).

    NOT_REQUESTED → AVAILABLE → CONSUMED is the legal sequence.
    AVAILABLE → CONSUMED is one-way; once consumed, the step is
    terminal. Skipping AVAILABLE (going straight to CONSUMED) is
    illegal and surfaces as ``recovery_loop_detected``.
    """
    NOT_REQUESTED = "not_requested"
    AVAILABLE     = "available"
    CONSUMED      = "consumed"

# --- Budget (Q3 two-layer decision) ----------------------------

@dataclass(frozen=True)
class RecoveryBudget:
    """Two-layer recovery budget (Round 1 design Q3).

    Layer 1 — ``max_attempts``: bounds retry storm within one
      recovery attempt sequence.
    Layer 2 — ``max_replans``:  enforces FR-08 ("at most one
      recovery + one replan per failing step").
    Layer 3 — ``deadline_ms``:  bounds total recovery time per
      step (cross-strategy, cross-replan).
    """
    max_attempts: int = 3
    max_replans:  int = 1     # FR-08 mapping
    deadline_ms:  int = 30000  # 30s ceiling per step

# --- Error codes (unchanged but enumerated) --------------------

class RecoveryErrorCode(str, Enum):
    RESTORE_NOT_AVAILABLE       = "restore_not_available"
    RESTORE_LOCK_VERIFY_FAILED  = "restore_lock_verify_failed"
    RESTORE_SOP_UNREGISTERED    = "restore_sop_unregistered"
    RESTORE_EXPECTATION_FAILED  = "restore_expectation_failed"
    RESTORE_ATTEMPTS_EXHAUSTED  = "restore_attempts_exhausted"
    RESTORE_DEADLINE_EXCEEDED   = "restore_deadline_exceeded"
    REPLAN_UNABLE_TO_REVALIDATE = "replan_unable_to_revalidate"
    RECOVERY_LOOP_DETECTED      = "recovery_loop_detected"

# --- Result contracts (Round 1 additional change) --------------

@dataclass(frozen=True)
class RecoveryResult:
    """Final outcome of a recovery cycle (per step, per branch).

    Always carries: status, strategy used, attempt count,
    branch_id for trace correlation, and a structured error_code
    when status != SUCCESS.
    """
    status:     RecoveryStatus
    strategy:   RestoreStrategy
    attempts:   int
    branch_id:  str | None
    error_code: str | None = None  # RecoveryErrorCode value or None

@dataclass(frozen=True)
class RestoreResult:
    """Lower-level outcome of a single restore execution."""
    ok: bool
    code: RecoveryErrorCode | None
    snapshot_id: str | None
    expectation_results: tuple[ExpectationResult, ...] = ()

# --- Branch + entry points (unchanged) -------------------------

@dataclass(frozen=True)
class Branch:
    branch_id: str
    forked_from_event_id: str
    forked_from_checkpoint_id: str
    head_event_id: str

def resume_from_checkpoint(
    checkpoint_id: str,
    decision: CheckpointDecision,           # from P2.1
    remaining_plan: list[AtomicTestStep],
    trace: TraceContext,
    *,
    budget: RecoveryBudget = RecoveryBudget(),
) -> Branch: ...

def execute_restore(
    branch: Branch,
    strategy: RestoreStrategy,              # enum, not str (Round 1)
    payload: dict,
    trace: TraceContext,
) -> RestoreResult: ...

# --- Q5: pure planner (no decide_checkpoint inside) -----------

def plan_recovery(
    failure: FailureContext,
    checkpoint: CheckpointDecision,
    budget: RecoveryBudget,
    inverse_registry: InverseRegistry,
) -> RecoveryPlan:
    """Pure: input -> output. No I/O. No trace mutation. No
    decide_checkpoint() call. Executor owns the orchestration."""
```

```python
# agent/execution/recovery_inverse.py

@dataclass(frozen=True)
class SOPInverseAction:
    sop_id: str
    expected_after_sop_id: str | None       # transition.sop_id to validate against
    inverse_action_ids: tuple[str, ...]
    timeout_s: float = 30.0

class InverseRegistry:
    def register(self, inverse: SOPInverseAction) -> None: ...
    def get(self, sop_id: str) -> SOPInverseAction | None: ...
    def has(self, sop_id: str) -> bool: ...
```

---

## 8. Test Strategy

In addition to the 9 tests required by the spec (lines 307-320),
P2.2 must add:

| Test | Purpose |
|------|---------|
| `test_recovery_budget_exhausts_terminal_code` | `attempts >= max_restore_attempts` → `restore_attempts_exhausted`. |
| `test_recovery_backoff_used_for_transient_errors` | Backoff tuple applied in order; assertion via recorded timestamps. |
| `test_recovery_more_restrictive_wins_tie` | Two strategies at same priority → process_restart > reconnect > redrive. |
| `test_recovery_application_state_requires_inverse_registered` | Inverse not in registry → `restore_sop_unregistered`. |
| `test_recovery_p21_decision_unchanged` | P2.1 unit tests still pass after IntEnum refactor (no public API break). |
| `test_recovery_topology_behavioral` | Replaces `_topology_signature` direct test for R3 #3. |

Total expected: **15 tests** for P2.2 (9 spec + 6 P2.2-specific).

---

## 9. Review Gate — Decision Log

All five Round 1 questions are **closed**:

| Q  | Question                           | Decision                                                       | Reference |
|----|------------------------------------|----------------------------------------------------------------|-----------|
| Q1 | `decide_checkpoint` re-trigger     | Yes, only at state-change boundary (post `application_state`)| §5.1       |
| Q2 | Multi-strategy conflict            | `more-restrictive wins` via `RecoverySeverity` IntEnum         | §5.2       |
| Q3 | Retry budget                       | Two-layer: `max_attempts` + `max_replans` + `deadline_ms`      | §5.3       |
| Q4 | `Target.native_index`              | Observation-local metadata (NOT identity, NOT in fingerprint)  | §6.2       |
| Q5 | Recovery calls `decide_checkpoint`? | No — `RecoveryPlanner` is pure; executor owns orchestration    | §4.3       |

**Status**: design review **APPROVED** (subject to doc patch landing).
Implementation begins in `feature/p2-2-recovery-planner` branch
once this doc is committed.

---

## 10. Out Of Scope (Reminder)

- `environment_snapshot` restore (P3+ — needs infra provider).
- LLM replan generation (P3.1 — depends on plan-validity LLM).
- Run-level report and reruns (P2.3).
- Cross-case recovery sharing.

---

## 11. Decision Log (formal)

| Round | Decision                                                          | Doc ref |
|-------|-------------------------------------------------------------------|---------|
| R0    | Document created in `proposed` state                              | (initial)|
| R0    | P2.1 / P2.2 boundary: `decide_checkpoint` pure, planner consumes  | §4.3    |
| R0    | Three carry-over items deferred from P2.1 review                  | §6      |
| R1    | Q1: `decide_checkpoint` re-trigger only at state-change boundary  | §5.1    |
| R1    | Q2: `more-restrictive wins` via `RecoverySeverity` IntEnum        | §5.2    |
| R1    | Q3: Two-layer budget (max_attempts + max_replans + deadline_ms)   | §5.3    |
| R1    | Q4: `native_index` = observation-local metadata only              | §6.2    |
| R1    | Q5: `RecoveryPlanner` is pure; executor owns orchestration        | §4.3    |
| R1    | Additional: `RestoreStrategy` enum replaces string constants      | §7      |
| R1    | Additional: `RecoveryStatus` enum (success/failed/blocked/replanned) | §7    |
| R1    | Additional: `ReplanState` enum (NOT_REQUESTED → AVAILABLE → CONSUMED) | §7   |
| R1    | Additional: `RecoveryResult` dataclass (status/strategy/attempts/branch_id) | §7 |