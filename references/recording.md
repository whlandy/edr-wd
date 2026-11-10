# Recording And Replay Reference

End-to-end user guide for desktop recording, assertion authoring, golden-trace
compilation, and replay. Command behavior documented here mirrors
`agent/cli.py` and `target/recording/` as of branch `hermes-remote`.

## Concepts

```text
record start → real user actions + assertions on the target
record stop  → compile → golden-trace.json (+ case.json, pytest, report)
replay       → re-resolve each step's selector on a fresh observation and
               re-execute with the same safety gates
```

A recording session owns exactly one target window: `record start` connects,
locks, and verifies the owning window before installing listen-only hooks.
The recorder never clicks for you — it observes.

## Prerequisites

- The target MCP server must be up (`edr-wd status`, `edr-wd up`).
- Recording requires a unique process/window match; two same-title windows
  fail the lock instead of guessing.
- Recording and replay cannot run concurrently on the same target
  (`recording_session_active`).

## Record

```bash
edr-wd --target TARGET record start \
  --name policy-flow \
  --process-name EDRClient.exe \
  --window-title '^EDRClient$'
edr-wd --target TARGET record status        # also renews the lease
edr-wd --target TARGET record pause
edr-wd --target TARGET record resume
edr-wd --target TARGET record assert        # opens the target-local assertion editor
edr-wd --target TARGET record stop --output-root recordings
```

- `--lease-seconds` (default 300) is the target-local activity lease. If the
  agent disappears mid-recording, a watchdog expires the lease and cleans up
  the recorder window and capture hooks.
- `record status --no-heartbeat` reads state without renewing the lease.
- `record stop` drains the target queue, retrieves each source-redacted frame
  once, validates its digest, and writes artifacts under
  `OUTPUT_ROOT/<safe-flow-name>/`. Transient connection blips during
  collection are retried (interval 1s × 3); exhaustion reports
  `recording_persistence_failed` with artifacts still recoverable target-side.

### Recorder panel (no agent in the loop)

```bash
edr-wd --target TARGET record ui         # agent-side bridge; polls until stop + collection
python3 scripts/open_recorder.py         # double-click entry on the target desktop
```

The panel runs on the target's interactive desktop: window dropdown, refresh,
focus, name, start/pause/stop, and a close prompt that offers stop-and-save.
Do **not** `connect`/`lock` the recorder's own UI windows during a session —
that perturbs the backend's current-window state and corrupts assertion
captures with `captureError` (compiles `incomplete`).

## Assertion Editor

`record assert` opens the target-local assertion editor bound to the active
session. Supported types:

| Type | Expected value | Scope |
|---|---|---|
| `text_equals` / `text_contains` | non-empty string | selected control |
| `value_equals` | string | selected control |
| `visible` / `checked` / `enabled` | boolean | selected control |
| `window_open` | boolean or object | window |
| `control_exists` / `control_absent` | — | explicit selector required |
| `text_contains_time` / `window_text_contains_time` | strftime pattern | control / window |
| `text_time_range` / `window_text_time_range` | `HH:MM[:S[ S]]` + optional `durationSeconds` | control / window |
| `region_query` | structured draft | screen region |

Notes validated against `target/recording/models.py`:

- Time ranges: `10:10` accepts `10:10:00`–`10:10:59`; a positive
  `durationSeconds` defines a half-open range from the prefix (e.g. `10:10` +
  `120` accepts through `10:11:59`, crossing midnight included).
  `timeoutSeconds` is the replay polling budget, not the accepted width.
- For dynamic "today/now" expectations use `*_contains_time` with a strftime
  pattern, rendered at replay time — never the literal timestamp the recorder
  saw.
- `text_contains` / `window_text_contains` reject empty expected text so they
  cannot pass vacuously.
- Control-scoped assertions keep their own stable control match; they do not
  borrow a preceding action's target. An assertion authored in a different
  product window becomes a separate observe-only replay step even with
  `bindPrevious=true`.
- Preview shows the live value; `unknown`/`error` reads are reported as
  undetermined and never silently pass.
- Password controls refuse content assertions (`recording_secret_plaintext`).

An assertion inherits the evidence window of the most recent real input event.
To bind a `window_text_*` assertion to the window it actually reads, record a
real input (e.g. a click) inside that window first.

## Stop And Compile

`record stop` writes under `OUTPUT_ROOT/<safe-flow-name>/`:

| File | Content |
|---|---|
| `recording.json` | raw redacted capture |
| `case.json` | generated test case |
| `golden-trace.json` | replayable golden trace |
| `trace.json` | MaaFramework node-table export (`edr.success-trace/v2`) |
| `test_<name>.py`, `conftest.py`, `pytest.ini` | runnable pytest |
| `compile-report.json` | per-step status, issues, advisories, `maaExport` |
| observation assets | digest-deduplicated frames |

The compiler is honest by design:

