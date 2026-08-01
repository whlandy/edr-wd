# P0.3 — Observation And Target Identity

## Status

- **Verdict**: implementation complete; P0.3 review #1 produced 3 blocker
  changes (AssignmentResult typing, IDENTITY/OWNERSHIP split,
  containment-based rect fallback) and 2 non-blocker cleanups.
  Ready for review #2.
- **Test state**: 43 pytest items in `test_case/test_observations/`, all PASSED.
- **Full suite**: 262 passed, 27 skipped.

## Architecture gate

P0.3 owns: deterministic observation-local identity (`target_id`,
`fingerprint`), per-snapshot content-addressed `tree_digest`,
6-step typed-resolution pipeline, snapshot invalidation registry.

P0.3 does NOT:
- probe live backends (`dump_tree` / `find_control` / `list_windows`
  wiring is deferred to P1.1 dispatcher integration);
- mutate the world; P0.3 only records what the live backend
  produced (in tests, synthetic dicts simulate the backend output).
- depend on private symbols from `protocol_models.models`; the
  strict-helper machinery is duplicated in
  `target/observations/_strict.py` and `ProtocolModelError` (the
  stable public type) is the only cross-package import.

## Module layout

```
target/observations/
    __init__.py          # public surface
    _strict.py           # local strict-mode helpers (no protocol_models._)
    enums.py             # TARGET_KIND_*, ARCHITECTURE_P0_3_OBSERVATION_CODES, strategies
    ids.py               # snapshot_id + target_id helpers (reuses protocol_models.ids)
    models.py            # Target, ObservationSnapshot (strict dataclasses)
    fingerprint.py       # IDENTITY_FIELDS, OWNERSHIP_FIELDS, compute_fingerprint
    assignment.py        # AssignmentResult dataclass + assign_target_ids
    snapshot.py          # build_snapshot from pre-collected data
    invalidate.py        # snapshot registry (P1.1 dispatcher wires this)
    resolver.py          # 6-step pipeline + typed errors
    ref.py               # ObservationRef (observer-side, allows empty target_id)

test_case/test_observations/
    test_observation_models.py     # strict mode + round-trip + AssignmentResult typing
    test_fingerprint.py            # IDENTITY/OWNERSHIP split + cross-restart stability
    test_resolver.py               # 6-step pipeline + typed errors
    test_invalidate.py             # registry + is_live + invalidate_snapshot

test_case/fixtures/observation/
    windows_uia_minimal.json       # round-trip fixture
    macos_ax_minimal.json          # round-trip fixture
```

## P0.3 review #1 — fixes applied

| Blocker / Issue | Fix |
|---|---|
| **Blocker 1**: `assign_target_ids` returned bare tuple `(list, str)` | Introduced `AssignmentResult` dataclass; function now returns the typed result. `build_snapshot` updated to read `assignment.targets` and `assignment.tree_digest`. |
| **Blocker 2**: `STABLE_FIELDS` mixed identity and ownership (pid / process_name); an EDR client restart would invalidate every target | Split into `IDENTITY_FIELDS` (`automation_id`, `native_window_id`, `control_type`, `title`, `text`, `kind`) and `OWNERSHIP_FIELDS` (`process_name`, `pid`). Fingerprint uses identity only. Ownership is checked at resolver step 1, never in fingerprint. `STABLE_FIELDS` retained as a back-compat alias for `IDENTITY_FIELDS`. |
| **Blocker 3**: rect fallback used center-to-center distance | Replaced with containment-based check: query rect's center point must fall inside target rect. When multiple candidates contain the query center, pick the smallest-area (deepest nested) target. Tie-break by `target_id` for determinism. |
| **Issue 4**: `STRATEGY_SELECTOR` ambiguous | Renamed to `STRATEGY_BACKEND_NATIVE_SELECTOR = "backend_native_selector"`. |
| **Issue 5**: `ObservationRef` imported private P0.2 helpers | Added `target/observations/_strict.py` with local minimal helpers (`_require_str`, `_allow_none_or_str`, `_require_int_or_none`, `_strict_from_dict_kwargs`). Only `ProtocolModelError` (P0.2 public surface) is imported. |

## P0.3 invariants (locked by tests)

1. `target_id` is observation-local; format `T0001`, `T0002`, ...
   (`format_target_id` / `parse_target_id`).
2. `fingerprint` is content-addressed (`sha256:` + 64 hex); same
   identity fields → same fingerprint, regardless of pid /
   process_name.
3. `tree_digest` excludes `target_id` from the canonical bytes;
   two snapshots of the same underlying control set produce the
   same tree_digest regardless of input order.
4. Resolver refuses ambiguous selectors (never picks one); refuses
   rect-proximity for non-pointer actions; refuses ownership
   mismatch before any candidate lookup; refuses stale snapshot
   ids before any candidate lookup.
5. Snapshot registry treats unknown snapshot_ids as not-live.

## Open follow-ups (deferred)

- Production wiring of `dump_tree` / `find_control` /
  `list_windows` to inject `snapshot_id` + per-control
  `target_id` is **explicitly out of scope** for P0.3 (architecture
  §8 / requirements doc). It will land in P1.1 dispatcher.
- Resolution-error details may grow additional diagnostic fields
  (e.g. `expected_fingerprint`) once the P1.1 dispatcher learns
  to surface them in trace events.