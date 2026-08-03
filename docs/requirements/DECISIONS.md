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