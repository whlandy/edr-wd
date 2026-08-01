# P1.1 — Single-Action Dispatcher And Idempotency

## Status

- **Verdict**: implementation complete; awaiting review.
- **Test state**: 73 pytest items in `test_case/test_dispatcher/`, all
  PASSED.
- **Full repo**: 325 passed, 27 skipped.
- **Canonical baseline** (P1.1 close):

      cd /Users/whl/AI-Agent/skill/edr-wd
      pytest -q test_case/

  Expected: **315 passed, 27 skipped** (the full-repo
  `pytest -q` count differs from `pytest -q test_case/` because
  root `pytest` also collects the dispatcher tests inside
  `test_case/`, summing the per-package counts).

## Architecture gate

P1.1 owns:
  * dispatcher mapping (`action_id -> backend method`);
  * bounded LRU idempotency cache;
  * pre-dispatch condition checks;
  * unified `execute_action` MCP tool;
  * observation-snapshot invalidation bridge.

P1.1 does NOT:
  * mutate state outside the dispatcher (cache, observation
    registry, target snapshot registry);
  * depend on agent-side packages (`agent/execution/` is for
    P1.2);
  * introduce trace events (P1.3);
  * introduce screenshot evidence (P1.4);
  * wire every legacy mutation tool to invalidate the snapshot
    (only `execute_action` does so; legacy tools remain byte-
    compatible wrappers).

## Module layout

```
target/action_dispatcher/
    __init__.py       # public surface
    runtime.py        # server_instance_id + backend resolver
    receipts.py       # ActionReceipt envelope + normalize
    cache.py          # bounded LRU idempotency cache
    mapping.py        # action_id <-> tool_name + coverage check
    conditions.py     # enablement / requires / code / ownership
    dispatch.py       # top-level dispatch() entry point
    CHANGELOG.md

target/observation_bridge/
    __init__.py       # public surface
    invalidation.py   # mutating action -> P0.3 invalidate

target/server.py     # EDIT: register execute_action MCP tool

test_case/test_dispatcher/
    test_mapping.py       # ActionDispatchMap + dispatch_target_missing coverage
    test_receipts.py      # ActionReceipt + normalize
    test_cache.py         # LRU idempotency
    test_conditions.py    # enablement / requires / action_code / ownership
    test_dispatch.py      # dispatch() top-level
    test_runtime.py       # server_instance_id + reset
    test_invalidation.py  # bridge -> P0.3 invalidation

test_case/fixtures/dispatch/
    receipt_ok.json
    receipt_disabled.json
```

## Stable dispatch codes

```
ok, unknown_action_id, backend_disabled, precondition_failed,
action_code_mismatch, ownership_mismatch, dispatch_target_missing,
missing_selector_hint, backend_error, invalid_request_id
```

Adding a new code is a MINOR bump; removing or renaming one is a
MAJOR bump (architecture §7.2).

## P0.1 bug discovered and fixed (commit-boundary note)

While implementing the dispatcher, P0.1's
`_build_capability_map` was found to ignore
`BACKEND_NOT_IMPLEMENTED`. Concretely:
`backend_capability_view()["gui.type_text"]["macos_accessibility"]`
returned True even though `BACKEND_NOT_IMPLEMENTED["macos_accessibility"]`
lists `type_text` as not implemented. The catalog digest did NOT
change (the static `ActionSpec` list is canonical; the runtime
capability map is derived).

P1.1 fixes `_build_capability_map` to honour
`BACKEND_NOT_IMPLEMENTED` (catalog digest unchanged at
`sha256:0ecbda1584b1...`). Tests at P0.1 / P0.2 / P0.3 continue
to pass; the dispatcher's `backend_disabled` path now correctly
surfaces the gap.

This fix should be split into a separate `chore` commit during
review-approved commit hygiene (see CHANGELOG below).

## ActionReceipt code migration note (P1.1 review #1 issue 5)

P1.1 defines `STABLE_DISPATCH_CODES` with these entries:

  ok, unknown_action_id, backend_disabled, precondition_failed,
  action_code_mismatch, ownership_mismatch,
  dispatch_target_missing, missing_selector_hint, backend_error,
  invalid_request_id

The historical / generic term `error` (used in legacy backend
return shapes) is intentionally NOT in the dispatch code set.
The P1.1 dispatch codes are finer-grained:

  * `backend_error` replaces the generic `error` slot in legacy
    payloads (dispatcher normalises `{"ok": False, "error": ...}`
    into `code=backend_error`).
  * `precondition_failed`, `action_code_mismatch`,
    `ownership_mismatch`, `dispatch_target_missing` are new
    finer-grained outcomes; pre-P1.1 callers that matched on
    `error` should migrate to the specific code.

When P1.3 (trace events) and P1.4 (evidence) introduce their own
error categorisation, they must reuse `STABLE_DISPATCH_CODES` for
the dispatch outcome and add new codes only at the trace /
evidence layer (architecture §7.2 stability rule).

## P1.1 review #1 fixes

* `observations.reset_for_tests` re-exported under its plain
  name (was previously aliased as `reset_invalidate_for_tests`).
* `ActionDispatchMap.resolve_backend_method` now documents
  explicitly the three-step routing decision; server-inline
  actions are gated on `execution_provider != "backend"`.
* `cache.inflight_lock(request_id)` context manager added; the
  dispatcher serialises concurrent calls sharing the same
  `request_id` so the backend executes exactly once.
* `invalidation.invalidate_after_mutation` docstring clarified
  to describe the actual MVP behaviour ("invalidate the single
  active snapshot tracked by this module").
* `test_server_execute_action.py` added (10 tests): covers
  FastMCP tool registration, signature, JSON envelope, every
  stable code path through the tool, and idempotency through
  the MCP layer.

## Open follow-ups (deferred)

- Legacy `dump_tree` / `list_windows` / `find_control` do not yet
  inject `snapshot_id` and per-control `target_id` into their
  responses. P0.3 deferred this wiring to P1.1, but P1.1 ships
  with the dispatcher + execute_action only; the byte-compatible
  augmentation of the legacy tools is queued for a follow-up
  commit once the snapshot attribution data shape is decided.
- Screenshot evidence, transition detection, and recovery
  branches remain P1.4 / P2.x.

## Canonical verification command

Per P0.3 review #3 doc follow-up, the canonical baseline:

    pytest -q test_case/

Expected at P1.1 close:

    315 passed, 27 skipped in 27.65s

Per-package counts (matching the same baseline run):

    pytest -q test_case/test_dispatcher/        -> 73 passed
    pytest -q test_case/test_observations/      -> 43 passed
    pytest -q test_case/test_protocol_models/   -> 65 passed
    pytest -q test_case/test_action_catalog/    -> 70 passed
    pytest -q test_case/test_target_naming/     -> 49 passed
    pytest -q test_case/                         -> 315 passed, 27 skipped

Future reviews should compare against these numbers; a regression
that changes the count by more than the natural pytest-version
delta is a real signal.