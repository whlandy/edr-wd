# Decisions Log

Single-line log of architectural decisions that were surfaced
as "open" or "deferred" at any prior checkpoint. Each entry
is closed by either an explicit resolution OR an explicit
deferral with rationale.

Format:

```
YYYY-MM-DD  D<n>  TITLE
  Contract:    ...
  Resolution:  ...
  Rationale:   ...
  Source:      P<x>.<y> review round N
```

Entries are append-only.

The log distinguishes **contract decisions** (locked
behavioural commitments) from **implementation choices** (left
to P3.x to decide). The contract is the gate; the
implementation is downstream.

---

## P2.5 Design Review Gate

### Time line (append-only)

- **2026-08-02 round 1** — CHANGES REQUESTED. P2.5 v1 had
  implementation-bound decisions and was rejected. See
  `P2-5-design-gate.md` "Reviewer Approval Log" for full
  findings.
- **2026-08-02 round 2** — APPROVED WITH MINOR NOTES. P2.5 v2
  rewrote D items as 5-part contracts (Problem / Contract /
  Non-Goals / Implementation Freedom / Migration Impact).
  Round 2 reviewer approved with non-blocking notes M1 / M2
  (addressed in this file + DECISIONS.md).

### Round 2 — contracts (current)

#### D1. Unique-identifier format

- Contract: unique + lex-sortable + JSON-round-trippable +
  stable across releases.
- Resolution: contract locked; option (a/b/c) selection at
  P3.1 review.
- Rationale: format choice (UUIDv7 / ULID / current ad-hoc)
  is downstream; what matters is the contract.
- Source: P0.1 Open Decision #1; P1.3 implemented as ad-hoc.

### D2. Redaction layering

- Contract: layer 1 (sanitisation at observation boundary) +
  layer 2 (escape at render boundary), both required,
  rule-id audit preserved end-to-end.
- Resolution: contract locked.
- Source: P2.4.D review note 4.

### D3. Screenshot bytes interface location

- Contract: backend returns raw PNG bytes on demand; capability
  declared in `BACKEND_CAPABILITIES`; supports live capture
  AND re-read from file path.
- Resolution: contract locked; module-ownership (option a/b/c)
  at P3.1 review.
- Source: P0 Open Decision #2.

### D4. Image transport threshold

- Contract: post-encoding size measured; ≤ threshold inline;
  > threshold via managed transfer; never target-local absolute
  path; digest verified at receiver.
- Resolution: contract locked; threshold value (default 1 MiB
  post-encoding) configurable per deployment.
- Source: P2 Open Decision #3.

### D5. Live E2E harness scope

- Contract: P3 reuses without modifying existing
  `test_case/run_*.py` / `test_case/test_e2e/*`; P3's own
  planner E2E is in a separate directory.
- Resolution: contract locked.
- Source: P2.4 acceptance #8.

### D6. LLM prompt sanitisation

- Contract: every prompt-input value treated as untrusted;
  per-transport encoding (Markdown / JSON / structured schema
  / plain text); renderer-generated structure un-escaped.
- Resolution: contract locked.
- Source: P3 spec §P3.1.

### D7. Metrics data sanitisation

- Contract: metrics are structured data; sanitisation is
  field-type aware (text → redaction; numeric → preserved;
  keys → controlled namespace); audit on post-sanitisation
  bytes.
- Resolution: contract locked.
- Source: P2.4 acceptance #7-8.

### D8. Planner capability boundary

- Contract: planner MUST NOT emit actions outside the active
  backend + profile capability set; dispatcher rejects with
  existing `unknown_action_id`; no second capability table.
- Resolution: contract locked; access mechanism (function /
  property / adapter) free at P3.1.
- Source: P3 spec §P3.1.

### D9. Coordinate fallback policy

- Contract: snapshot-validated semantic target preferred;
  coordinate fallback rejected when unique semantic target
  exists; existing `missing_selector_hint` enforcement
  reused for HiSec.
- Resolution: contract locked; detection mechanism (snapshot
  lookup / tool call / hybrid) free at P3.1.
- Source: P3 spec §P3.1.

### D10. Replan / recovery auditability

- Contract: unexpected execution divergence MUST produce an
  auditable replan event with trigger / prior snapshot / new
  plan / reason metadata; emitter-agnostic; lands in trace
  chain (not side channel).
- Resolution: contract locked; event schema + emitter choice
  free at P3.1.
- Source: P2.2 + P2.4.

---

## Open Decisions From Architecture Ledger

Recap from architecture §24 deferred lists:

