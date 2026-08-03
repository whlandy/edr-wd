# P2.5 — Design Review Gate (Post-P2 / Pre-P3)

## Status

- **State**: requirements (no implementation).
- **Source contract**: `../architecture/01-action-trace-test-report-design.md` §18, §20, §21; P2 review-deferred items.
- **Mapped checkpoints**: gate before **P3.1 Structured LLM Planner** and **P3.2 Evaluation And Metrics**.
- **Completion state**: UNDONE.
- **Milestone**: this gate exists to convert the open decisions / review-deferred items / scope ambiguities surfaced during the P2.x review cycle into a single reviewable design package before any P3 implementation begins.

P2.5 must NOT start P3 code; it exists to **lock the design** for the P3 reader.

---

## Why this gate exists

P2.x review history (P2.1 / P2.2 / P2.3 / P2.4 / P2.4.D) shipped
production-ready behaviour, but during those rounds the reviewer
flagged several design decisions that were **deferred** or
**never formally resolved**:

- Items deferred to "later phase" or "future checkpoint".
- Items where the implementation chose one path but the reviewer
  noted the other path was equally valid.
- Items where multiple review rounds surfaced a recurring theme
  (e.g. catalog stability, secret hygiene, sanitisation layering)
  that deserves a single design statement rather than a
  per-checkpoint patch.

P3 introduces an LLM planner that consumes the live catalog,
structured output schema, target snapshot, and redaction surface.
If those P2 deferred decisions are not closed before P3 starts,
the planner will lock them in implicitly via its prompt and
output schema. Closing them here avoids a redesign round inside
P3 itself.

---

## In Scope

This checkpoint collects, classifies, and decides the design
questions that P3 will need answered. Each item lists:

- the P2.x review round that surfaced it,
- the current state in code/docs,
- the proposed resolution,
- what the resolution unlocks / locks in for P3.

If the reviewer cannot agree on a resolution, the item is
**deferred to P3 implementation** with an explicit decision log
entry; P2.5 itself never blocks on disagreement but never
silently chooses either.

### Out of Scope

- Any new code under `agent/execution/`, `agent/trace/`,
  `target/`, or `agent/planner/`.
- New dependency adoption (LLM SDKs, schema libraries).
- Catalog bumps or backwards-incompatible signature changes.
- Live E2E harness work; P2.5 is design only.

---

## Decision Items Collected From P2.x Review Cycle

### D1. UUIDv7 vs ULID (resolves a P0 deferral)

**Source**: P0.1 Open Decision #1; P0.3 deferred to P3; P1.3
implemented as `<12 hex ms> <4 hex counter> <4 hex random>` —
not real UUIDv7 or ULID.

**Current state**: ids.py uses an ad-hoc format that is
sortable within one process but does NOT match a published
spec. Cross-process and cross-language readers cannot rely on
the format.

**Proposed resolution**: adopt one of:
- (a) **UUIDv7** — requires Python 3.14 stdlib `uuid.uuid7()`
  (or 3rd-party dep); future-proof.
- (b) **ULID** — Crockford base32; language-agnostic; spec
  at <https://github.com/ulid/spec>.
- (c) **Stay with current ad-hoc format** — accept the cost of
  no published compatibility.

**P3 impact**: the planner needs to emit trace / branch / call
ids as part of `ActionSequence`. If P3 readers (downstream
analytics, third-party tooling) need a standard format, pick (a)
or (b). Otherwise (c) is acceptable.

### D2. Redaction layering: registry + renderer escape

**Source**: P2.4.D review note 4 (defense-in-depth positioning).

**Current state**: two layers exist:
- `apply_text_redaction` / `apply_image_redaction` (P1.4) —
  registry-based replacement at persistence time.
- `escape_markdown()` (P2.4.D) — escape-only at renderer time,
  only on user-controlled values.

**Proposed resolution**: keep both. P2.4.D review confirmed the
defence-in-depth design is correct. P3 inherits both layers:
the planner never sees redacted text (sanitisation at
observation/persistence boundary), AND the renderer escape
runs as a final safety net.

**P3 impact**: planner can prompt with `case_id`, `target`,
`profile`, but never with raw window titles or screen text. The
escape layer ensures that even if a planner bug leaks
uncontrolled text, the markdown renderer produces inert output.

### D3. Where `target/screenshot_bytes()` lives for cross-platform

**Source**: P0 Open Decision #2; P1.4 introduced the interface
with `image_provider` hook in `EvidenceRecord`.