- Weak-identity controls (no automationId) get a full ancestry chain as their
  selector; the replay-side observation produces the same ordered chain, and
  `HybridLocator` refuses candidates whose chain does not match exactly.
- Six same-named controls sharing one ancestry chain compile to an explicit
  `locator_ambiguous` — replay never guesses and never falls back to recorded
  coordinates.
- A label that changed between recorded frames in the same window/position
  fails compilation (`compile_weak_identity_volatile_label`) instead of
  producing a trace that deterministically blocks at replay.
- Scroll/drag steps without a bound result verifier compile `incomplete`
  (`compile_scroll_verifier_required` / `compile_drag_verifier_required`):
  a gesture with no observable proof cannot replay as success. Fix by adding
  `record assert` after the gesture, then recompiling — do not fabricate a
  verifier.
- Window transitions must share a causal ID with the preceding action
  (`compile_transition_unbound` otherwise); same-process open/close pairs are
  folded into end-state verifiers.
- A flow recorded across two coexisting same-titled windows cannot compile
  `ready` unless declared identity evidence resolves them: replay locks one
  window per collapsed identity, so action steps whose recorded handles
  differ but whose replay window identity is identical flag the whole group
  (`compile_window_identity_ambiguous`) and compile `incomplete`. Sequential
  reopen (lifecycle-provable) and declared owner chains are the two
  exemptions — see "Multi-Window Flows And Identity" below. Otherwise,
  capture a stable window root identity (automation id) instead.
- A weak-identity target (no automationId) must be proven unique against the
  recorded control tree before a step stays `ready`: zero candidates compile
  `incomplete` (`compile_weak_identity_unresolved`), multiple candidates
  (`compile_weak_or_ambiguous_identity`), a recorder sibling count that
  contradicts the tree (`compile_weak_identity_tree_undercount`), and a label
  that changed between frames in the same position
  (`compile_weak_identity_volatile_label`) all block ready.
- Document-level capture honesty: no accepted events
  (`compile_recording_empty`), dropped input packets
  (`recording_packets_dropped`), correlation errors
  (`recording_correlation_errors`), a lifecycle screenshot that could not be
  captured (`recording_capture_evidence_unavailable` — except a step that
  provably closed its own window, which carries the structural `afterFrame`
  marker instead; see below), and protected text
  without an environment `textSource` (`recording_secret_review_required`)
  each compile `incomplete`.
- A step whose event type is not in the action catalog (`compile_action_unsupported`),
  a selector that cannot be synthesized (`compile_selector_ambiguous`), a bound
  control assertion without its own stable selector
  (`compile_assertion_selector_ambiguous`), an assertion without an expected
  value (`compile_assertion_missing_expected`), and a region assertion whose
  container has no stable identity (`compile_region_container_unresolved`,
  never degraded to coordinates) each compile `incomplete`.
- Swallowed input is surfaced: recorder-UI events and unidentified target
  events become compile issues (`recording_recorder_ui_events`,
  `recording_unidentified_target_events`), so a partial flow cannot compile
  `ready`.

Check readiness before replay:

```bash
python3 - <<'PY'
from agent.recording.models import GoldenTrace
import json, sys
g = GoldenTrace.from_dict(json.load(open(sys.argv[1])))
bad = [(s.step_id, s.action_id, tuple(s.issues))
       for s in (*g.steps, *g.cleanup) if s.status != "ready"]
print("READY" if g.status == "ready" else f"INCOMPLETE ({len(bad)} steps)")
for step_id, action, issues in bad:
    print(f"  {step_id} {action} issues={issues}")
PY
recordings/policy-flow/golden-trace.json
```

Recompile offline without touching the target:

```bash
edr-wd record compile recordings/policy-flow/recording.json \
  --output-root recordings --profile windows_hisec
```

`record stop` / `record compile` also print Chinese advisories for every known
issue code — the advisory list is the triage starting point.

## Replay

```bash
edr-wd --target TARGET replay recordings/policy-flow/golden-trace.json \
  [--profile windows_hisec] [--replay-mode semantic_only|semantic_first|visual_only]
  [--confirm-action ACTION_ID]... [--persist-screenshots]
  [--trace-root result-report/replay-traces]
```

Hard gates, checked in order before any GUI action:

1. `golden.status == "ready"` and every step ready (`golden_trace_incomplete`).
2. Catalog version + digest match the replay runtime
   (`golden_catalog_mismatch`).
3. Profile binding matches the golden's environment
   (`golden_profile_mismatch`); omitting `--profile` uses the target's
   configured app profile.
4. No active recording session (`recording_session_active`).

Replay modes:

- `semantic_only` (default): every step must resolve semantically; image-only
  steps are rejected.
- `semantic_first`: try semantic, fall back to the visual template.
- `visual_only`: template matching only; requires all templates present and
  digest-verified before connecting (missing/alterred assets fail locally
  without a partial GUI run).

