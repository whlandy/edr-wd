# P2.2 Regression Baseline — Failure Classification

## Purpose

Verify that P2.2 implementation did not introduce any new test
failures. Compare current full-suite results against the
P2.1-closed baseline.

## Captured

- Date: 2026-08-03
- Branch: `feature/p2-2-recovery-planner` @ 97fa46f + Round 3 patch
  (helpers + C1 wording)
- Command: `pytest test_case/ --ignore=test_case/test_e2e -q`
- Environment: macOS Darwin 25.3.0, Python 3.14.5,
  `PYTHONPATH=target:.`

## Counts

| Status              | P2.1 baseline | P2.2 round 3 | Δ |
|---------------------|---------------|--------------|---|
| passed              | 558           | 600          | +42 (P2.2 new tests) |
| failed              | 24            | 24           | 0 |
| error               | 10            | 10           | 0 |
| **combined passed** | **558**       | **600**      | **+42** |

Δpassed = +42 = the 42 P2.2 tests (16 contracts + 18 planner + 8 composition
helpers). Per edr-test review standard (Δpassed after skip ≈ 0 expected),
this is a clean growth: every P2.2 test is new and passes.

## Failure Classification

### FAIL group (24 tests)

| Test file | Count | Category | Pre-existing? | Touched by P2.1? | Touched by P2.2? |
|-----------|-------|----------|---------------|-------------------|-------------------|
| `test_action_catalog/test_catalog.py` | 5 | FastMCP server / catalog metadata | yes (baseline) | no | no |
| `test_dispatcher/test_legacy_compat.py` | 8 | FastMCP dispatcher byte-compat | yes (baseline) | no | no |
| `test_dispatcher/test_server_execute_action.py` | 11 | FastMCP execute_action tool surface | yes (baseline) | no | no |
| **FAIL subtotal** | **24** | | | | |

### ERROR group (10 tests)

| Test file | Count | Category | Pre-existing? | Touched by P2.1? | Touched by P2.2? |
|-----------|-------|----------|---------------|-------------------|-------------------|
| `test_integration/test_edr_window_pair_e2e.py` | 1 | Windows backend HiSecAgent+EDRClient visibility | yes (baseline, requires Windows host) | no | no |
| `test_integration/test_is_window_open.py` | 3 | Windows backend integration | yes (baseline, requires Windows host) | no | no |
| `test_integration/test_list_windows.py` | 3 | Windows backend integration | yes (baseline, requires Windows host) | no | no |
| `test_integration/test_wait_window.py` | 3 | Windows backend integration | yes (baseline, requires Windows host) | no | no |
| **ERROR subtotal** | **10** | | | | |

### Summary

| Category | Count | Pre-existing | Introduced |
|----------|-------|--------------|------------|
| FastMCP server (catalog / dispatcher / execute_action) | 24 | yes | no |
| Windows backend e2e (hisec / list / wait / open) | 10 | yes | no |
| P2.1 surface (transitions / checkpoints / regression) | 0 | n/a | no |
| P2.2 surface (recovery / contracts / planner) | 0 | n/a | no |

**Verdict: zero new failures introduced.** All 34 failures/errors are
pre-existing in the P2.1 baseline and are unrelated to the recovery
planner surface (they are FastMCP server and Windows-only backend
tests, none of which the P2.1 or P2.2 changes touch).

## Verification commands

Reproduce the baseline:

```bash
cd /Users/edr-test/edr-wd
PYTHONPATH=target:. pytest test_case/ --ignore=test_case/test_e2e -q
```

Targeted P2.2 run (must be clean):

```bash
PYTHONPATH=target:. pytest test_case/test_recovery/ \
                            test_case/test_checkpoints/ \
                            test_case/test_transitions/ \
                            test_case/test_regression/ -q
# Expected: 96 passed (54 P2.1 + 42 P2.2)
```

Targeted P2.2-only run (commit-only regression):

```bash
PYTHONPATH=target:. pytest test_case/test_recovery/ -q
# Expected: 42 passed
```

## Notes for Round 2 review

- The FastMCP failures are dispatcher + catalog server tools that
  pre-date P2.1. They block production execution paths but do not
  touch the pure planner surface. They are out of scope for P2.2.
- The Windows backend errors require a Windows host (the macOS
  test runner cannot execute them). They are out of scope for P2.2
  and should be exercised in a CI matrix that includes Windows
  runners (P2.4 or P3.x work).
- Δpassed = +42 matches the new test count exactly (no skip-and-pass
  inflation). All P2.2 tests are real assertions, not skips.