**Current state**: stub `ImageProvider` exists in
`agent/trace/evidence.py`; production backends return dict
shapes (path + metadata) via `target/automation/base.py`'s
`screenshot()` method. The `screenshot_bytes()` capability is
not yet declared in `BACKEND_CAPABILITIES`.

**Proposed resolution**: declare `screenshot_bytes` in
`BACKEND_CAPABILITIES` for both `windows_pywinauto` and
`macos_accessibility`. Update `BACKEND_NOT_IMPLEMENTED` if
either backend genuinely cannot return raw bytes (use the
existing `screenshot(path=...)` fallback only as a last
resort).

**P3 impact**: the planner needs to consume live screenshots
to ground its structured output. If both backends can return
bytes inline, P3.1 can prompt with image references without
resorting to managed transfer.

### D4. Image transport for over-threshold images

**Source**: P2 Open Decision #3.

**Current state**: not yet decided. Base64 inline works for
small/medium; managed transfer for large; target-local
absolute paths forbidden in `trace.md` / `report.md`.

**Proposed resolution**: keep both paths available, with the
following decision matrix:

| Image size | Transport |
|------------|-----------|
| ≤ 1 MiB | base64 inline in `ActionReceipt` |
| > 1 MiB | managed transfer layer (existing) |
| Target wrote to local disk only | explicit copy via dispatcher's transfer helper; verify digest before recording |

**P3 impact**: the planner will receive image bytes inline (1
MiB cap) or via a transfer handle (large). P3.2 metrics
collector needs to know the transfer method for cost / latency
recording.

### D5. Live E2E harness scope for P3

**Source**: P2.4 acceptance #8 + Live E2E requirement.

**Current state**: `test_case/run_*.py` exists; not modified
by P2.x.

**Proposed resolution**: P3 does NOT modify the E2E harness.
P3 produces a separate E2E suite at `test_case/test_planner_e2e/`
that reuses the existing harness without changing it.

**P3 impact**: keeps P2.x E2E stable; isolates planner E2E
to its own directory.

### D6. Prompt sanitisation contract

**Source**: P2.4.D M1 (escape scope conservative), M2 (control
chars stripped).

**Current state**: `escape_markdown()` runs only on
user-controlled values, not on renderer-generated markdown
structure. `safe_case_id`, `trace_id`, `target` are escaped.
Control chars stripped; TAB preserved.

**Proposed resolution**: extend the same contract to the P3
planner prompt:
- All user-controlled values in the prompt go through the
  same escape helper.
- Renderer-generated structure (table separators, headings,
  status enums, fixed strings) stays un-escaped.

**P3 impact**: planner prompt can include live target
metadata without re-introducing injection risk.

### D7. Metrics collection: where do secrets / screen text go?

**Source**: P2.4 acceptance #7-8 (metrics file passes the
no-secret / no-screen-text audit).

**Current state**: P2.4 declares metrics file passes
`test_metrics_no_screen_text` + `test_metrics_no_secret_arguments`
but the metrics writer itself is deferred to P3.2.

**Proposed resolution**: P3.2 metrics writer MUST run the
metrics payload through `RedactionRegistry.apply_text()` before
persistence. A fuzz test exercises the metrics writer with
adversarial inputs (NUL, control chars, Unicode separators,
known secret patterns).

**P3 impact**: locks the metrics surface against the same
adversarial inputs that the existing redactor handles.

### D8. Sandbox / eval target policy

**Source**: P3 spec §P3.1 in_scope (limit LLM output to live
enabled actions).

**Current state**: catalog filtering (`enabled_actions_for`)
exists in P0.1 views; planner is not wired.

**Proposed resolution**: P3.1's planner is restricted to actions
that pass the catalog's `enabled_actions_for(backend, profile)`
filter. The planner never emits an action outside this set;
the dispatcher rejects any out-of-set action with
`code=unknown_action_id`.

**P3 impact**: closes the "planner invents an action" attack
surface. The dispatcher already has the validation; planner
just has to honour it.

### D9. Coordinate fallback policy

**Source**: P3 spec §P3.1 in_scope (reject coordinate
fallbacks when a unique semantic target exists).

**Current state**: catalog declares `pointer.*` actions with
risk=risk_candidate; dispatcher passes through.

**Proposed resolution**: when the snapshot contains a unique
semantic target for the action's selector, the planner MUST
emit the semantic action (e.g. `gui.click`) and NOT emit
`pointer.click_xy`. The dispatcher's `missing_selector_hint`
code already enforces "no selector, no mutation" for HiSec
profiles; extend the same enforcement to planner output.

