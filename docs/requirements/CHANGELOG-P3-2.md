# P3.2 — Design Review Gate (Pre-Implementation)

## Status

- **Round 1**: ⚠️ CHANGES REQUESTED (3 blockers, 3 minors).
- **Round 2**: ✅ APPROVED (gate closed 2026-08-04).
- **Verdict (current)**: ✅ APPROVED.
- **Code written**: 0
- **Tests added**: 0
- **Branch**: `feature/p3-2-design`

## Round 1 → Round 2 Changelog

| Item | Round 1 | Round 2 |
|------|---------|---------|
| D17 mutation policy | absent | added: semantic change requires id or version bump; fixture_id reuse forbidden across semantic shift |
| D19 owner lifecycle | owner = person | owner = accountable component; deprecation requires successor; id reuse forbidden |
| D20 ungated status | missing threshold = fail | ungated marker is explicit; CI policy decides permissibility; missing threshold (no declaration AND no ungated) still fails |
| D21 expected/observed | free-form strings | structured expectation identifier + sanitised structured outcome summary |
| D22 determinism | byte-for-byte | determinism classes D0/D1/D2; model artifact fingerprint |
| D23 taxonomy | absent | new D23: Coverage / Quality / Efficiency / Behaviour; cross-cutting rule (≥3 categories) |
| Cross-cutting metrics | 4 metrics | 5 metrics (added `evaluation_completeness_rate`) |
| FR mapping | FR-05..10 all → D19 | FR-05..10 → D19 + D23, with taxonomy category |
| Acceptance criteria | 8 items | 11 items (added 3 round-2-specific) |

## Decision Items Summary

| ID | Contract | Status | Round |
|----|----------|--------|-------|
| D17 | Dataset identity `(dataset_id, dataset_version, fixture_id)`; content-stable; embedded in report; old versions remain runnable; **mutation policy (round 2)** | contract locked | 1 (with round 2 patch) |
| D18 | Evaluation result schema: parseable JSON; required fields (dataset_*, planner_version, profile, started_at, ended_at, metrics, threshold_decisions, reproducibility_digest, schema_version); no sensitive text; `report_schema.v1` initial | contract locked | 1 (unchanged round 2) |
| D19 | Metric ownership: single owner (component, not person — **round 2**); declaration includes metric_id, owner, definition, unit, direction, category; cross-cutting metrics defined at gate; per-subsystem MAY be added at impl review; **owner lifecycle rule (round 2)** | contract locked | 1 (with round 2 patch) |
| D20 | Threshold policy: declaration (metric_id, direction, pass_if, threshold, rationale); **ungated marker separates from threshold (round 2)**; CI fails on thresholded metric violation only; no warnings; hardcoded cross-cutting thresholds PROHIBITED; missing threshold AND missing ungated = CI fail; no live GUI in unit CI | contract locked | 1 (with round 2 patch) |
| D21 | CI failure semantics: machine-readable output; each failure carries metric_id, dataset_*, fixture_id, **expected (structured expectation identifier) + observed (sanitised structured outcome summary)** — **round 2 sanitised**; non-zero exit on thresholded metric violation; no `args` / observed text / selectors / tokens / LLM prompts in output; deterministic | contract locked | 1 (with round 2 patch) |
| D22 | **Determinism classes (round 2 rewrite)**: D0 evaluator-only byte-identical, D1 planner+model artifact reproducible, D2 byte-identical only for non-LLM stages; reproducibility digest includes hash(fixtures, prompt, planner artifact, **model artifact fingerprint**, catalog, thresholds); excludes timestamps / run ids / env paths; embedded in report; CI warns on digest mismatch but still emits pass/fail | contract locked | 1 (rewritten round 2) |
| D23 | **Evaluation metric taxonomy (added round 2)**: four categories Coverage / Quality / Efficiency / Behaviour; every metric declares category; new metrics MUST join existing category; cross-cutting MUST span ≥3 categories | contract locked | added round 2 |
| D15 (inherited) | Per-transport prompt sanitisation | reused by D22 (digest of prompt template) | 1 |
| D16 (inherited) | Plan event redaction; secret audit | reused by D18 + D21 (no sensitive text in reports; sanitised expected/observed) | 1 |

