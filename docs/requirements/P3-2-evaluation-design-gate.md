# P3.2 — Design Review Gate (Pre-Implementation)

## Status

- **State**: requirements (no implementation).
- **Source contract**: `../architecture/01-action-trace-test-report-design.md`
  §21 Metrics; `../requirements/P3-llm-evaluation.md` §"Checkpoint P3.2";
  `../requirements/P3-1-design-gate.md` (predecessor gate).
- **Mapped checkpoints**: gate before **P3.2 implementation** begins.
- **Completion state**: **DRAFT** — awaiting reviewer sign-off (round 1).
- **Round 1 reviewer verdict**: ⏳ pending.
- **Milestone**: this gate exists to lock the **evaluation and
  metrics contracts** that P3.2 must satisfy BEFORE any code lands.
  P3.2 is the first checkpoint that introduces dataset versioning,
  metric ownership, regression thresholds, and CI failure semantics
  — these are non-trivial design surfaces that, if locked by
  accident, freeze the team into a particular metric formula or
  dataset layout.

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
  carry so that two runs can be compared byte-for-byte?
- **Metric ownership**: who defines each metric, and who is
  responsible when the formula drifts?
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
- Dataset identity and versioning contract (what MUST be
  present on every fixture / dataset).
- Evaluation result schema boundary (what MUST be present on
  every report; what MUST NOT change between versions).
- Metric ownership contract (who defines a metric; what
  fields MUST a metric declaration carry).
- Threshold policy boundary (the policy shape; the decision
  surface; what MUST be in a threshold declaration).
- CI failure semantics (the machine-readability contract; the
  fail-vs-pass surface; the reference-from-failure surface).
- Reproducibility boundary (what MUST be reproducible; what is
  explicitly non-deterministic; what goes into the digest).

**Does NOT lock:**

- YAML / JSON layout of fixture files (P3.2 may pick either).
- Storage location of fixtures and reports.
- Module names (`agent/eval/...`) — the spec names these but P3.2
  may reorganise.