**P3 impact**: keeps the planner within the deterministic
boundary; no fuzzy "click at coordinate X" paths from LLM
output.

### D10. Branch / replan surfaces

**Source**: P2.2 (RecoveryExecutor); P2.4 (cleanup cascade).

**Current state**: `recovery_requested`, `branch_created`,
`replan_created` events exist; the planner doesn't yet emit
them. RecoveryExecutor consumes recovery events.

**Proposed resolution**: P3.1 planner emits `replan_created`
when it observes an unexpected transition and decides to
re-plan. P3.2 metrics records the replan count.

**P3 impact**: keeps the recovery surface usable from the
planner; doesn't introduce a second planning mechanism.

---

## Open Decisions Deferred From P0 / P1 / P2

Recap from the architecture's deferred-decision ledger, now
P2.5 must commit to a direction:

1. UUIDv7 vs ULID — see D1.
2. `target/screenshot_bytes()` location — see D3.
3. Image transport threshold — see D4.

These are the only open decisions that survived P2.x without
a resolution.

---

## Functional Requirements (P2.5 itself)

This checkpoint has **no code-level functional requirements**.
It produces:

- this design review package
- a `DECISIONS.md` log under `docs/requirements/` recording
  the resolved items (D1..D10 above)
- a CHANGELOG entry under `docs/requirements/P3-llm-evaluation.md`
  linking to P2.5 as the gate that closed the open decisions

Acceptance criteria for P2.5 are reviewer-side, not test-side:

1. **Reviewer can read D1..D10 and either agree with the
   proposed resolution OR explicitly defer with a logged
   decision**. No silent deferral.
2. **All three open decisions from architecture** (§24 / P0
   / P1 / P2 deferred lists) **have a final answer** (either
   "resolved" or "deferred to P3 with documented rationale").
3. **The reviewer confirms** that P3.1 / P3.2 can begin
   without re-opening any of D1..D10.

If the reviewer asks to flip a proposed resolution, P2.5
edits this document in place and re-submits; no
implementation work begins until the gate is closed.

---

## Required Tests

| Test | Purpose |
|------|---------|
| `none` | P2.5 ships no code, no tests. The "test" is reviewer approval of D1..D10. |

If the reviewer wants a specific decision verified via tests,
those tests land under P3.1 / P3.2 (not P2.5).

---

## Deliverables

- `docs/requirements/P2-5-design-gate.md` (this file).
- `docs/requirements/DECISIONS.md` — single-line log of each
  decision and its outcome.
- Update to `docs/requirements/P3-llm-evaluation.md` adding a
  "P2.5 design gate" reference section.

---

## Review Stop Point

Stop after the reviewer has:

1. read this document,
2. signed off on D1..D10 (or explicitly deferred each with a
   rationale),
3. signed off on the P3.1 / P3.2 scope unaffected by these
   decisions.

After approval, **P3.1 implementation** may begin. P3.1 itself
will produce its own review package — P2.5 is not a substitute
for that gate.

This checkpoint is a **paper gate**. It produces no code, no
tests, no commits beyond the docs above.

---

## Status Of Existing Code (Reused / Carried Forward)

| Area | Reusable in P3 |
|------|----------------|
| Catalog filtering (`enabled_actions_for`, `status_action_space`) | Yes — D8 / D9 use it directly. |
| Redaction registry (`apply_text`, `apply_image`) | Yes — D2 / D7. |
| Markdown escape (`escape_markdown`) | Yes — D6. |
| Trace event types (`replan_created`, `branch_created`) | Yes — D10. |
| Recovery executor | Yes — D10. |
| Live E2E harness (`test_case/run_*.py`) | Yes — D5 (read-only). |

No P2.x code is modified by P2.5. P2.5 is docs only.

---

## Final Verdict

```
P2.5 — Design Review Gate (Post-P2 / Pre-P3)

Status: DRAFT awaiting reviewer sign-off
Implementation: NONE (docs only)
Tests: NONE (paper gate)
Decision items: D1..D10 (10 deferred decisions surfaced)
Open decisions: 3 from architecture (D1 / D3 / D4)
```

Reviewer action items:

1. Read D1..D10.
2. For each: agree with proposed resolution, OR propose
   alternative, OR explicitly defer to P3 with rationale.
3. Confirm P3.1 / P3.2 scope is not blocked by any of these.
4. Sign off (or send Request Changes).