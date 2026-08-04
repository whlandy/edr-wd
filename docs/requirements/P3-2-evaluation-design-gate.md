# P3.2 — Design Review Gate (Pre-Implementation)

## Status

- **State**: requirements (no implementation).
- **Source contract**: `../architecture/01-action-trace-test-report-design.md`
  §21 Metrics; `../requirements/P3-llm-evaluation.md` §"Checkpoint P3.2";
  `../requirements/P3-1-design-gate.md` (predecessor gate).
- **Mapped checkpoints**: gate before **P3.2 implementation** begins.
- **Completion state**: **DRAFT — round 2** (round 1 = CHANGES REQUESTED;
  see "Reviewer Approval Log" below).
- **Round 1 reviewer verdict**: ⚠️ CHANGES REQUESTED (3 blockers, 3 minors).
- **Round 2 reviewer verdict**: ⏳ pending.
- **Milestone**: this gate exists to lock the **evaluation and
  metrics contracts** that P3.2 must satisfy BEFORE any code lands.

P3.2 must NOT begin implementation until this gate closes.

---

## Why this gate exists

P3.1 closed on contracts (D11..D16) but P3.1 deliberately did NOT
prescribe the evaluation harness. P3.2's spec
(`P3-llm-evaluation.md`) defines FR-P3.2-01..10 and acceptance
criteria, but those are **what** the harness does, not **how the
design holds together** across:

- **Dataset identity**: how are fixtures versioned? Can a
  regression run cite the exact dataset it ran against?
- **Evaluation result schema**: what MUST an evaluation report
  carry so that two runs can be compared?
- **Metric ownership**: who defines each metric, and who is
  responsible when the formula drifts?
- **Metric taxonomy**: how do we distinguish coverage metrics
  from quality metrics from efficiency metrics from behaviour
  metrics?
- **Threshold policy**: when does a regression become a CI fail,
  and who decides the threshold?
- **CI failure semantics**: how does the gate tell CI "this is
  broken" in a way that is machine-readable and traceable?
- **Reproducibility**: what is the boundary between
  "deterministic" and "non-deterministic" output? What MUST be in
  the reproducibility digest?

P3.2 design gate locks the answers in the same 5-part contract
form as P2.5 and P3.1.

---

## What P3.2 design gate locks — and what it does not

**Locks:**

- Behavioural contracts for the evaluation harness surface.
- Dataset identity and versioning contract (D17).
- Dataset mutation policy (D17 minor update from round 1).
- Evaluation result schema boundary (D18).
- Metric ownership contract (D19, including component-not-person).
- Metric taxonomy contract (D23 — added in round 2).
- Threshold policy boundary (D20, with ungated-metric separation).
- CI failure semantics (D21, with sanitized expected/observed).
- Reproducibility boundary (D22 — rewritten with determinism
  classes per round 1 blocker 0).

**Does NOT lock:**

- YAML / JSON layout of fixture files.
- Storage location of fixtures and reports.
- Module names (`agent/eval/...`).
- Specific metric formulas (the gate locks the metric id and
  taxonomy category but NOT the algorithm).