The generated pytest selects `semantic_first` automatically when its golden
trace contains an image-only click; set `EDR_WD_REPLAY_MODE` to override
deliberately. Visual matching keeps RGB evidence, retains spatially distinct
repeated-icon candidates, and constrains element search through a uniquely
matched context patch; equal-scoring contexts stay ambiguous and no pointer
action is sent.

Replay writes an append-only trace under `--trace-root`: `events.jsonl`,
`evaluation.json`, and (with `--persist-screenshots`) source-redacted runtime
frames. An interrupted replay still finalizes its trace with `TRACE_ABORTED`
before re-raising.

## Multi-Window Flows And Identity

Flows that move between same-titled windows — a settings dialog opening
language submenus, a window closed and reopened mid-flow — are compiled from
window-identity evidence, never from title proximity or timing guesses:

- **Sequential reopen**: two rounds of the same dialog whose lifecycle
  intervals provably never overlap (first instance destroyed before the
  second appeared, per the recorded timeline) are exempted from identity
  collapse. Coexisting same-titled windows still refuse without a declared
  relation (`compile_window_identity_ambiguous`).
- **Owner chains**: when the capture proves a native owner edge
  (`GetAncestor(GA_ROOTOWNER)` resolved to a recorded instance), the golden
  declares `ownerWindowId` and replay narrows coexisting candidates through
  the live owner handle. An unbound or dead owner keeps the gate closed.
- **Menu-layout structural locators**: weak-identity menu items (Qt menus
  expose no automationId) compile a `menu-layout/v1` contract — the complete
  sibling row set normalized against the recorded menu geometry. Replay
  re-resolves the contract in the current tree, verifies exactly one
  candidate, and clicks with the freshly materialized hit (text from the
  resolved projection, never the recorded label alone and never a recorded
  screen point). When several same-titled windows coexist, the per-step
  selector also narrows the binding: each candidate window's own tree must
  resolve the structure uniquely, and windows already bound in the run are
  excluded from re-resolution.
- **Layout drag classification**: a drag whose start AND end identity are
  title-bar features is a layout move and verifies position, not data; a
  data object dragged onto a title bar keeps its business-result verifier
  (`compile_drag_verifier_required` stays fail-closed on missing start
  evidence).
- **Self-closing steps**: a step that destroys its own locked window cannot
  produce an after-frame. The evidence store probes the locked handle and
  records a structural `afterFrame` marker (only a provably dead HWND
  reclassifies — an unknown stays `recording_capture_evidence_unavailable`);
  the close itself is proven by the folded window-transition verifiers.

## Failure Trajectory Recovery (design 12)

When a recorded flow fails to compile or replay, recovery runs in a fixed
order — never by deleting steps, fabricating verifiers, or editing raw
captures:

1. **Repair**: bounded, evidence-constrained candidate derivations from the
   original capture (parent hash, minimal diff, repair rule recorded);
   validated statically, then through the real replay and independent
   assertions. Only a real replay pass counts as recovered.
2. **Step plan**: if no repair verifies, every raw business operation point
   — not only the compiled ones — maps to an executable edr-wd step with
   explicit gaps for unresolvable targets.
3. **Agent re-record**: the plan executes through the controlled action
   chain with the real recorder started before the first business action;
   stop/persist/collect/compile and a fresh run/binding replay with
   independent assertions follow. Only both layers failing yields an
   `unrecovered` conclusion — authorization or environment gaps are
   `blocked`, never counted as attempts.

## Robustness Boundaries

Stated explicitly so failures are diagnosable, not surprising:

- Recording-time HWNDs distinguish same-title windows during capture but are
  never replay selectors; two captured windows collapsing to one
  title/process selector with no stable root control ID compile `incomplete`.
- `visible` maps to `control_exists` — existence in the tree, not the
  is-visible flag.
- Only the provably value-neutral key class may merge into a following same-Edit
  text commit; other `key_command`s (modifiers, ENTER/TAB/arrows) are
  explicitly unsupported.
- Cross-process Windows flows and real two-pass replay still require target
  acceptance.

## Safety And Redaction

- All capture frames are source-redacted on the target before persistence;
  digest validation runs on retrieval.
- Evidence metadata (window titles, process names) is scrubbed pre-persistence.
- Minimized lock windows refuse capture at the source.
- Credential/password content cannot be captured into assertions or evidence.

## Live Acceptance

Offline fakes never substitute for the live gate:

```bash
EDR_WD_RECORDING_E2E=1 python3 -m pytest -q \
  test_case/test_e2e/test_recording_live_e2e.py
```

Windows and macOS must each record a real flow and replay it twice after
fresh application starts before the feature is declared complete.

## Trust Audit (separate from replay)

To judge whether a recording is worth trusting — separately from whether it
replays:

```bash
python3 ../trace-eval/trust/audit.py recordings/<case>
```

It reads `golden-trace.json` directly and reports per-node evidence along two
axes: will it replay the same way again, and what does a green run prove.
