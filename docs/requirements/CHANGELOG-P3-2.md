# P3.2 — Design Review Gate (Pre-Implementation)

## Status

- **Round**: 1 — DRAFT (awaiting reviewer sign-off).
- **Verdict**: ⏳ pending.
- **Code written**: 0
- **Tests added**: 0
- **Branch**: `feature/p3-2-design`

## Decision Items Summary

| ID | Contract | Status |
|----|----------|--------|
| D17 | Dataset identity: `(dataset_id, dataset_version, fixture_id)` triple; content-stable; embedded in report; old versions remain runnable | contract locked |
| D18 | Evaluation result schema: parseable JSON; required fields (dataset_*, planner_version, profile, started_at, ended_at, metrics, threshold_decisions, reproducibility_digest, schema_version); no sensitive text; `report_schema.v1` initial | contract locked |
| D19 | Metric ownership: single owner per metric; declaration carries metric_id, owner, definition, unit, direction; cross-cutting metrics defined at gate; per-subsystem MAY be added at impl review | contract locked |
| D20 | Threshold policy: declaration (metric_id, direction, pass_if, threshold, rationale); CI fails on violation only; no warnings; hardcoded cross-cutting thresholds PROHIBITED; missing thresholds = fail; no live GUI in unit CI | contract locked |
| D21 | CI failure semantics: machine-readable output; each failure carries metric_id, dataset_*, fixture_id, expected, observed, direction; non-zero exit; no `args` / observed text / selectors / tokens / LLM prompts | contract locked |
| D22 | Reproducibility boundary: byte-for-byte reproducible given same `(dataset_version, planner_version, execution_profile)` triple + frozen inputs; digest includes fixtures / prompt / catalog / thresholds; excludes timestamps / run ids / env paths | contract locked |
| D15 (inherited) | Per-transport prompt sanitisation | reused by D22 (digest of prompt template) |
| D16 (inherited) | Plan event redaction; secret audit | reused by D18 (no sensitive text in reports) |

## Required Cross-Cutting Metrics (locked at gate)

| metric_id | unit | direction | definition |
|-----------|------|-----------|------------|
| `parse_success_rate` | ratio | higher_is_better | Fraction of `ActionSequence` parse attempts that produce a parseable plan. |
| `validation_rejection_accuracy` | ratio | higher_is_better | Fraction of validation rejections that match an expected rejection (validator catches what it should). |
| `confirmation_correctness` | ratio | higher_is_better | Fraction of high-risk / irreversible actions that correctly trigger the D14 confirmation gate. |
| `execution_divergence_rate` | ratio | lower_is_better | Fraction of evaluation steps where the observed execution diverges from the expected outcome. |

(Full formula details are implementation freedom; the gate locks
only the identifier, unit, direction, and plain-English definition.)

## Functional Requirements Mapping

| FR | D item(s) that satisfy it |
|----|---------------------------|
| FR-P3.2-01 (reproducible evaluation) | D22 |
| FR-P3.2-02 (no sensitive text in metrics) | D18, D19 |
| FR-P3.2-03 (action-selection dataset) | D17 |
| FR-P3.2-04 (sequence-success evaluation) | D17 |
| FR-P3.2-05 (semantic vs coordinate ratio) | D19 |
| FR-P3.2-06 (replans-per-task) | D19 |
| FR-P3.2-07 (stale target counts) | D19 |
| FR-P3.2-08 (safety rejection) | D19 |
| FR-P3.2-09 (recovery success) | D19 |
| FR-P3.2-10 (screenshot cost) | D19 |

## Deliverables

- `docs/requirements/P3-2-evaluation-design-gate.md` — design
  review package. 6 new decision items (D17..D22) in 5-part
  contract form. 2 inherited from P3.1 (D15 / D16) carried
  forward.
- `docs/requirements/DECISIONS.md` — append-only log updated
  with P3.2 section.
- `docs/requirements/P3-llm-evaluation.md` — Status block
  updated to reference P3.2 design gate.

