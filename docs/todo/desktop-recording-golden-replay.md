# Desktop Recording And Golden Replay — Implementation Tracker

> Source design: `docs/architecture/03-desktop-record-script-golden-replay-design.md`
>
> Status: `[ ]` pending · `[~]` in progress · `[x]` verified

This tracker is authoritative for implementation progress. A task is complete only
when its implementation, focused tests, and relevant regression tests pass. Live
capture tasks additionally require evidence from an authorised target.

## Phase 0 — Schema And Offline Compiler

- [x] P0.1 Strict raw-recording wire models and schema
- [x] P0.2 Replay selector, recorded-case, golden-trace, and evaluation models
- [x] P0.3 Deterministic event coalescing and action-catalog mapping
- [x] P0.4 Stable selector synthesis without replaying observation-local IDs
- [x] P0.5 Canonical JSON artifacts and lightweight pytest projection
- [x] P0.6 Unit and offline integration tests

Phase 0 acceptance:

- unknown fields and unsupported schema versions are rejected;
- click/double-click/toggle/text/assertion compilation is deterministic;
- unsupported, ambiguous, and secret-review steps remain visible and make the
  golden trace incomplete;
- `expected=false` and `expected=""` survive round trips;
- generated pytest imports and calls only the public replay API;
- canonical input produces byte-identical JSON output.

## Phase 1 — Target Capture Session And Windows

- [x] P1.1 Scoped capture-session lifecycle, lease, receipts, and persistence
- [x] P1.2 Recording management MCP tools and CLI commands
- [x] P1.3 Windows mouse/keyboard hooks with callback-to-queue isolation
- [x] P1.4 UIA hit testing, value/toggle/select commit correlation, and source redaction
- [x] P1.5 User-visible indicator, explicit editor, shortcut, scoped current-pointer
  hit test, and last-semantic-target fallback
- [x] P1.5a Capture diagnostics prevent dropped/error events from producing a
  silently ready golden trace
- [~] P1.6 Windows fake-hook integration tests complete; authorised live acceptance pending
- [x] P1.7 Press/release drag correlation and `SetWinEventHook` window-transition capture

## Phase 2 — macOS Capture

- [x] P2.1 Accessibility/Input Monitoring permission preflight
- [x] P2.2 CGEventTap queue and AX target correlation
- [x] P2.3 macOS source redaction and shared indicator/assertion editor with
  scoped current-pointer hit test and semantic fallback
- [~] P2.4 Shared-fixture parity tests complete; authorised live acceptance pending
- [x] P2.5 Mouse-down tap events and `CGWindowList` transition polling share the Windows correlator

Previously deferred recorder actions (now implemented; live acceptance still
runs with the Phase 1/2 matrix):

- [x] Scroll compiler: bind an explicit content/position verifier to each recorded scroll
- [x] Drag capture/replay: correlate stable start/end coordinates and require a result verifier
- [x] Automatic window-transition capture: subscribe to platform window events,
  assign the triggering action's causal ID, and emit opened/closed metadata
- [x] Persistent replay screenshots: add a target-side source-redacted replay
  capture path before writing runtime frames into execution reports

## Phase 3 — Golden Replay And Evaluation

- [x] P3.1 Semantic selector resolution against a fresh observation per step
- [x] P3.2 Replay materializer adapter for the existing `AtomicExecutor`
- [x] P3.3 Catalog/profile/incomplete gates and confirmation enforcement
- [x] P3.4 Replay evaluation projection and append-only trace integration
- [x] P3.5 Fake-target end-to-end replay, trace-integrity, and cleanup tests

## Phase 4 — Protected Visual Fallback

- [x] P4.1 Automatic target capture, source redaction, digest-verified transfer,
  and element/context template attachment
- [x] P4.2 Scale-aware bitmap matching with confidence and uniqueness margin
- [x] P4.3 Window-bound, ownership, and action-risk guards
- [x] P4.4 Visual fallback safety and evaluation tests
- [x] P4.5 `replay_capture` source-redacted runtime frames persisted under
  `--persist-screenshots`, refused when unredacted or not window-scoped

Screenshot lifecycle:

- [x] One source-redacted INIT baseline is captured before hooks start
- [x] Each accepted lifecycle event captures one after frame and reuses the
  preceding after frame as its `beforeCapture`
- [x] Agent transfer and observation persistence deduplicate shared capture IDs

## Completion Gate

- [ ] Windows and macOS each pass the design's live acceptance matrix
- [ ] A recorded flow replays twice in a fresh application session
- [x] Generated pytest executes through the public replay API twice in two
  distinct offline fake application sessions with fresh observation-local IDs
