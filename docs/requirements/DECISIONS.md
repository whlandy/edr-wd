# Decisions Log

Single-line log of architectural decisions that were surfaced
as "open" or "deferred" at any prior checkpoint. Each entry
is closed by either an explicit resolution OR an explicit
deferral with rationale.

Format:

```
YYYY-MM-DD  D<n>  TITLE
  Resolution: ...
  Rationale: ...
  Source:    P<x>.<y> review round N
```

Entries are append-only.

---

## P2.5 Design Review Gate (2026-08-02)

### D1. UUIDv7 vs ULID

- Resolution: **PENDING** (reviewer's call).
- Options:
  - (a) UUIDv7 (Python 3.14 stdlib or 3rd-party dep).
  - (b) ULID (Crockford base32; language-agnostic).
  - (c) Keep current ad-hoc `<12 hex ms> <4 hex counter> <4 hex random>` format.
- Source: P0.1 Open Decision #1; P1.3 implemented as (c).

### D2. Redaction layering

- Resolution: **KEEP BOTH LAYERS** (defence-in-depth).
- Source: P2.4.D review note 4.

### D3. `target/screenshot_bytes()` location

- Resolution: **PENDING**.
- Source: P0 Open Decision #2; P1.4 left as `image_provider` hook.

### D4. Image transport threshold

- Resolution: **1 MiB** (base64 inline ≤ 1 MiB; managed transfer > 1 MiB; never target-local absolute path).
- Source: P2 Open Decision #3.

### D5. Live E2E harness scope for P3

- Resolution: **P3 reuses without modifying** the existing harness.
- Source: P2.4 acceptance #8.

### D6. Prompt sanitisation contract

- Resolution: **Same escape helper as `trace.md`** for all user-controlled values in planner prompt; renderer-generated structure un-escaped.
- Source: P2.4.D M1 / M2.

### D7. Metrics file sanitisation

- Resolution: **Metrics writer runs payload through `RedactionRegistry.apply_text()`** before persistence; fuzz-tested.
- Source: P2.4 acceptance #7-8.

### D8. Planner sandbox / eval target policy

- Resolution: **Planner restricted to `enabled_actions_for(backend, profile)`**; dispatcher rejects out-of-set with `unknown_action_id`.
- Source: P3 spec §P3.1 in_scope.

### D9. Coordinate fallback policy

- Resolution: **Reject coordinate fallback when unique semantic target exists**; dispatcher's `missing_selector_hint` enforces "no selector, no mutation".
- Source: P3 spec §P3.1 in_scope.

### D10. Branch / replan surfaces

- Resolution: **P3.1 planner emits `replan_created`** on unexpected transition; P3.2 metrics records replan count.
- Source: P2.2 + P2.4.

---

## Open Decisions From Architecture Ledger

Recap from architecture §24 deferred lists:

| ID | Title | Status (P2.5) |
|----|-------|---------------|
| 1 | UUIDv7 vs ULID | D1 |
| 2 | `target/screenshot_bytes()` location | D3 |
| 3 | Image transport threshold | D4 |

All three either resolved or pending reviewer call. P2.5
closes the ledger entry "open decisions" at this point.

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
- **Event id format**: chose ad-hoc (c) — see D1.
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