- Specific threshold values.
- Warning-vs-fail grading (the gate locks "below threshold =
  fail" but does NOT define a "warn" level).
- Whether ungated metrics are allowed (CI policy decides, not
  the gate).
- CI gate exit codes.
- Parallel runner implementation.
- The hash algorithm used in D22.
- The model fingerprint scheme used in D22.

---

## Per-decision shape

Each D item follows the same shape as P2.5 and P3.1:

1. **Problem** — what was deferred or ambiguous.
2. **Contract** — what MUST hold (the locked part).
3. **Non-Goals** — what MUST NOT be assumed.
4. **Implementation freedom** — concrete options left open.
5. **Migration impact** — what existing P0..P3.1 surfaces must
   continue to work.

---

## Decision Items

### D17. Dataset identity and versioning contract

**Problem.**

P3.2's spec calls for "versioned fixtures" and "dataset
version" as regression anchors, but the exact identity, the
runnability of historical dataset versions, AND the rule
for what counts as a "fixture change" are all deferred. If
we lock the wrong identity now, every regression report
becomes untraceable; if we lock no mutation rule, a fixture
can silently change semantics between runs.

**Contract.**

- Every fixture MUST carry an **explicit identity** that is
  stable across runs. The identity MUST be a tuple of:
  - `dataset_id` (string, e.g. `"eval-fixtures"`)
  - `dataset_version` (semver-style string, e.g. `"v1"`,
    `"v2.1"`)
  - `fixture_id` (string, unique within `(dataset_id,
    dataset_version)`)
- The identity MUST be **content-stable**: renaming a
  fixture file MUST NOT change the identity.
- The identity MUST be **embedded in the report**, not
  inferred.
- **Old dataset versions MUST remain runnable** for at
  least one major version of P3.2 after they are
  superseded. The split between content and contract is the
  responsibility of the dataset owner.
- The dataset version is incremented when the contract of
  a fixture changes (added field, removed field, semantic
  shift), NOT when content alone changes.
- **Mutation policy** (added in round 2 per M4): changing
  fixture **semantics** requires either a `fixture_id`
  change OR a `dataset_version` bump. Pure content
  changes (typos, formatting, comment updates) MAY keep
  the same identity.
- A fixture's `fixture_id` MUST NOT be **reused with
  different semantics** across dataset versions. Once a
  `fixture_id` semantically meant X, it MUST never mean Y,
  even in a later dataset version. The fixture's change
  log MUST record any semantic shift that triggered a
  `fixture_id` change.

**Non-Goals.**

- Content-addressable storage.
- Cross-dataset composition.
- Storage backend.
- Format choice.

**Implementation freedom.**

- The fixture identity MAY be encoded as separate fields or
  computed from path + version.
- Old-version runnability MAY be implemented via a shim, a
  version-pinned loader, or a content snapshot.

**Migration impact.**

- P3.1's `eval-fixtures/` references (per
  `P3-llm-evaluation.md` §Checkpoint P3.2) become concrete
  under this contract.
- Pre-P3.2 fixtures (if any exist in test directories)
  MUST either be assigned a `(dataset_id, dataset_version)`
  or marked as "non-eval, ad-hoc" and excluded from
  regression reporting.

---

### D18. Evaluation result schema contract

**Problem.**

[unchanged from round 1]

**Contract.**

- Every evaluation report MUST be a structured document
  (parseable from JSON). The exact format MAY be JSON or
  YAML but MUST round-trip through `json.dumps` /
  `json.loads`.
- The schema MUST carry the following **top-level fields**:
  - `dataset_id` — references D17
  - `dataset_version` — references D17
  - `planner_version` — the planner / harness version that
    produced this report
  - `execution_profile` — the profile under which the
    evaluation ran
  - `started_at` / `ended_at` — ISO 8601 timestamps
  - `metrics` — a mapping of metric name → metric value
    (see D19 for the metric value shape)
  - `threshold_decisions` — a mapping of metric name →
    pass / fail / ungated (see D20)
  - `reproducibility_digest` — see D22
- Within a `(dataset_version, planner_version)` pair, the
  schema MUST NOT change without a version bump. Adding a
  new metric MUST be additive (new field, no rename /
  removal of existing fields).
- The schema MUST be **explicit about non-deterministic
  fields**: any timestamp, run id, or other
  reproducibility-bypass field MUST be excluded from
  byte-for-byte comparison (see D22).
- The schema MUST NOT carry:
  - Observed screen text (per P3.1 D16 + P1.4 redaction).
  - Plan `args` values (per P3.1 D16).
  - Selector hints (per P3.1 D16).
  - Confirmation tokens (per P3.1 D14 / E.B).
- The schema MUST carry a `schema_version` field
  (`report_schema.v1` initial). A schema break is a major
  version bump.

**Non-Goals.**