- Specific metric formulas (the gate locks "a metric MUST
  define X, Y, Z" but NOT the algorithm).
- Specific threshold values (the gate locks "a threshold MUST
  define X, Y, Z" but NOT the number).
- Warning-vs-fail grading (the gate locks "below threshold = fail"
  but NOT the warn level or the dual-pass-with-warning mode).
- CI gate exit codes (the gate locks machine-readability but
  NOT the exact code per failure class).
- Parallel runner implementation (the gate locks the input/output
  contract but NOT whether the runner is sequential or parallel).

---

## Per-decision shape

Each D item follows the same shape as P2.5 and P3.1:

1. **Problem** — what was deferred or ambiguous.
2. **Contract** — what MUST hold (the locked part).
3. **Non-Goals** — what MUST NOT be assumed by the
   implementation.
4. **Implementation freedom** — concrete options left open.
5. **Migration impact** — what existing P0..P3.1 surfaces must
   continue to work.

---

## Decision Items

### D17. Dataset identity and versioning contract

**Problem.**

P3.2's spec calls for "versioned fixtures" and "dataset version"
as regression anchors, but the exact identity (single id, id +
version, content hash, or combination) and the runnability of
historical dataset versions (can you re-run last quarter's
dataset?) are deferred. If we lock the wrong identity now, every
regression report written under the old format becomes
untraceable.

**Contract.**

- Every fixture MUST carry an **explicit identity** that is
  stable across runs. The identity MUST be a tuple of:
  - `dataset_id` (string, e.g. `"eval-fixtures"`)
  - `dataset_version` (semver-style string, e.g. `"v1"`,
    `"v2.1"`)
  - `fixture_id` (string, unique within `(dataset_id,
    dataset_version)`)
- The identity MUST be **content-stable**: renaming a fixture
  file MUST NOT change the identity.
- The identity MUST be **embedded in the report**, not inferred.
  Two reports from the same `(dataset_id, dataset_version,
  fixture_id)` triple MUST reference identical identities even
  when the underlying files have moved.
- **Old dataset versions MUST remain runnable** for at least
  one major version of P3.2 after they are superseded. A P3.2
  release that drops the ability to run `v1` while `v2` is the
  current default MUST document this in CHANGELOG and provide
  a migration window.
- The dataset version is incremented when the contract of a
  fixture changes (added field, removed field, semantic shift),
  NOT when content alone changes (typo fix in expected output
  description). The split between content and contract is the
  responsibility of the dataset owner.

**Non-Goals.**

- Content-addressable storage (fixtures do not need to be
  addressed by hash in P3.2).
- Cross-dataset composition (running v1 + v2 fixtures together
  is out of scope; each dataset version is run as a unit).
- Storage backend (filesystem, S3, git-LFS — all are
  acceptable).
- Format choice (JSON, YAML, TOML — all are acceptable).

**Implementation freedom.**

- The fixture identity MAY be encoded as separate fields in
  the fixture file or computed from path + version, as long as
  it is stable.
- Old-version runnability MAY be implemented via a separate
  runner shim, a version-pinned dataset loader, or a content
  snapshot stored alongside the current dataset.

**Migration impact.**

- P3.1's `eval-fixtures/` references (per `P3-llm-evaluation.md`
  §Checkpoint P3.2) become concrete under this contract.
- Pre-P3.2 fixtures (if any exist in test directories) MUST
  either be assigned a `(dataset_id, dataset_version)` or
  marked as "non-eval, ad-hoc" and excluded from regression
  reporting.
- Evaluation reports produced before this contract lands
  (none expected, but documented for safety) MUST be readable
  by the new runner or marked as deprecated.

---

### D18. Evaluation result schema contract

**Problem.**

P3.2's spec calls for an "evaluation report" but the schema is
deferred. If the schema is over-specified now, future metrics
cannot be added without breaking old report parsers. If the
schema is under-specified, regression comparisons become
ambiguous (which metric was below threshold last quarter?).

**Contract.**

- Every evaluation report MUST be a structured document
  (parseable from JSON). The exact format MAY be JSON or YAML
  but MUST round-trip through `json.dumps` / `json.loads`.
- The schema MUST carry the following **top-level fields**:
  - `dataset_id` — references D17
  - `dataset_version` — references D17
  - `planner_version` — the planner / harness version that
    produced this report
  - `execution_profile` — the profile under which the
    evaluation ran (e.g. `"default"`, `"strict"`)
  - `started_at` / `ended_at` — ISO 8601 timestamps
  - `metrics` — a mapping of metric name → metric value (see
    D19 for the metric value shape)
  - `threshold_decisions` — a mapping of metric name →
    pass / fail (see D20)
  - `reproducibility_digest` — see D22
- Within a `(dataset_version, planner_version)` pair, the
  schema MUST NOT change without a version bump. Adding a new
  metric MUST be additive (new field, no rename / removal of
  existing fields).
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

- The schema is NOT a database schema. P3.2 does not need
  referential integrity, indexing, or query optimization.
- The schema is NOT a wire protocol. Evaluation reports do
  not need streaming or chunked delivery.
- The schema is NOT a UI schema. P3.2 does not need to
  pre-shape data for a frontend.

**Implementation freedom.**

- The report MAY be stored as a single JSON file, a directory
  of JSON files, or any other structured store.
- The schema MAY add optional fields (e.g. `tags`,
  `git_commit`) for tooling convenience, as long as the
  required fields above are present.
- The schema MAY use nested objects (e.g. `metrics.parse_success
  = {value: 0.97, ...}`) as long as the metric value shape
  itself is consistent (see D19).

**Migration impact.**

- P2.4 / P3.1 trace events (in `case-attempt-manifest.json`,
  `report.md`) are NOT evaluation reports and are out of
  scope. The evaluation report is a separate artifact.
- Future P3.x reports (P3.3, etc.) MUST satisfy D18.

---

### D19. Metric ownership contract

**Problem.**

P3.2's spec lists required metrics (parse success, validation
rejection accuracy, confirmation correctness, execution
divergence, etc.) but the ownership and formula responsibility
is deferred. If two teams define overlapping metrics
differently, regression reports will disagree.