| ID | Title | Status (P2.5 round 2) |
|----|-------|------------------------|
| 1 | UUIDv7 vs ULID | D1 — contract locked; option at P3.1 |
| 2 | `target/screenshot_bytes()` location | D3 — contract locked; module at P3.1 |
| 3 | Image transport threshold | D4 — contract locked; threshold value at P3.1 |

All three have contracts; option selection is the P3.1
implementation's job.

---

## Prior Closed Decisions (P0..P2.x)

### P0.1
- **Top-level `shared/` vs `target/protocol.py`**: chose
  `target/protocol_models/`.
- **Catalog digest method**: chose SHA-256 of canonical JSON.
- **`runtime_capability` vs `static_capability` split**: kept
  static in P0.1, runtime probe in P1.1 territory.

### P0.2
- **Errors on unknown fields**: raise at `from_dict` time.
- **DAG cycle detection**: three-color DFS.
- **Dry-run no-backend invariant**: enforced via spy + `sys.modules` guard.

### P0.3
- **Fingerprint identity/ownership split**: `IDENTITY_FIELDS` for
  fingerprint; `OWNERSHIP_FIELDS` for resolver step 1.
- **Rect fallback strategy**: containment-based, smallest-area
  tie-break by `target_id`.
- **`STABLE_FIELDS` back-compat alias**: kept pointing at
  `IDENTITY_FIELDS`; deprecate post-P2.

### P1.1
- **Idempotency cache**: bounded LRU; size configurable.
- **Dispatch code set**: 10 codes (see CHANGELOG); `error`
  intentionally excluded.
- **Legacy tool wrapper**: kept as byte-compatible thin
  wrappers; no second capability table.

### P1.2
- **9 evaluators vs 10**: `visual_evidence_captured` deferred
  to P1.4.
- **`visual_evidence_available` flag**: kept as forward-compat
  no-op (P1.4 ships the evaluator unconditionally).
- **State machine skip-capture paths**: explicit
  `PREPARING_STEP → EXECUTING` and `OBSERVING_AFTER → ASSERTING`
  edges.

### P1.3
- **Event id format**: chose ad-hoc — see D1 for future
  contract.
- **Hash chain over `canonical_bytes`**: yes; excludes
  `event_hash` field.
- **Recovery on open**: detect partial trailing line,
  truncate, append `trace_recovered`.
- **Corruption policy**: reject on first prior-line
  corruption; no partial repair.

### P1.4
- **Markdown escape scope**: user-controlled values only;
  renderer-generated structure un-escaped.
- **Image redaction**: actual PNG re-encoding (not just
  metadata); V1 minimum is enough.
- **Path validation**: `relative_path` + `step_id` both
  validated before any write.
- **Capture policy mandatory flag**: baseline + failure
  captures are mandatory and cannot be weakened.

### P2.1
- **Transition signals**: coordinates alone never establish
  a transition (architecture §15.1).
- **Checkpoint kinds**: logical / session / application_state
  / environment_snapshot.

### P2.2
- **Recovery budget**: bounded; tracked in `Limits`.
- **`replan_created` vs `recovery_result`**: planner emits
  `replan_created`; runtime emits `recovery_result`.

### P2.3
- **Run-level report aggregation**: defer to P3.2 metrics.
- **Reruns**: deterministic; same `request_id` semantics.

### P2.4
- **Limits**: `agent/limits.py` dataclass with explicit
  defaults.
- **Restart identity**: `server_instance_id` distinguishes
  prior in-flight work from new requests; old `request_id`
  refused.
- **`visual_evidence_captured` registry entry**: kept
  P1.4-functional; no removal in P2.x.

### P2.4.D
- **Escape scope**: pipe + backtick + backslash + CR/LF +
  control chars; render text untouched.
- **Escape ordering**: control chars → backslash → pipe →
  backtick → CR/LF normalization.
- **Boundary rule**: user-controlled values only.

---

## P3.1 Design Review Gate

### Time line (append-only)

- **2026-08-02 P3.1 round 1** — approved with minor notes; notes addressed
  and implementation subsequently closed.

### Round 1 — contracts (current)

#### D11. Structured-output schema contract

- Contract: schema derived from P0.2 models; `additionalProperties:
  false` everywhere; rejection returns `plan_schema_invalid` +
  JSON pointer + message; exported to
  `test_case/schema/plan.schema.json`.
- Resolution: contract locked.
- Source: P3.1 spec §FR-P3.1-01..02.

#### D12. Plan validation contract

- Contract: 5-stage validation order (schema → catalog →
  action_code → target_ref → DAG); stable codes per P1.1;
  failures do not silently pass.