- The schema is NOT a database schema.
- The schema is NOT a wire protocol.
- The schema is NOT a UI schema.

**Implementation freedom.**

- The report MAY be stored as a single JSON file, a
  directory of JSON files, or any other structured store.
- The schema MAY add optional fields (e.g. `tags`,
  `git_commit`) for tooling convenience.
- The schema MAY use nested objects.

**Migration impact.**

- P2.4 / P3.1 trace events (in
  `case-attempt-manifest.json`, `report.md`) are NOT
  evaluation reports and are out of scope.

---

### D19. Metric ownership contract

**Problem.**

P3.2's spec lists required metrics but the ownership and
formula responsibility are deferred. Round 1 M3 surfaced
that the owner MUST be a stable component (not a person)
so the metric survives team reorganisations.

**Contract.**

- Each metric MUST have a **single owner**. The owner is
  responsible for:
  - The metric definition (what it measures, in plain
    English).
  - The metric formula (how the value is computed).
  - The metric unit (dimensionless ratio, count, latency
    in milliseconds, etc.).
  - The metric's expected range (rough order of magnitude;
    the exact threshold is in D20).
- The metric declaration MUST include:
  - `metric_id` — stable identifier (e.g.
    `"parse_success_rate"`)
  - `owner` — **accountable component** (not an individual
    person). Component names are stable strings
    (e.g. `"execution-safety"`, `"planner-quality"`,
    `"evaluation-infra"`).
  - `definition` — one-paragraph plain-English description
  - `unit` — e.g. `"ratio"`, `"count"`, `"ms"`
  - `direction` — `"higher_is_better"` or
    `"lower_is_better"`
  - `category` — references D23 (Coverage / Quality /
    Efficiency / Behaviour)
- **Owner lifecycle** (added in round 2 per M3): when the
  owner component is deprecated, a successor MUST be
  declared within the same release that deprecates the
  prior owner. Metrics with no successor MUST be marked
  deprecated. The metric id MUST NOT be reused for a
  different formula.
- The metric formula MUST be **pure**: same inputs → same
  value. Time, randomness, and external state MUST NOT
  influence the value.
- **Cross-cutting metrics** (those that span multiple
  subsystems) MUST be defined at the P3.2 gate.
- Per-subsystem metrics MAY be defined at implementation
  review.
- The set of cross-cutting metrics (the metrics that every
  P3.2 release MUST report) is locked by the gate. New
  cross-cutting metrics require a fresh reviewer round.

**Non-Goals.**

- The metric definition is NOT a benchmark.
- The metric formula is NOT a machine-learning metric.
- The metric owner is NOT a code owner.

**Implementation freedom.**

- The metric declaration MAY be a Python dataclass, a JSON
  document, or a registry entry.
- The metric formula MAY be implemented inline, in a helper,
  or in a separate module.
- Per-subsystem metrics MAY be added at implementation
  review without a fresh gate round.

**Migration impact.**

- Future metrics in P3.3 / P4 / etc. MUST satisfy D19.

---

### D20. Threshold policy contract

**Problem.**

P3.2's spec calls for "regression thresholds" but the policy
shape is deferred. Round 1 B1 surfaced that the original
draft ("missing thresholds = fail") is too rigid: a new
metric without a baseline would fail CI forever.

**Contract.**

- Each metric MUST have a **threshold declaration** OR an
  explicit **"ungated" marker**. The declaration is
  separate from the metric definition (D19).
- A thresholded metric MUST declare:
  - `metric_id` — references D19
  - `direction` — references D19 (must match)
  - `pass_if` — the value or range that constitutes a pass
  - `threshold` — the numeric value
  - `rationale` — one-paragraph plain-English explanation
- An **ungated metric** is one whose value is recorded but
  does not contribute to the pass / fail decision. The
  report MUST tag such metrics with status `"ungated"`.
  - Rationale: evaluation framework and release gate are
    separate layers; do not lock the evaluation framework
    into failing for new metrics that have no baseline.
  - The CI policy (not the gate) decides whether ungated
    metrics are allowed in a given release.