**Contract.**

- Each metric MUST have a **single owner**. The owner is the
  person / team responsible for:
  - The metric definition (what it measures, in plain
    English).
  - The metric formula (how the value is computed).
  - The metric unit (dimensionless ratio, count, latency in
    milliseconds, etc.).
  - The metric's expected range (rough order of magnitude;
    the exact threshold is in D20).
- The metric declaration MUST include:
  - `metric_id` — stable identifier (e.g. `"parse_success_rate"`)
  - `owner` — owner reference (string or pointer)
  - `definition` — one-paragraph plain-English description
  - `unit` — e.g. `"ratio"`, `"count"`, `"ms"`
  - `direction` — `"higher_is_better"` or `"lower_is_better"`
    (so D20's threshold direction is unambiguous)
- The metric formula MUST be **pure**: same inputs → same
  value. Time, randomness, and external state MUST NOT
  influence the value.
- **Cross-cutting metrics** (those that span multiple
  subsystems, e.g. `parse_success_rate` across all
  `ActionSequence` parses) MUST be defined at the P3.2 gate,
  not at implementation. Per-subsystem metrics (e.g.
  `validation_stage_2_rejections`) MAY be defined at
  implementation review.
- The set of cross-cutting metrics (the metrics that every
  P3.2 release MUST report) is locked by the gate. New
  cross-cutting metrics require a fresh reviewer round.
- The list of required cross-cutting metrics is documented
  in §"Required Cross-Cutting Metrics" below.

**Non-Goals.**

- The metric definition is NOT a benchmark. P3.2 does not
  prescribe target values or competitive targets.
- The metric formula is NOT a machine-learning metric.
  P3.2 does not include AUC, F1, perplexity, or other
  model-evaluation metrics at the gate (those may be added
  at implementation review if needed).
- The metric owner is NOT a code owner. Ownership here is
  about definition and formula responsibility, not code
  review.

**Implementation freedom.**

- The metric declaration MAY be a Python dataclass, a JSON
  document, or a registry entry, as long as it is queryable.
- The metric formula MAY be implemented inline, in a helper,
  or in a separate module, as long as it is pure.
- Per-subsystem metrics MAY be added at implementation
  review without a fresh gate round.

**Migration impact.**

- Existing P3.1 metrics (none locked at P3.1) are forward-
  compatible: they MAY be re-declared under D19 in the P3.2
  implementation review.
- Future metrics in P3.3 / P4 / etc. MUST satisfy D19.

**Required Cross-Cutting Metrics (locked at gate):**

| metric_id | unit | direction | plain-English definition |
|-----------|------|-----------|--------------------------|
| `parse_success_rate` | ratio | higher_is_better | Fraction of `ActionSequence` parse attempts that produce a parseable plan. |
| `validation_rejection_accuracy` | ratio | higher_is_better | Fraction of validation rejections that match an expected rejection (i.e. the validator catches what it is supposed to catch). |
| `confirmation_correctness` | ratio | higher_is_better | Fraction of high-risk / irreversible actions that correctly trigger the D14 confirmation gate. |
| `execution_divergence_rate` | ratio | lower_is_better | Fraction of evaluation steps where the observed execution diverges from the expected outcome. |

(Full formula details are implementation freedom; the gate
locks only the identifier, unit, direction, and plain-English
definition.)

---

### D20. Threshold policy contract

**Problem.**

P3.2's spec calls for "regression thresholds without making
live GUI availability a unit-CI requirement" but the policy
shape is deferred. If the gate over-specifies the policy (e.g.
"fail if metric < X"), every threshold change requires a gate
re-round. If the gate under-specifies, regression reports will
be subjective.

**Contract.**

- Each metric MUST have a **threshold declaration**. The
  declaration is separate from the metric definition (D19)
  and may evolve without bumping the metric's contract.
- The threshold declaration MUST include:
  - `metric_id` — references D19
  - `direction` — references D19 (must match)
  - `pass_if` — the value or range that constitutes a pass,
    expressed relative to direction:
    - If `direction == "higher_is_better"`: `pass_if = ">=" threshold`
    - If `direction == "lower_is_better"`: `pass_if = "<=" threshold`
  - `threshold` — the numeric value
  - `rationale` — one-paragraph plain-English explanation of
    why this value
- The CI gate MUST fail if and only if a metric violates its
  threshold declaration. There MUST be no "soft fail",
  "warning pass", or undocumented override.
- The threshold value is **implementation freedom** (the gate
  does not lock numbers). Each P3.2 implementation MUST
  declare its thresholds in the implementation review and the
  reviewer MUST sign off on them as part of the per-commit
  review gate.
- **Hardcoded thresholds** (thresholds baked into the runner)
  are PROHIBITED for cross-cutting metrics. Cross-cutting
  thresholds MUST live in a config file or registry entry so
  that they can be updated without a code change.
- **Missing thresholds** (a metric that the runner reports but
  has no threshold declaration for) MUST be treated as a CI
  fail. This prevents the failure mode where someone adds a
  metric and forgets to declare a threshold.
- The threshold policy MUST NOT include live GUI availability.
  Per the spec: live E2E remains opt-in; the unit CI gate runs
  on fixtures + stub executor.

**Non-Goals.**

- The threshold is NOT a service-level objective (SLO). P3.2
  does not prescribe error budgets, time windows, or alert
  policies.
- The threshold is NOT a benchmark target. P3.2 does not
  prescribe target values or competitive comparisons.
- The threshold policy does NOT include warning levels,
  advisory levels, or graduated severity. Below threshold = CI
  fail; otherwise pass.

**Implementation freedom.**

- The threshold declaration MAY be a YAML / JSON / TOML
  config file, a Python registry, or a CLI argument.
- The threshold value MAY be tuned per-deployment (stricter
  for prod, looser for dev).
- Per-subsystem metrics MAY have their thresholds updated at
  implementation review without a fresh gate round (the gate
  locks the policy shape, not the per-subsystem values).

**Migration impact.**

- Existing P2.4 / P3.1 acceptance suites (in
  `test_case/test_runs/test_acceptance.py` etc.) are NOT
  thresholds. They are tests; they fail on assertion error,
  not on threshold violation. The P3.2 threshold gate is a
  separate, post-test-acceptance check.
- Future P3.x threshold policies MUST satisfy D20.

---

### D21. CI failure semantics contract

**Problem.**

P3.2's spec calls for a CI gate CLI but the failure semantics
are deferred. If the gate emits only an exit code, CI logs lose
traceability (which metric, which fixture, which threshold?). If
the gate emits too much, secret leakage (per D16 / P3.1 D16)
becomes a risk.

**Contract.**

- The CI gate MUST produce **machine-readable output** (JSON
  or equivalent structured format) in addition to the human-
  readable summary. The structured output MUST be parseable by
  CI tooling without re-running the evaluation.
- Each CI failure MUST carry:
  - `metric_id` — references D19
  - `dataset_id` / `dataset_version` / `fixture_id` —
    references D17
  - `expected` — the threshold value (from D20)
  - `observed` — the metric value the runner actually got
  - `direction` — references D19 (so the reader knows
    whether observed > expected is bad or good)
- The CI gate MUST exit non-zero on any threshold violation.
  The exit code MAY be the same code for all failures (a
  simple "1 on fail, 0 on pass" policy is acceptable).
- The CI gate MUST NOT include in the failure output:
  - The fixture's `args` (per P3.1 D16)
  - Observed screen text (per P3.1 D16)
  - Selector hints (per P3.1 D16)
  - Confirmation tokens (per P3.1 D14)
  - LLM prompts or LLM responses
- The CI gate MUST include a **run identifier** in the output
  so that the failure can be cross-referenced with the
  evaluation report (D18).
- The CI gate MUST be **deterministic**: same inputs →
  same exit code, same output bytes (modulo the run
  identifier, which is explicitly non-deterministic per D22).

**Non-Goals.**

- The CI gate is NOT a CI platform integration. P3.2 does
  not lock GitHub Actions / GitLab CI / Jenkins specifics.
- The exit code is NOT a semantic error code. A single
  "non-zero on fail" is sufficient.
- The CI gate does NOT include retry semantics. A failure
  means a failure; CI may re-run the job but the gate does
  not retry internally.

**Implementation freedom.**

- The structured output format MAY be JSON, JSON Lines, or
  another structured format.
- The CI gate MAY also produce a JUnit XML for CI
  platforms that consume it (additional output, same data).
- The run identifier MAY be a UUIDv7, ULID, timestamp + slug,
  or another scheme, as long as it is unique per run.

**Migration impact.**

- Existing `test_case/` pytest runs are NOT the CI gate. The
  gate is a separate CLI that consumes the runner output.
- Future P3.x CI gates MUST satisfy D21.

---

### D22. Reproducibility boundary contract

**Problem.**

P3.2's spec calls for "reproducibility: a regression report
must include a content digest of the catalog, fixtures, and
planner prompt template so results can be traced back to
inputs" but the boundary between "deterministic" and
"non-deterministic" output is deferred. If everything is in
the digest, byte-for-byte comparison breaks on timestamps. If
nothing is in the digest, regression comparison is fuzzy.

**Contract.**

- The evaluation MUST be **byte-for-byte reproducible** given
  the same `(dataset_version, planner_version,
  execution_profile)` triple and the same frozen inputs.
- The **reproducibility digest** MUST include the
  content-hash of:
  - The dataset fixtures (per D17)
  - The planner prompt template (per P3.1 D15)
  - The action catalog (per P3.1 A)
  - The threshold declarations (per D20)
- The reproducibility digest MUST NOT include:
  - Timestamps (`started_at`, `ended_at`)
  - Run identifiers (per D21)
  - Environment-specific paths
  - Host name, user name, or other environment variables
- The digest MUST be embedded in the report (per D18,
  `reproducibility_digest` field). Two reports with the same
  digest MUST be byte-for-byte comparable (modulo the
  excluded fields above).
- The digest MUST be computed **before** the report is
  written; the report MUST reference the digest, not compute
  it lazily.
- The CI gate MUST refuse to compare two reports whose
  digests differ AND warn that the difference may be due to
  non-excluded changes (catalog update, fixture update,
  etc.). The CI gate MUST still emit a pass / fail decision
  based on the threshold check, but the comparison report
  MUST flag the digest mismatch.

**Non-Goals.**

- Reproducibility does NOT mean bitwise reproducibility at
  the kernel / syscall level. P3.2 does not require
  `/usr/bin/python` byte equivalence.
- Reproducibility does NOT cover the LLM call itself. If the
  LLM is non-deterministic (temperature > 0), the plan
  output may differ. The digest tracks the planner prompt
  template, not the LLM response.
- Reproducibility does NOT cover wall-clock time. The
  runner's wall-clock duration is explicitly non-deterministic.

**Implementation freedom.**

- The digest MAY be SHA-256, BLAKE3, or another
  cryptographic hash. Collision resistance is required;
  speed is not.
- The digest MAY be computed incrementally (per fixture) or
  in one pass.
- The comparison report MAY use textual diff, JSON diff, or
  another diff format.

**Migration impact.**

- Existing P3.1 trace events are NOT evaluation reports;
  reproducibility does not apply to them.
- Future P3.x evaluation runs MUST satisfy D22.

---

## Open Decisions From P3.1 Inherited

| ID | Contract | P3.2 review note required |
|----|----------|---------------------------|
| D15 (P3.1) | Per-transport prompt sanitisation | P3.2 reuses D15 for the planner prompt template digest (per D22) |
| D16 (P3.1) | Plan events through `apply_text`; secret audit | P3.2 evaluation reports MUST satisfy D16 (per D18) |

No new inherited items.

---

## Functional Requirements (P3.2 itself)

The spec (`P3-llm-evaluation.md`) defines FR-P3.2-01..10.
This gate does NOT redefine them; it locks the contracts
above which the FR-P3.2 implementation must satisfy.

For traceability, the FR-P3.2 → D mapping:

| FR | D item(s) that satisfy it |
|----|---------------------------|
| FR-P3.2-01 (reproducible evaluation) | D22 |
| FR-P3.2-02 (no sensitive text in metrics) | D18, D19 |
| FR-P3.2-03 (action-selection evaluation dataset) | D17 |
| FR-P3.2-04 (sequence-success evaluation) | D17 |
| FR-P3.2-05 (semantic vs coordinate ratio metric) | D19 |
| FR-P3.2-06 (replans-per-task metric) | D19 |
| FR-P3.2-07 (stale target counts) | D19 |
| FR-P3.2-08 (safety rejection metric) | D19 |
| FR-P3.2-09 (recovery success metric) | D19 |
| FR-P3.2-10 (screenshot cost metric) | D19 |

---

## Acceptance Criteria (reviewer-side, P3.2 design gate)

1. Each D item has the 5-part structure.
2. Contract vs implementation is separated.
3. D17..D22 cover the P3.2 spec surfaces that were deferred.
4. No D item re-implements a P0..P3.1 contract without a
   migration note.
5. Required cross-cutting metrics (D19) are present in the
   table.
6. The reproducibility boundary (D22) explicitly excludes
   the LLM call itself.
7. The CI failure semantics (D21) explicitly forbid secret
   leakage.
8. The threshold policy (D20) explicitly forbids hardcoded
   thresholds for cross-cutting metrics.

P3.2 design gate closes when all 8 criteria hold AND
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

---

## Required Tests

| Test | Purpose |
|------|---------|
| `none` | P3.2 design gate ships no code, no tests. |

The "test" is reviewer approval of D17..D22.

---

## Deliverables

- `docs/requirements/P3-2-evaluation-design-gate.md` (this
  file).
- `docs/requirements/DECISIONS.md` — append new D17..D22
  rows.
- `docs/requirements/CHANGELOG-P3-2.md` — checkpoint status.
- Update `docs/requirements/P3-llm-evaluation.md` Status
  block to mark P3.2 design gate as in-progress.

---

## Review Stop Point

Stop after the reviewer has:

1. read this document,
2. confirmed each D17..D22 item satisfies the 5-part
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
| P3.1 redaction registry (`agent/redaction.py`) | Yes — D18 reuses D16 redaction contract |
| P3.1 schema versioning (`plan_schema.v1`, `plan_created.v1`) | Yes — D18 introduces `report_schema.v1` in the same lineage |
| P3.1 confirmation gate (`agent/execution/confirmation.py`) | Yes — D19 `confirmation_correctness` metric depends on the gate |
| P2.4 trace + report (P2.4.A/B/C/D) | No — P2.4 reports are run-level, not eval-level; out of scope |

---

## Reviewer Approval Log

### Round 1 — DRAFT

- **Reviewer**: ⏳ pending
- **Verdict**: ⏳ pending
- **Notes**: ⏳ pending

---

## Final Verdict

```
P3.2 Design Gate

Status:
DRAFT — awaiting reviewer sign-off (round 1)

Locked contracts:
D17 dataset identity / versioning
D18 evaluation result schema
D19 metric ownership
D20 threshold policy
D21 CI failure semantics
D22 reproducibility boundary

Open: none

Inherited from P3.1 (unchanged):
D15 prompt sanitisation (reused by D22)
D16 plan event redaction (reused by D18)

Required cross-cutting metrics:
parse_success_rate
validation_rejection_accuracy
confirmation_correctness
execution_divergence_rate

Paper-only: no code, no tests, no runtime changes.
```