# P1.2 — Atomic Test Executor And Step Results

## Status

- **Verdict**: implementation complete; review #2 APPROVED WITH CONDITIONS.
- All 3 review #2 conditions addressed (see "Review #2 fix log" below).
- 80 pytest items in `test_case/test_execution_unit/` + `test_executor_integration/`,
  all PASSED.
- Canonical repo: **418 passed, 27 skipped** (3 warnings are pre-existing
  P1.1 inflight-lock thread exceptions, not introduced by P1.2).

## Canonical verification command (P1.2 final)

```
pytest -q test_case/test_execution_unit/        -> 69 passed
pytest -q test_case/test_executor_integration/  -> 11 passed
pytest -q test_case/                            -> 418 passed, 27 skipped
```

## Public surface

```
agent/execution/
    __init__.py
    models.py        — StepResult, CaseRunResult, ExecutorConfig,
                       StepStatus / CaseOutcome / OnErrorPolicy
    state_machine.py — StepState enum + legal transition map
    expectations.py  — 9 evaluators (see "Expectation registry" below)
    step_results.py  — atomic write_atomic + load_step_results
    executor.py      — AtomicExecutor.run_case / run_step
    CHANGELOG.md     — this file
```

## Expectation registry

P1.2 ships exactly **9 evaluators** (architecture §9.3 minus the
P1.4-deferred `visual_evidence_captured`):

1. `action_ok`
2. `window_open`
3. `window_closed`
4. `active_window_owner`
5. `control_exists`
6. `control_absent`
7. `control_text_equals`
8. `control_text_contains`
9. `window_text_contains`

`visual_evidence_captured` is registered in `EVALUATORS_NOT_AVAILABLE`
(P1.2 review #2 minor note #3). The executor returns a blocked step
with `error.code == "expectation_not_available"` when an evaluator
type is in that set (FR-P1.2-07).

## State machine (architecture §11)

```
CREATED → VALIDATING → OBSERVING → PREPARING_STEP →
  CAPTURING_BEFORE → EXECUTING → OBSERVING_AFTER →
    CAPTURING_AFTER → ASSERTING → RECORDING →
      FINALIZING → COMPLETED | ABORTED
```

Skip-capture paths (no `evidence.screenshot` declared) are legal:

  * `PREPARING_STEP → EXECUTING`        (skip CAPTURING_BEFORE)
  * `OBSERVING_AFTER → ASSERTING`       (skip CAPTURING_AFTER)

The state machine rejects any move not in `TRANSITIONS`
(`agent/execution/state_machine.py` is the single source of truth).

## OnError policies

```
abort               — default; first FAILED/BLOCKED required step
                       stops the case, remaining steps are SKIPPED.
retry               — bounded; default retry_max = 2. Retry exhaustion
                       aborts the case.
capture_and_abort   — FR-P1.2-05; case becomes aborted, remaining
                       steps SKIPPED, terminal state = aborted.
reobserve_replan    — P2.2 stub; no behaviour in P1.2.
```

## Step outcome precedence (architecture §9.4, Blockers 2 + 3)

```
ActionReceipt.ok == False              → step FAILED
expectation_results worst status       → step {PASSED|FAILED|BLOCKED}
no expectation_results + receipt ok    → step PASSED
error_payload != None                  → step BLOCKED
```

Empty expectations do NOT mask a backend failure — that was
P1.2 review #1 Blocker 3.

## Case outcome precedence (architecture §9.4, Blocker 2)

```
required FAILED    → case FAILED
required BLOCKED   → case BLOCKED
all required SKIPPED → case SKIPPED
otherwise          → case PASSED
```

Empty required set → PASSED (defensive default).

## Functional requirements coverage

| ID | Status | Implementation |
|----|--------|----------------|
| FR-P1.2-01 | ✅ | `TRANSITIONS[EXECUTING]` is `{OBSERVING_AFTER, ABORTED}` only. |
| FR-P1.2-02 | ✅ | `ExecutorConfig.retry_max` and `step_timeout_seconds`. |
| FR-P1.2-03 | ✅ | Executor maps timeout to FAILED (not ERROR). |
| FR-P1.2-04 | ✅ | `_probe_backend()` blocks every step. |
| FR-P1.2-05 | ✅ | `capture_and_abort` policy. |
| FR-P1.2-06 | ✅ | `step_results.write_atomic` (temp file + os.replace). |
| FR-P1.2-07 | ✅ | `EVALUATORS_NOT_AVAILABLE` → blocked step. |
| FR-P1.2-08 | ✅ | `target_stale` check before EXECUTING. |
| FR-P1.2-09 | ✅ | `transition.expected=True` skips capture, no checkpoint. |

## Review #2 fix log

| Condition | Status | Detail |
|-----------|--------|--------|
| 1. PytestCollectionWarning for `TestCase` | ✅ | `protocol_models.TestCase.__test__ = False`. Review #1 also fixed; verified post-review. |
| 2. Document "8 vs 9 evaluators" mismatch | ✅ | This CHANGELOG pins the count at 9 and lists all 9 by name. Code + tests + expectations.py docstring were already consistent at 9. |
| 3. Simplify FakeBackend retry fixture | ✅ | `FakeBackend.responses` FIFO queue + `_ok_receipt()` / `_failed_receipt()` helpers. Review #1 also fixed; existing retry tests refactored to use the queue. |

## Out of Scope (deferred)

* Trace events (P1.3).
* Screenshots and evidence persistence (P1.4).
* Recovery branches, replans (P2.2).
* Transition-aware checkpoints (P2.1).
* Run-level report (P2.3).