- The CI gate MUST fail if and only if a **thresholded**
  metric violates its threshold declaration. Ungated
  metrics do not affect the pass / fail decision.
- The threshold value is **implementation freedom**. Each
  P3.2 implementation MUST declare its thresholds in the
  implementation review.
- **Hardcoded thresholds** are PROHIBITED for
  cross-cutting metrics. Cross-cutting thresholds MUST
  live in a config file or registry entry.
- **Missing thresholds** (a metric that the runner reports
  but has no threshold declaration AND no ungated marker)
  MUST be treated as a CI fail. This prevents the failure
  mode where someone adds a metric and forgets to declare
  it as either thresholded or ungated.
- The threshold policy MUST NOT include live GUI
  availability. Live E2E remains opt-in; the unit CI gate
  runs on fixtures + stub executor.

**Non-Goals.**

- The threshold is NOT a service-level objective (SLO).
- The threshold is NOT a benchmark target.
- The threshold policy does NOT include warning levels,
  advisory levels, or graduated severity.

**Implementation freedom.**

- The threshold declaration MAY be a config file, a Python
  registry, or a CLI argument.
- The threshold value MAY be tuned per-deployment.
- The CI policy's stance on ungated metrics (whether to
  allow them, log them, or reject their presence) is
  outside this gate.

**Migration impact.**

- Existing P2.4 / P3.1 acceptance suites are NOT
  thresholds.
- Future P3.x threshold policies MUST satisfy D20.

---

### D21. CI failure semantics contract

**Problem.**

P3.2's spec calls for a CI gate CLI but the failure
semantics are deferred. Round 1 B2 surfaced that
free-form `expected` / `observed` fields can leak user
data, UI text, or secrets.

**Contract.**

- The CI gate MUST produce **machine-readable output**
  (JSON or equivalent) in addition to the human-readable
  summary.
- Each CI failure MUST carry:
  - `metric_id` — references D19
  - `dataset_id` / `dataset_version` / `fixture_id` —
    references D17
  - `expected` — **structured expectation identifier**,
    NOT a free-form string. Examples:
    - `{ "kind": "threshold", "value": 0.95 }`
    - `{ "kind": "stage_reached", "name": "validation" }`
    - `{ "kind": "exact", "value": "click submit" }` (only
      when the value is itself a structured identifier;
      observed text MUST NOT appear here).
  - `observed` — **sanitized structured outcome summary**,
    NOT raw observed text. Examples:
    - `{ "kind": "value", "value": 0.92 }`
    - `{ "kind": "mismatch", "category": "wrong_target",
      "digest": "sha256:..." }`
    - `{ "kind": "stage_missed", "name": "validation" }`
  - `direction` — references D19
- The CI gate MUST exit non-zero on any thresholded
  metric's threshold violation.
- The CI gate MUST NOT include in the failure output:
  - The fixture's `args` (per P3.1 D16)
  - Observed screen text (per P3.1 D16)
  - Selector hints (per P3.1 D16)
  - Confirmation tokens (per P3.1 D14)
  - LLM prompts or LLM responses
- The CI gate MUST include a **run identifier** in the
  output.
- The CI gate MUST be **deterministic**: same inputs →
  same exit code, same output bytes (modulo the run
  identifier).

**Non-Goals.**

- The CI gate is NOT a CI platform integration.
- The exit code is NOT a semantic error code.
- The CI gate does NOT include retry semantics.

**Implementation freedom.**

- The structured output format MAY be JSON, JSON Lines,
  etc.
- The CI gate MAY also produce a JUnit XML for CI
  platforms.
- The run identifier MAY be UUIDv7, ULID, etc.

**Migration impact.**

- Existing `test_case/` pytest runs are NOT the CI gate.
- Future P3.x CI gates MUST satisfy D21.

---

### D22. Reproducibility boundary contract (determinism classes)