## Required Cross-Cutting Metrics (locked at gate)

| metric_id | unit | direction | category | definition |
|-----------|------|-----------|----------|------------|
| `parse_success_rate` | ratio | higher_is_better | Quality | Fraction of `ActionSequence` parse attempts that produce a parseable plan. |
| `validation_rejection_accuracy` | ratio | higher_is_better | Quality | Fraction of validation rejections that match an expected rejection (validator catches what it should). |
| `confirmation_correctness` | ratio | higher_is_better | Quality | Fraction of high-risk / irreversible actions that correctly trigger the D14 confirmation gate. |
| `execution_divergence_rate` | ratio | lower_is_better | Behaviour | Fraction of evaluation steps where the observed execution diverges from the expected outcome. |
| `evaluation_completeness_rate` | ratio | higher_is_better | Coverage | Fraction of cases in the dataset that produced a usable evaluation result (not aborted / timed out / errored before threshold check). |

**Taxonomy coverage (D23 rule)**: Quality × 3 + Behaviour × 1 + Coverage × 1 = **3 of 4 categories** ✓.

(Full formula details are implementation freedom; the gate locks
only the identifier, unit, direction, category, and plain-English
definition.)

## Functional Requirements Mapping

| FR | D item(s) that satisfy it | taxonomy category |
|----|---------------------------|-------------------|
| FR-P3.2-01 (reproducible evaluation) | D22 | (cross-cutting) |
| FR-P3.2-02 (no sensitive text in metrics) | D18, D19, D21 | (cross-cutting) |
| FR-P3.2-03 (action-selection dataset) | D17 | Coverage |
| FR-P3.2-04 (sequence-success evaluation) | D17 | Coverage |
| FR-P3.2-05 (semantic vs coordinate ratio) | D19, D23 | Behaviour |
| FR-P3.2-06 (replans-per-task) | D19, D23 | Behaviour |
| FR-P3.2-07 (stale target counts) | D19, D23 | Behaviour |
| FR-P3.2-08 (safety rejection) | D19, D23 | Quality |
| FR-P3.2-09 (recovery success) | D19, D23 | Quality |
| FR-P3.2-10 (screenshot cost) | D19, D23 | Efficiency |

## Deliverables

- `docs/requirements/P3-2-evaluation-design-gate.md` — design
  review package. 7 new decision items (D17..D23) in 5-part
  contract form. 2 inherited from P3.1 (D15 / D16) carried
  forward.
- `docs/requirements/DECISIONS.md` — append-only log updated
  with P3.2 section.
- `docs/requirements/P3-llm-evaluation.md` — Status block
  updated to reference P3.2 design gate.

## Decision Items — Detail (round 2)

### D17 — Dataset identity + mutation policy

Each fixture carries `(dataset_id, dataset_version, fixture_id)`
explicit identity. Identity is content-stable (renaming files
does not change identity) and embedded in the report (not
inferred). Old dataset versions remain runnable for at least one
major P3.2 release after supersession. Dataset version bumps on
contract change, not content change. **Mutation policy (round 2)**:
semantic change requires `fixture_id` change OR `dataset_version`
bump; `fixture_id` MUST NOT be reused with different semantics
across dataset versions; fixture change log MUST record semantic
shifts.

### D18 — Evaluation result schema

Every report is parseable from JSON. Required top-level fields:
`dataset_id`, `dataset_version`, `planner_version`,
`execution_profile`, `started_at`, `ended_at`, `metrics`,
`threshold_decisions`, `reproducibility_digest`,
`schema_version` (initial `report_schema.v1`). Within a
`(dataset_version, planner_version)` pair, schema evolves
additively only. No observed screen text / args / selectors /
confirmation tokens / LLM prompts in reports.

### D19 — Metric ownership (component, lifecycle)

Each metric has a single owner responsible for definition,
formula, unit, expected range. **Owner is an accountable
component (round 2)**, not an individual person. Owner
deprecation requires successor in same release. Metric id MUST
NOT be reused for different formula. Declaration carries
`metric_id`, `owner`, `definition`, `unit`, `direction`,
`category`. Cross-cutting metrics defined at the gate (locked
list above); per-subsystem metrics MAY be added at
implementation review.