- Resolution: contract locked.
- Source: P3.1 spec §FR-P3.1-03..06, -10.

#### D13. Replan trigger and bound contract

- Contract: replan triggered on stale target_ref / unexpected
  transition / stale snapshot_id; bounded (no loops);
  `replan_budget_exhausted` at bound; bound value is
  implementation-defined.
- Resolution: contract locked.
- Source: P3.1 spec §"post_step.py".

#### D14. Confirmation policy contract

- Contract: required for `risk ∈ {high, irreversible}` OR
  `(side_effect ∈ {gui_mutation, system_mutation} ∧ profile
  demands)`; deterministic gate; LLM cannot bypass via
  action_id swap / `requires` omission / in-payload
  confirmation; missing confirmation returns
  `confirmation_required`; plan not dispatched.
- Resolution: contract locked.
- Source: P3.1 spec §FR-P3.1-08.

#### D15. Prompt sanitisation contract (re-stating D6 in P3)

- Contract: per-transport encoding (Markdown → `escape_markdown`;
  JSON → `json.dumps`; structured schema → none needed;
  plain-text → P3.1-specific); renderer-generated structure
  un-escaped.
- Resolution: contract locked.
- Source: P2.5 D6 + P3.1 in_scope.

#### D16. Persistence + sanitisation contract

- Contract: plan events (`plan_created`, `plan_validated`,
  `plan_rejected`, `replan_created`) MUST run through
  `RedactionRegistry.apply_text` before persistence; audit
  contract extends; events land in P1.3 trace chain.
- Resolution: contract locked.
- Source: P3.1 spec §"persist.py" + P1.4 redaction.

### Inherited from P2.5 (unchanged contracts; P3.1 review notes required)

| ID | Contract | P3.1 review note required |
|----|----------|---------------------------|
| D1 | Unique + lex-sortable + JSON-round-trippable + stable | P3.1 implementation records option chosen (UUIDv7 / ULID / ad-hoc) |
| D3 | Backend returns raw PNG bytes + capability declared | P3.1 implementation records module chosen (catalog / execution / provider) |
| D4 | Post-encoding threshold; inline ≤ threshold; managed transfer above; digest verified | P3.1 implementation records threshold value (default 1 MiB post-encoding) |


---

## P3.1 Design Gate Closure

- **2026-08-02 round 1** — APPROVED WITH MINOR NOTES.
- Notes addressed in revision commit (D11 schema_version;
  D12 stages must pass; D13 replan_id; D14 executor boundary;
  D15 3 trust boundaries; D16 event schema_version; M1
  completion state; M2 inherited note).
- Gate closed.
- **Next**: P3.1 implementation may begin once it produces
  its own review package that records option choices for
  D1 / D3 / D4.

---

## P3.2 Design Review (D17..D23)

### P3.2-specific (D17..D23)

#### D17. Dataset identity and versioning contract

- Contract: every fixture carries explicit identity
  `(dataset_id, dataset_version, fixture_id)`; identity is
  content-stable and embedded in the report; old dataset
  versions remain runnable for at least one major P3.2
  release after supersession; dataset version bumps on
  contract change (not content change); semantic change to a
  fixture requires `fixture_id` change OR `dataset_version`
  bump; `fixture_id` MUST NOT be reused with different
  semantics across dataset versions; fixture change log MUST
  record any semantic shift.
- Resolution: contract locked (round 2 added mutation
  policy).
- Source: P3.2 spec §Checkpoint P3.2.

#### D18. Evaluation result schema contract

- Contract: structured document (parseable from JSON);
  required top-level fields (dataset_id, dataset_version,
  planner_version, execution_profile, started_at, ended_at,
  metrics, threshold_decisions, reproducibility_digest,
  schema_version); additive evolution within a
  `(dataset_version, planner_version)` pair; no observed
  text / args / selectors / confirmation tokens; schema
  version `report_schema.v1` initial.
- Resolution: contract locked (round 2 unchanged).
- Source: P3.2 spec §Checkpoint P3.2.

#### D19. Metric ownership contract

- Contract: each metric has a single owner (definition /
  formula / unit / expected range); owner MUST be an
  accountable component (not an individual person); owner
  deprecation requires successor in same release; metric id
  MUST NOT be reused for different formula; declaration
  includes `metric_id`, `owner`, `definition`, `unit`,
  `direction`, `category`; cross-cutting metrics defined at
  the gate (not at implementation); cross-cutting metric
  set locked at the gate; per-subsystem metrics MAY be
  added at implementation review.