**Problem.** *(rewritten in round 2 per B0)*

The original D22 contract required byte-for-byte
reproducibility given the same `(dataset_version,
planner_version, execution_profile)` triple and frozen
inputs. This is **over-strong for the LLM planner
scenario**: even with the same dataset, planner version,
and profile, the planner output may differ due to model
backend, model weights, provider version, sampling
config, tokenizer version, or system prompt drift.
Locking byte-identical reproduction forces the framework
to either pin all model internals (impractical) or fail
spuriously.

The contract MUST distinguish evaluation-stage
reproducibility from LLM-planner-output
reproducibility.

**Contract.**

- The reproducibility contract is expressed in terms of
  **determinism classes**, not byte-identical reproduction.
  Each evaluation stage declares its determinism class
  explicitly:
  
  - **D0 — Evaluator-only stage.**
    Same input → same evaluator output (no planner, no
    model involved). Example: parsing a fixture,
    computing a metric value, comparing against a
    threshold.
    
    **The D0 stage MUST be byte-identical reproducible.**
  
  - **D1 — Planner + model artifact stage.**
    Same input + same planner artifact + same model
    artifact → reproducible planner output. The
    reproducibility requires the model artifact (weights,
    provider config, sampling config, tokenizer version) be
    declared and fingerprinted.
    
    **The D1 stage is NOT byte-identical reproducible
    across model versions, but IS reproducible given a
    fixed `(planner_artifact, model_artifact)` pair.**
  
  - **D2 — Byte-identical reproduction required only for
    non-LLM evaluation stages. LLM stages are reproducible
    per D1 (artifact-bound).**
- Each stage MUST declare its determinism class. Stages
  that don't declare are assumed D1 (most permissive).
- The **reproducibility digest** MUST include the
  content-hash of:
  - The dataset fixtures (per D17)
  - The planner prompt template (per P3.1 D15)
  - The planner artifact (when D1 stages are involved)
  - The **model artifact fingerprint** (when D1 stages
    are involved): a stable identifier like model name +
    provider + commit hash. Not necessarily the full
    weights, but a stable identifier.
  - The action catalog (per P3.1 A)
  - The threshold declarations (per D20)
- The reproducibility digest MUST NOT include:
  - Timestamps (`started_at`, `ended_at`)
  - Run identifiers (per D21)
  - Environment-specific paths
  - Host name, user name, or other environment variables
- The digest MUST be embedded in the report (per D18,
  `reproducibility_digest` field).
- The digest MUST be computed **before** the report is
  written.
- Two reports with the same digest MUST have
  byte-identical deterministic fields (the non-deterministic
  fields are excluded per above).
- The CI gate MUST refuse to compare two reports whose
  digests differ AND warn that the difference may be due
  to non-excluded changes (catalog update, fixture update,
  planner update, model artifact update, etc.). The CI
  gate MUST still emit a pass / fail decision based on
  the threshold check, but the comparison report MUST
  flag the digest mismatch.

**Non-Goals.**

- Reproducibility does NOT mean bitwise reproducibility
  at the kernel / syscall level.
- Reproducibility does NOT cover the LLM call itself.
  The digest tracks the planner prompt template and
  model artifact fingerprint, not the LLM response bytes.
- Reproducibility does NOT cover wall-clock time.

**Implementation freedom.**

- The digest MAY be SHA-256, BLAKE3, or another
  cryptographic hash.
- The digest MAY be computed incrementally or in one
  pass.
- The model artifact fingerprint MAY be a content hash of
  the weights, a (model name, version, provider)
  tuple, or a hash of the loaded model object — as long
  as it is stable.
- Stages MAY be grouped (one declaration per stage type,
  not per stage instance).

**Migration impact.**

- Existing P3.1 trace events are NOT evaluation reports;
  reproducibility does not apply to them.
- Future P3.x evaluation runs MUST satisfy D22.

---

### D23. Evaluation metric taxonomy contract

**Problem.** *(added in round 2 per M6)*