## Decision Items — Detail

### D17 — Dataset identity

Each fixture carries `(dataset_id, dataset_version, fixture_id)`
explicit identity. Identity is content-stable (renaming files
does not change identity) and embedded in the report (not
inferred). Old dataset versions remain runnable for at least one
major P3.2 release after supersession. Dataset version bumps on
contract change, not content change.

### D18 — Evaluation result schema

Every report is parseable from JSON. Required top-level fields:
`dataset_id`, `dataset_version`, `planner_version`,
`execution_profile`, `started_at`, `ended_at`, `metrics`,
`threshold_decisions`, `reproducibility_digest`,
`schema_version` (initial `report_schema.v1`). Within a
`(dataset_version, planner_version)` pair, schema evolves
additively only. No observed screen text / args / selectors /
confirmation tokens / LLM prompts in reports.

### D19 — Metric ownership

Each metric has a single owner responsible for definition,
formula, unit, expected range. Declaration carries `metric_id`,
`owner`, `definition`, `unit`, `direction` (so D20 knows
`higher_is_better` vs `lower_is_better`). Cross-cutting metrics
defined at the gate (locked list above); per-subsystem metrics
MAY be added at implementation review.

### D20 — Threshold policy

Each metric has a threshold declaration with `metric_id`,
`direction`, `pass_if`, `threshold`, `rationale`. CI fails on
violation only (no warning levels). Threshold values are
implementation freedom (locked by impl review, not gate).
Hardcoded cross-cutting thresholds PROHIBITED — must live in
config. Missing thresholds = CI fail. No live GUI availability
in unit CI (live remains opt-in).

### D21 — CI failure semantics

CI gate produces machine-readable output (JSON or equivalent)
in addition to human summary. Each failure carries `metric_id`,
`(dataset_id, dataset_version, fixture_id)`, `expected`,
`observed`, `direction`. Non-zero exit on any threshold
violation. No `args` / observed text / selectors / confirmation
tokens / LLM prompts in failure output. Deterministic (modulo
run identifier).

### D22 — Reproducibility boundary

Evaluation MUST be byte-for-byte reproducible given same
`(dataset_version, planner_version, execution_profile)` triple
and frozen inputs. Reproducibility digest includes content-hash
of: dataset fixtures, planner prompt template, action catalog,
threshold declarations. Digest excludes: timestamps, run
identifiers, env-specific paths, host / user info. Digest
embedded in report (computed before write). CI gate warns on
digest mismatch but still emits pass / fail decision.

## Acceptance Criteria (reviewer-side)

1. Each D item has the 5-part structure (Problem / Contract /
   Non-Goals / Implementation freedom / Migration impact).
2. Contract vs implementation is separated.
3. D17..D22 cover the P3.2 spec surfaces that were deferred.
4. No D item re-implements a P0..P3.1 contract without a
   migration note.
5. Required cross-cutting metrics are present in the table.
6. Reproducibility boundary (D22) explicitly excludes the LLM
   call itself.
7. CI failure semantics (D21) explicitly forbid secret leakage.
8. Threshold policy (D20) explicitly forbids hardcoded
   thresholds for cross-cutting metrics.

Gate closes when all 8 criteria hold AND reviewer signs off.

## Out of Scope

- Any new code under `agent/eval/`, `target/`, or test
  directories.
- Metric formula implementations.
- Threshold value selection.
- CI gate CLI implementation.
- Dataset fixture content (only the contract is locked).
- LLM provider SDK choice for the evaluation harness.

## Reviewer Stop Point

After sign-off, P3.2 may begin. P3.2 implementation produces
its own review package that records option choices for:

- The exact metric formulas chosen for the cross-cutting
  metrics (D19 implementation freedom).
- The exact threshold values chosen (D20 implementation
  freedom).
- The exact CI gate CLI exit semantics (D21 implementation
  freedom).
- The exact digest hash function (D22 implementation
  freedom).