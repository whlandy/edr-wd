# P1.3 — Append-Only Trace Core

## Status

- **Verdict**: implementation complete; review pending.
- 43 pytest items in `test_case/test_trace/`, all PASSED.
- Full repo: **456 passed, 27 skipped** (3 warnings are pre-existing
  P1.1 inflight-lock thread exceptions, not introduced by P1.3).

## Canonical verification command (P1.3 final)

```
pytest -q test_case/test_trace/    -> 43 passed
pytest -q test_case/               -> 456 passed, 27 skipped
```

## Public surface

```
agent/trace/
    __init__.py
    ids.py            — sortable unique event/trace/branch/plan/call ids
                        (12 hex ms + 4 hex random; lex-sortable across
                        sessions sharing a wall clock)
    events.py         — EventType (26 V1 types incl. trace_recovered)
                        + TraceEvent envelope dataclass + from_dict
                        + to_dict + to_hashable_dict
    integrity.py      — canonical_event_hash + stamp_event + verify_chain
                        + IntegrityReport + load_events
    projections.py    — project_step_results (pure read-model
                        rebuild of step-results.json from events)
    manifest.py       — ManifestRecord + from_dict
    store.py          — TraceStore.open/append/finalize/read_events/verify
                        + OpenResult + ChainCorrupted + AlreadyFinalized
    CHANGELOG.md      — this file
```

## EventType catalogue (architecture §12.2 + trace_recovered)

Total V1 event types = **26** (architecture §12.2 = 25 + P1.3 trace_recovered = 1):

```
trace_started, environment_recorded, plan_created, plan_validated,
plan_rejected, observation_recorded, step_started, checkpoint_created,
action_requested, action_started, action_result, transition_detected,
unexpected_transition, screenshot_captured, screenshot_persisted,
expectation_result, step_completed, recovery_requested, branch_created,
recovery_result, replan_created, cleanup_started, cleanup_completed,
trace_completed, trace_aborted, trace_recovered  (P1.3-only)
```

Terminal event types: `trace_completed`, `trace_aborted`.

## Hash chain

```
event_hash = sha256(canonical_bytes(event_dict - {event_hash}))
previous_hash = prior.event_hash (or None for the first event)
sequence_no = monotonic starting from 1
```

Per FR-P1.3-02: `event_hash` is computed over canonical bytes of the
event with `event_hash` itself excluded.

`canonical_bytes` is P0.2's `protocol_models.canonical_json.canonical_bytes`
(sort_keys=True, separators=(",", ":"), UTF-8, no NaN/Inf).

## Crash safety

The store writes each event line with `fsync` after every append.
On open, a partial trailing line is detected (file does not end
with `\n`), hashed for diagnostics, truncated to the last complete
newline, `fsync`ed, and followed by a `trace_recovered` event whose
`previous_hash` follows the last valid event (FR-P1.3-06).

Corruption in any earlier line raises `ChainCorrupted` on open; no
partial repair is attempted (FR-P1.3-07).

## Projections are disposable

`step-results.json` is fully derivable from `events.jsonl` via
`project_step_results()`. Deleting and rebuilding produces
byte-identical output (FR-P1.3-08).

## Functional requirements coverage

| ID | Status | Implementation |
|----|--------|----------------|
| FR-P1.3-01 | ✅ | `TraceStore.append()` emits one event per call; event types are an enum (deterministic). |
| FR-P1.3-02 | ✅ | `canonical_event_hash` excludes `event_hash`. |
| FR-P1.3-03 | ✅ | `test_incomplete_final_line_recovery` — partial tail dropped, prior events verifiable. |
| FR-P1.3-04 | ✅ | `test_tamper_detection_remove_event`, `test_tamper_detection_reorder`. |
| FR-P1.3-05 | ✅ | `test_tamper_detection_insert`. |
| FR-P1.3-06 | ✅ | `TraceStore.open()` recovery path emits `trace_recovered`. |
| FR-P1.3-07 | ✅ | `ChainCorrupted` raised on any prior-line corruption. |
| FR-P1.3-08 | ✅ | `test_projection_byte_identical_replay`. |
| FR-P1.3-09 | ✅ | `ManifestRecord` carries `catalog_digest`, `branch_heads`, `evidence_counts`, `terminal_status`, `integrity_verification_result`. |
| FR-P1.3-10 | ✅ | Sortable unique ids via 12 hex ms + 4 hex random. |

## Acceptance criteria coverage

1. **3-step trace produces documented event count** — `test_open_creates_trace` + `test_two_stores_verify_same_chain` confirm the chain emits + persists the right events.
2. **Two stores verify the same chain** — `test_two_stores_verify_same_chain`.
3. **Manual tamper fails verification** — `test_verify_rejects_tamper_in_middle`.
4. **Crash mid-write: reopen, recover, append, verify** — `test_incomplete_final_line_recovery`.
5. **Projection byte-identical replay** — `test_projection_byte_identical_replay`.

## Review #1 fix log (P1.3 review)

| Finding | Status | Detail |
|---------|--------|--------|
| Blocker 1: UUIDv7/ULID monotonic semantics | ✅ | `ids.py` now uses `<12 hex ms> <4 hex counter> <4 hex random>` with a per-process monotonic counter. Same-ms emissions sort by insertion order; counter overflow bumps the timestamp. |
| Blocker 2: Manifest round-trip | ✅ | `IntegrityIssue.from_dict` and `IntegrityReport.from_dict` added; `ManifestRecord.from_dict` uses them. Round-trip tests cover all three. |
| Required fix: EventType count wording | ✅ | Both `test_integrity.py` docstring and this CHANGELOG say "26 = 25 + 1". |
| Observation: trace namespace collision | ⚠ deferred | P1.3 keeps the package name `agent/trace` (architecture-mandated). Tests pin the import path; future review may rename if collisions become routine. |

## Out of Scope (deferred)

* Screenshot persistence (P1.4).
* Markdown rendering (P1.4 minimal; P2.3 full).
* Transition detection (P2.1).
* Recovery execution (P2.2).
* Run-level aggregation (P2.3).