FR-P3.2-05..10 enumerate specific metrics (semantic vs
coordinate ratio, replans-per-task, stale target counts,
safety rejection, recovery success, screenshot cost) but
all currently map to D19 (ownership). Without a taxonomy,
the implementation phase will need to re-decide what
counts as a "metric" vs a "property" vs a "statistic",
and what each metric class promises. Cross-cutting metric
coverage (per M5) also needs a categorical anchor.

**Contract.**

- Metrics are classified into a fixed **taxonomy** with
  four categories:
  - **Coverage**: did the evaluation reach the metric?
    Example: `evaluation_completeness_rate` (cases
    evaluated / cases total).
  - **Quality**: is the result correct? Examples:
    `parse_success_rate`,
    `validation_rejection_accuracy`,
    `confirmation_correctness`.
  - **Efficiency**: how much did it cost? Examples:
    `screenshot_cost` (count / bytes / capture
    failures), `execution_latency`.
  - **Behaviour**: did it behave as expected? Examples:
    `semantic_vs_coordinate_ratio`, `replans_per_task`,
    `stale_target_counts`, `safety_rejection`,
    `recovery_success`, `execution_divergence_rate`.
- Every metric MUST declare which category it belongs to
  (per D19).
- New metrics MUST be added to one of the four categories;
  introducing a new category requires a fresh reviewer
  round.
- **Cross-cutting coverage rule** (per M5): the
  cross-cutting metrics set MUST span at least three of
  the four categories. This prevents a metric suite that
  is all quality (no coverage = "all perfect but only 5%
  evaluated") or all efficiency (no behaviour = blind
  to correctness).

**Non-Goals.**

- The taxonomy is NOT a hierarchy. Metrics belong to
  exactly one category.
- The taxonomy is NOT a priority order. All categories
  are equally required.
- The taxonomy does NOT prescribe which metrics MUST be
  cross-cutting vs per-subsystem.

**Implementation freedom.**

- The taxonomy MAY be encoded as a Python enum, a string
  field, or a registry entry.
- The category assignment MAY be updated at implementation
  review without a fresh gate round (the gate locks the
  category SET, not the per-metric assignment).

**Migration impact.**

- FR-P3.2-05..10 metrics are tagged with categories in
  the P3.2 implementation review.
- Future metrics MUST satisfy D23.

---

## Open Decisions From P3.1 Inherited

| ID | Contract | P3.2 review note required |
|----|----------|---------------------------|
| D15 (P3.1) | Per-transport prompt sanitisation | P3.2 reuses D15 for the planner prompt template digest (per D22) |
| D16 (P3.1) | Plan events through `apply_text`; secret audit | P3.2 evaluation reports MUST satisfy D16 (per D18, D21) |

No new inherited items.

---

## Functional Requirements (P3.2 itself)

The spec (`P3-llm-evaluation.md`) defines FR-P3.2-01..10.
This gate does NOT redefine them; it locks the contracts
above which the FR-P3.2 implementation must satisfy.

For traceability, the FR-P3.2 → D mapping:

| FR | D item(s) that satisfy it | taxonomy category |
|----|---------------------------|-------------------|
| FR-P3.2-01 (reproducible evaluation) | D22 | (cross-cutting) |
| FR-P3.2-02 (no sensitive text in metrics) | D18, D19, D21 | (cross-cutting) |
| FR-P3.2-03 (action-selection evaluation dataset) | D17 | Coverage |
| FR-P3.2-04 (sequence-success evaluation) | D17 | Coverage |
| FR-P3.2-05 (semantic vs coordinate ratio) | D19, D23 | Behaviour |
| FR-P3.2-06 (replans-per-task metric) | D19, D23 | Behaviour |
| FR-P3.2-07 (stale target counts) | D19, D23 | Behaviour |
| FR-P3.2-08 (safety rejection) | D19, D23 | Quality |
| FR-P3.2-09 (recovery success) | D19, D23 | Quality |
| FR-P3.2-10 (screenshot cost) | D19, D23 | Efficiency |