- [x] Automated secret fixtures find no captured plaintext in recording or visual artifacts
- [x] Ambiguous controls fail without clicking a first match
- [x] Required assertions, cleanup, and trace integrity determine task success
- [x] Full default unit suite and relevant regression suites pass

## Progress Log

- 2026-08-17: tracker created; Phase 0 implementation started.
- 2026-08-17: Phase 0 verified with 11 focused tests; default unit suite
  passed (`1281 passed, 611 deselected`).
- 2026-08-17: target-local lifecycle, scope filtering, source secret guard,
  lease expiry, and offline `record compile` CLI added; 17 recording tests pass.
- 2026-08-17: recording management MCP/CLI lifecycle, agent-side stop/compile
  persistence, Windows low-level hook driver, bounded callback queue, foreground
  ownership filter, and UIA pointer hit testing added; 26 recording tests and
  the full default suite pass (`1296 passed, 611 deselected`).
- 2026-08-17: fresh per-step semantic replay, atomic-executor materialization,
  three control-state expectation evaluators, catalog/profile/confirmation
  gates, MCP observation/dispatch adapters, replay CLI, evaluation projection,
  and hash-chained replay traces added. Recording tests pass (`35 passed`);
  full default suite passes (`1305 passed, 611 deselected`).
- 2026-08-17: macOS permission preflight, listen-only CGEventTap queue, AX
  correlation, explicit cleanup replay, and guarded semantic-first visual
  fallback added. Recording tests pass (`41 passed`); full default suite passes
  (`1311 passed, 611 deselected`).
- 2026-08-17: target-local always-on-top indicator, explicit assertion editor,
  assertion shortcut filtering, redact-before-crop templates, Pillow scale
  matcher, and package-safe CLI/server imports added. Recording tests pass
  (`47 passed`); full default suite passes (`1317 passed, 611 deselected`).
- 2026-08-17: current-pointer assertion preselection, source-redacted in-memory
  recording captures, digest-verified agent transfer, automatic visual-template
  attachment, window-origin coordinate projection, template integrity gates,
  and cleanup trace attribution added. Recording tests pass (`66 passed`).
  Full default suite passes (`1336 passed, 612 deselected`); explicit historical
  regression suite passes (`585 passed, 1363 deselected`). An opt-in live
  recording lifecycle E2E is collected behind `EDR_WD_RECORDING_E2E=1`.
- 2026-08-17: strict compiled-case schema/loaders, replay trace ordering and
  evaluation persistence, assertion type/timeout enforcement, activity lease,
  stop/drain deadlock prevention, fresh lock verification, causal window
  transition compilation, secure macOS capture handling, and recorder UI
  masking were verified. Screenshot evidence now forms an INIT -> per-event
  after chain with capture-ID deduplication. Unsafe recorded scroll/drag steps
  remain explicit and incomplete until their result verifiers are implemented.
  Strict verifier loading and unbound-transition visual-evidence alignment were
  subsequently added. Recording tests pass (`82 passed`); full default suite
  passes (`1353 passed, 612 deselected`); regression passes
  (`585 passed, 1380 deselected`).
- 2026-08-17: lifecycle screenshots now use one INIT baseline plus one after
  frame per accepted event; double-click evidence spans the first before frame
  through the second after frame. Capture health is persisted, secret/control
  enumeration and screenshot scope/origin fail closed, and visual replay
  rejects non-window frames. Focused text capture reads only the final scoped
  edit value, right/middle clicks preserve button semantics using a fresh
  semantic target, empty recordings cannot pass vacuously, target app profiles
  bind into generated goldens, and capture-transfer failures persist in the
  compile report. Automatic window-transition capture, verified scroll/drag,
  and persistent source-redacted replay screenshots remain explicit deferred
  work. Recording tests pass (`118 passed`); full default suite passes
  (`1392 passed, 612 deselected`); regression passes
  (`585 passed, 1419 deselected`). Compile-with-warnings, diff whitespace, CLI
  help, and target-server import gates also pass.
- 2026-08-17: the four deferred recorder actions were implemented. Explicit
  verifiers now bind to their action through `bindPrevious`/`causalId`, so
  scroll and drag can reach ready; drag is correlated from a press/release pair
  past the OS drag threshold and replays both endpoints as anchors inside
  freshly resolved rectangles; Windows `SetWinEventHook` and macOS
  `CGWindowList` polling feed causally bound `window_transition` events through
  a shared `CompositeHookDriver`; and `replay_capture` reuses the recording
  source-redaction boundary so `--persist-screenshots` can write runtime frames
  into the replay trace, refusing unredacted or full-screen frames. Recording
  tests pass (`155 passed`); full default suite passes
  (`1429 passed, 612 deselected`); regression passes
  (`585 passed, 1456 deselected`). Live acceptance on authorised Windows and
  macOS targets remains the only open gate.