- Resolution: contract locked (round 2 added component +
  lifecycle rule).
- Source: P3.2 spec §Checkpoint P3.2.

#### D20. Threshold policy contract

- Contract: each metric MUST have a threshold declaration
  OR an explicit "ungated" marker; declaration includes
  `metric_id`, `direction`, `pass_if`, `threshold`,
  `rationale`; ungated metrics contribute NO pass / fail
  decision; CI policy (not the gate) decides whether
  ungated metrics are allowed; threshold violations of
  thresholded metrics fail CI; threshold values are
  implementation freedom; hardcoded thresholds PROHIBITED
  for cross-cutting metrics; missing threshold AND missing
  ungated marker = CI fail; no live GUI availability in
  unit CI.
- Resolution: contract locked (round 2 separated ungated
  status from thresholded pass/fail).
- Source: P3.2 spec §Checkpoint P3.2.

#### D21. CI failure semantics contract

- Contract: machine-readable output (JSON or equivalent) in
  addition to human-readable summary; each failure carries
  `metric_id`, `(dataset_id, dataset_version, fixture_id)`,
  `expected` (structured expectation identifier, NOT
  free-form), `observed` (sanitised structured outcome
  summary, NOT raw observed text), `direction`; non-zero
  exit on any thresholded metric's threshold violation; no
  `args` / observed screen text / selectors / confirmation
  tokens / LLM prompts or responses in failure output;
  deterministic output (modulo run identifier).
- Resolution: contract locked (round 2 sanitised expected
  /observed contract).
- Source: P3.2 spec §Checkpoint P3.2.

#### D22. Reproducibility boundary contract (determinism classes)

- Contract: each evaluation stage declares its determinism
  class explicitly (D0 = evaluator-only byte-identical; D1
  = planner + model artifact reproducible given fixed
  `(planner_artifact, model_artifact)` pair; D2 = byte-
  identical required only for non-LLM stages); stages
  without declaration assumed D1; reproducibility digest
  includes content-hash of dataset fixtures, planner
  prompt template, planner artifact (when D1), model
  artifact fingerprint (when D1), action catalog, and
  threshold declarations; digest excludes timestamps, run
  identifiers, environment-specific paths, host / user
  info; digest embedded in the report (computed before
  write); CI gate warns on digest mismatch but still emits
  pass / fail decision.
- Resolution: contract locked (round 2 rewritten with
  determinism classes; byte-identical no longer required).
- Source: P3.2 spec §Checkpoint P3.2.

#### D23. Evaluation metric taxonomy contract

- Contract: metrics classified into fixed taxonomy with
  four categories (Coverage / Quality / Efficiency /
  Behaviour); every metric declares category; new metrics
  MUST join existing category (introducing new category
  requires fresh reviewer round); cross-cutting metrics
  MUST span at least three of the four categories;
  per-metric category assignment is implementation
  freedom (locked at implementation review).
- Resolution: contract locked (added round 2 per M6).
- Source: P3.2 spec §Checkpoint P3.2.

### Inherited from P3.1 (unchanged contracts; P3.2 reuse)

| ID | Contract | P3.2 reuse |
|----|----------|------------|
| D15 (P3.1) | Per-transport prompt sanitisation | D22 references the planner prompt template for the reproducibility digest |
| D16 (P3.1) | Plan event redaction; secret audit | D18 reuses D16 redaction contract (no observed text / args / selectors / tokens in reports); D21 reuses for sanitised expected/observed |

---

## P3.2 Design Gate Closure

- **2026-08-04 round 1** — ⚠️ CHANGES REQUESTED.
- **2026-08-04 round 2** — ✅ APPROVED.
- Round 2 addresses all 3 blockers (B0 D22 rewritten with
  determinism classes; B1 D20 separated ungated status; B2
  D21 sanitised expected/observed) and all 3 minors (M3 D19
  component + lifecycle; M4 D17 mutation policy; M5
  `evaluation_completeness_rate` added; M6 D23 taxonomy
  added).
- One non-blocking observation carried to implementation
  phase: reviewer suggested P3.2 implementation add an
  Efficiency cross-cutting metric (e.g.
  `screenshot_cost_per_task` or `planner_latency_cost`) so
  the cross-cutting suite spans 4 of 4 categories. This is
  an implementation-phase observation, NOT a design-gate
  requirement.
- Gate closed 2026-08-04.
- **Next**: P3.2 implementation may begin once it produces
  its own review package that records the option choices
  (metric formulas, threshold values, CI exit semantics,
  digest hash function, model fingerprint scheme, stage →
  determinism class mapping) made in code.