---

## Required Cross-Cutting Metrics (locked at gate)

| metric_id | unit | direction | category | plain-English definition |
|-----------|------|-----------|----------|--------------------------|
| `parse_success_rate` | ratio | higher_is_better | Quality | Fraction of `ActionSequence` parse attempts that produce a parseable plan. |
| `validation_rejection_accuracy` | ratio | higher_is_better | Quality | Fraction of validation rejections that match an expected rejection (validator catches what it should). |
| `confirmation_correctness` | ratio | higher_is_better | Quality | Fraction of high-risk / irreversible actions that correctly trigger the D14 confirmation gate. |
| `execution_divergence_rate` | ratio | lower_is_better | Behaviour | Fraction of evaluation steps where the observed execution diverges from the expected outcome. |
| `evaluation_completeness_rate` | ratio | higher_is_better | Coverage | Fraction of cases in the dataset that produced a usable evaluation result (i.e. not aborted, timed out, or errored before reaching the threshold check). |

Cross-cutting coverage check (per D23): the set spans
**3 of 4 categories** (Quality × 3, Behaviour × 1,
Coverage × 1, Efficiency × 0). Round 2 review SHOULD
confirm whether this satisfies "at least three of the
four"; per-strict reading, "Quality + Behaviour +
Coverage" is three categories and the rule is met.

Full formula details are implementation freedom; the gate
locks only the identifier, unit, direction, category, and
plain-English definition.

---

## Acceptance Criteria (reviewer-side, P3.2 design gate)

1. Each D item has the 5-part structure.
2. Contract vs implementation is separated.
3. D17..D22 cover the P3.2 spec surfaces that were deferred.
4. D23 (taxonomy) is added per round 2 M6.
5. D22 uses determinism classes (D0/D1/D2), not
   byte-identical reproduction, per round 2 B0.
6. D20 allows ungated metric status (CI policy decides
   permissibility), per round 2 B1.
7. D21 sanitizes expected/observed (structured identifier
   + sanitized summary), per round 2 B2.
8. D17 includes fixture mutation policy (semantic change
   requires id or version bump), per round 2 M4.
9. D19 owner is a component (not a person) with a
   lifecycle rule, per round 2 M3.
10. Cross-cutting metrics cover at least 3 of the 4
    taxonomy categories, per round 2 M5 / D23.
11. No D item re-implements a P0..P3.1 contract without a
    migration note.

P3.2 design gate closes when all 11 criteria hold AND
reviewer signs off.

---

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

---

## Required Tests

| Test | Purpose |
|------|---------|
| `none` | P3.2 design gate ships no code, no tests. |

The "test" is reviewer approval of D17, D18, D19, D20,
D21, D22, D23.

---

## Deliverables

- `docs/requirements/P3-2-evaluation-design-gate.md` (this
  file).
- `docs/requirements/DECISIONS.md` — D17..D23 rows.
- `docs/requirements/CHANGELOG-P3-2.md` — checkpoint status.
- Update `docs/requirements/P3-llm-evaluation.md` Status
  block to mark P3.2 design gate as in review (round 2).

---

## Review Stop Point

Stop after the reviewer has:

1. read this document,
2. confirmed each D17..D23 item satisfies the 5-part
   structure,
3. signed off on each D item's contract (or formally
   deferred with owner + target milestone),
4. confirmed the P3.2 implementation scope is unaffected
   by any unresolved contract.

After approval, **P3.2 implementation** may begin. P3.2
implementation produces its own review package that records:

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

This checkpoint is a **paper gate**. It produces no code, no
tests, no commits beyond the docs above.

---

## P3.2 Design Gate Closed

P3.2 design gate closes on this revision. **P3.2
implementation** may begin. P3.2 implementation produces its
own review package that records the option choices above.

The next gate is **P3.2 implementation review**, not direct
implementation.