### D20 — Threshold policy + ungated separation

Each metric MUST have a threshold declaration OR an explicit
**"ungated" marker (round 2)**. Thresholded metrics declare
`metric_id`, `direction`, `pass_if`, `threshold`, `rationale`.
CI fails on violation of thresholded metrics only (no warning
levels). Ungated metrics contribute NO pass / fail decision;
the CI policy (not the gate) decides whether ungated metrics
are allowed. Threshold values are implementation freedom (locked
by impl review, not gate). Hardcoded cross-cutting thresholds
PROHIBITED — must live in config. Missing threshold AND missing
ungated marker = CI fail. No live GUI availability in unit CI
(live remains opt-in).

### D21 — CI failure semantics (sanitised expected/observed)

CI gate produces machine-readable output (JSON or equivalent)
in addition to human summary. Each failure carries `metric_id`,
`(dataset_id, dataset_version, fixture_id)`, **`expected`
(structured expectation identifier — round 2)**, **`observed`
(sanitised structured outcome summary — round 2)**,
`direction`. Non-zero exit on any thresholded metric violation.
No `args` / observed text / selectors / confirmation tokens /
LLM prompts in failure output. Deterministic (modulo run
identifier).

### D22 — Reproducibility boundary (determinism classes)

Each evaluation stage declares its determinism class explicitly
(round 2 rewrite):
- **D0** evaluator-only: byte-identical reproducible.
- **D1** planner + model artifact: reproducible given fixed
  `(planner_artifact, model_artifact)` pair.
- **D2** byte-identical reproduction required only for non-LLM
  evaluation stages.

Stages without declaration assumed D1. Reproducibility digest
includes content-hash of fixtures, prompt template, planner
artifact (when D1), **model artifact fingerprint (when D1)**,
action catalog, threshold declarations. Digest excludes
timestamps, run ids, env paths, host/user info. Digest embedded
in report (computed before write). CI gate warns on digest
mismatch but still emits pass/fail.

### D23 — Evaluation metric taxonomy (added round 2)

Four-category taxonomy: Coverage / Quality / Efficiency /
Behaviour. Every metric declares category. New metrics MUST
join existing category (introducing new category requires fresh
reviewer round). **Cross-cutting metrics MUST span at least
three of the four categories** (prevents all-quality / no-
coverage failure modes).

## Acceptance Criteria (reviewer-side, round 2)

1. Each D item has the 5-part structure (Problem / Contract /
   Non-Goals / Implementation freedom / Migration impact).
2. Contract vs implementation is separated.
3. D17..D22 cover the P3.2 spec surfaces that were deferred.
4. **D23 (taxonomy) is added per round 2 M6.**
5. **D22 uses determinism classes (D0/D1/D2), not
   byte-identical reproduction, per round 2 B0.**
6. **D20 allows ungated metric status (CI policy decides
   permissibility), per round 2 B1.**
7. **D21 sanitizes expected/observed (structured identifier
   + sanitized summary), per round 2 B2.**
8. **D17 includes fixture mutation policy (semantic change
   requires id or version bump), per round 2 M4.**
9. **D19 owner is a component (not a person) with a
   lifecycle rule, per round 2 M3.**
10. **Cross-cutting metrics cover at least 3 of the 4
    taxonomy categories, per round 2 M5 / D23.**
11. No D item re-implements a P0..P3.1 contract without a
    migration note.

Gate closes when all 11 criteria hold AND reviewer signs off.

## Out of Scope

- Any new code under `agent/eval/`, `target/`, or test
  directories.
- Metric formula implementations.
- Threshold value selection.
- CI gate CLI implementation.
- Dataset fixture content (only the contract is locked).
- LLM provider SDK choice for the evaluation harness.
- Specific model artifact fingerprint scheme (D22
  implementation freedom).
- Hash algorithm choice (D22 implementation freedom).
- CI policy's stance on ungated metrics (D20 explicitly
  defers this to CI policy).

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
- The exact model artifact fingerprint scheme (D22
  implementation freedom).
- The exact stage → determinism class mapping (D22
  implementation freedom).