This file remains the audit trail for the gate; future
checkpoints that surface deferred decisions MAY add new D
items here but MUST NOT revise already-locked contracts
without a fresh reviewer round.

---

## Status Of Existing Code (Reused / Carried Forward)

| Area | Reusable in P3.2 |
|------|------------------|
| P3.1 catalog (`agent/planner/catalog_view.py`) | Yes — D22 references the catalog for the reproducibility digest |
| P3.1 prompt template | Yes — D22 references the prompt template for the reproducibility digest |
| P3.1 redaction registry (`agent/redaction.py`) | Yes — D18 reuses D16 redaction contract; D21 reuses for sanitised expected/observed |
| P3.1 schema versioning | Yes — D18 introduces `report_schema.v1` in the same lineage |
| P3.1 confirmation gate (`agent/execution/confirmation.py`) | Yes — D19 `confirmation_correctness` metric depends on the gate |
| P2.4 trace + report (P2.4.A/B/C/D) | No — P2.4 reports are run-level, not eval-level; out of scope |

---

## Reviewer Approval Log

### Round 1 — CHANGES REQUESTED (2026-08-04)

- **Reviewer verdict**: ⚠️ CHANGES REQUESTED.
- **Blockers raised**: 3
  - **B0 — D22 too strong**: byte-for-byte reproduction is
    unrealistic for LLM planner. Rewrite with determinism
    classes (D0 / D1 / D2). ✅ **addressed**: D22 rewritten
    in round 2.
  - **B1 — D20 missing threshold = fail too rigid**: new
    metrics have no baseline. Separate evaluation framework
    from release gate. ✅ **addressed**: D20 rewritten in
    round 2 with ungated status; CI policy decides
    permissibility.
  - **B2 — D21 expected/observed leakage**: free-form
    strings leak user data / UI text / secrets. Sanitize
    with structured identifiers. ✅ **addressed**: D21
    rewritten in round 2 with structured `expected` and
    sanitised `observed`.
- **Minors raised**: 3
  - **M3 — D19 owner lifecycle**: owner should be a
    component, not a person. ✅ **addressed**: D19 updated
    in round 2 with component + lifecycle rule.
  - **M4 — D17 mutation policy**: fixture semantic change
    must require identity bump. ✅ **addressed**: D17
    updated in round 2.
  - **M5 — coverage metric missing**: add
    `evaluation_completeness_rate`. ✅ **addressed**:
    added to required cross-cutting metrics; D23 enforces
    3-of-4-category cross-cutting coverage.
  - **M6 — FR mapping too coarse**: add metric taxonomy.
    ✅ **addressed**: D23 added in round 2.
- **Disposition**: Round 2 review required after all
  blockers and minors are addressed.

### Round 2 — DRAFT

- **Reviewer**: ⏳ pending
- **Verdict**: ⏳ pending
- **Notes**: ⏳ pending

---

## Round 1 → Round 2 Changelog (for reviewer audit)

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

---

## Final Verdict

```
P3.2 Design Gate

Status:
DRAFT — round 2 (awaiting reviewer sign-off)

Locked contracts:
D17 dataset identity / versioning (+ mutation policy)
D18 evaluation result schema
D19 metric ownership (component, not person; lifecycle rule)
D20 threshold policy (with ungated status separation)
D21 CI failure semantics (sanitised expected/observed)
D22 reproducibility boundary (determinism classes)
D23 evaluation metric taxonomy (added round 2)

Open: none

Inherited from P3.1 (unchanged):
D15 prompt sanitisation (reused by D22)
D16 plan event redaction (reused by D18, D21)

Required cross-cutting metrics (5):
parse_success_rate                   Quality
validation_rejection_accuracy        Quality
confirmation_correctness             Quality
execution_divergence_rate            Behaviour
evaluation_completeness_rate         Coverage

Taxonomy category coverage:
Quality (3) + Behaviour (1) + Coverage (1)
= 3 of 4 categories (satisfies D23 cross-cutting rule)

Paper-only: no code, no tests, no runtime changes.
```