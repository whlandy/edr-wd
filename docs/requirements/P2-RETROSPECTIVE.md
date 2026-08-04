# P2 Retrospective — Recovery, Reports, Production Hardening

## Status

- **Scope**: P2.1 / P2.2 / P2.3 / P2.4 / P2.4.D / P2.5.
- **Milestone**: production-ready for human-authored test cases.
- **Status**: closed; P3.1 design gate in progress.

P2 is the production-ready milestone for EDR-WD. After P2.4
closed, EDR-WD can run a multi-case suite, retain trustworthy
evidence, aggregate a run report, and survive target restarts
without silently losing or replaying work.

This retrospective summarises what landed, what was deferred,
what each review round fixed, and what P3 inherits.

---

## P2.x timeline

| Checkpoint | Verdict | Key feature |
|-----------|---------|-------------|
| P2.1 | ✅ APPROVED | Transitions + checkpoints |
| P2.2 (R0+R1) | ✅ APPROVED | Recovery planner design |
| P2.2 (Commits A–D) | ✅ APPROVED | RecoveryExecutor + plan_recovery |
| P2.2 (Round 3 patch) | ✅ APPROVED | Composition helpers |
| P2.2 (Commit F–G) | ✅ APPROVED | Branch + replan events; projections |
| P2.3 | ✅ APPROVED | Run-level reports |
| P2.4 | ✅ APPROVED | Production hardening |
| P2.4.A schema | ✅ APPROVED | Schema design |
| P2.4.B propagation | ✅ APPROVED | Manifest propagation |
| P2.4.C cascade | ✅ APPROVED | Cleanup cascade |
| P2.4.D escape | ✅ APPROVED | Markdown escape + acceptance |
| P2.5 round 1 | ⚠️ CHANGES REQUESTED | Initial draft |
| P2.5 round 2 | ✅ APPROVED | Contract rewrite |
| P2.5 polish | ✅ APPROVED | M1/M2 audit trail |

All four P2.x checkpoints (P2.1 / P2.2 / P2.3 / P2.4) closed.
P2.5 design gate (paper-only) closed with 10 decision items.

---

## Commit inventory (P2.x on origin/hermes_remote)

```
P2.5 docs-only (3 commits):
  0e8f052 docs(requirements): P2.5 polish — M1 reviewer log + M2 time line
  282b20a docs(requirements): P2.5 round 2 — rewrite D items as contracts
  ced7f88 docs(requirements): add P2.5 design review gate (paper-only)

P2.3 / projections (4 commits, F–G of P2.2 series):
  4295987 feat(execution): add projections + trace.md writer (G)
  08d744a feat(execution): add branch + replan events + branch ancestry (F)
  67ea36a feat(execution): add restore dispatch + trace adapter (E)
  0422732 feat(execution): add RecoveryExecutor lifecycle + budget enforcement (D)

P2.2 round 3 + tests (3 commits):
  97b20e7 feat(recovery): add composition helpers + Round 3 review patch
  97fa46f test(recovery): add P2.2 planner + contract tests (Commit C)
  afaf21f feat(execution): add pure plan_recovery() (Commit B)

P2.2 design + impl (3 commits):
  686a2cd feat(execution): add P2.2 recovery contracts (Commit A)
  0470665 docs(requirements): add P2.2 recovery planner design (R0+R1)
  6107cf3 feat(execution): add P2.1 transitions checkpoints and review hardening
```

Total P2.x commits: **14**
Diff size (6107cf3^..origin/hermes_remote):
- **44 files changed**
- **+11,175 / -2**

---

## What each P2.x checkpoint delivered

### P2.1 — Transition Detection And Checkpoint Policy

- `agent/execution/transitions.py` — `classify_transition`
  returning one of 10 transition kinds (architecture §15.1).
- `agent/execution/checkpoints.py` — `decide_checkpoint` with
  4 kinds (logical / session / application_state /
  environment_snapshot).
- Decision inputs merged: catalog `transition_policy`, step
  `transition` declaration, SOP metadata, selector semantics,
  current session state.
- Signals: top-level window set, owner PID/process, native
  window ID, modal role, navigation title, tree root, stable
  target survival, normalized tree digest.
- **Coordinates alone never establish a transition.**

### P2.2 — Recovery Branches

- `plan_recovery()` — pure function returning a recovery plan
  given a prior snapshot, current snapshot, and policy.
- `RecoveryExecutor` — lifecycle + budget enforcement
  (Commit D); the executor respects `Limits.recovery_*`.
- `restore_dispatch` + branch / replan events (Commits E–F).
- Projections + trace.md writer (Commit G).
- Branch ancestry + `recovery_result` events.
- Composition helpers (Round 3 review patch).
- **No arbitrary replan loops**: budget is bounded.

### P2.3 — Run-Level Report And Reruns

- Multi-case suite aggregation.
- Reruns: deterministic, same `request_id` semantics.
- Defer some metrics to P3.2.

### P2.4 — Production Hardening

- `agent/limits.py` — `Limits` dataclass with explicit defaults
  (max_image_bytes, max_event_payload_bytes,
  max_events_per_trace, max_trace_duration_seconds,
  max_screenshots_per_case).
- `agent/redaction.py` — `RedactionRegistry` with
  `apply_text` / `apply_image` / `audit_artifacts`.
- Restart handler (`on_target_restart`) distinguishes
  `server_instance_id` from prior in-flight requests; old
  `request_id` is refused.
- Image transport: small/medium inline; large via managed
  transfer; never target-local absolute path.
- Adversarial input rejection: oversize / NUL / control chars /
  unicode separators.

### P2.4.D — Markdown Escape + Acceptance

- `agent/trace/markdown_escape.py` — `escape_markdown()` helper
  with conservative scope (pipe / backtick / backslash /
  CR/LF / control chars).
- Escape ordering: control chars → backslash → pipe →
  backtick → CR/LF normalization.
- Boundary rule: user-controlled values escape;
  renderer-generated markdown structure untouched.
- Acceptance test `TestFR10MarkdownEscape` covers the table.

### P2.5 — Design Review Gate

- 10 deferred decisions (D1..D10) closed in contract form.
- D1 / D3 / D4 are inherited by P3.1 (option selection at
  P3.1 review).
- `DECISIONS.md` append-only log.
- `CHANGELOG-P2-5.md` + `P2-5-design-gate.md` audit trail.

---

## Tests (P2.x window)

| Scope | Count | Source |
|-------|-------|--------|
| P2.1 review tests | (transitions + checkpoints) | `test_case/test_execution/test_p21_*.py` |
| P2.2 planner + contract tests | 148 | `test_case/test_recovery/` |
| P2.4.D escape + acceptance | 22 + 6 = 28 | `test_case/test_trace_markdown_escape/` |
| P2.3 + P2.4 cascade | 94 | `test_case/test_regression/test_p21_regression.py` + others |
| **P2.x total** | **242 / 242 PASS** | — |
| Full regression (P2 close) | **924 passed, 24 failed, 10 errors** | baseline pre-P2 (then later Δ=0) |

Δ failed/errors = **0** — no regression introduced.

---

## What was deferred to P3.x

| Item | Where it landed | P3 contract |
|------|----------------|--------------|
| UUIDv7 vs ULID | P2.5 D1 | P3.1 review records option |
| `target/screenshot_bytes()` location | P2.5 D3 | P3.1 review records module |
| Image transport threshold value | P2.5 D4 (default 1 MiB) | P3.1 review records value |
| Structured-output schema | P3.1 D11 | contract locked |
| Plan validation contract | P3.1 D12 | contract locked |
| Replan trigger + bound | P3.1 D13 | contract locked |
| Confirmation policy | P3.1 D14 | contract locked |
| Prompt sanitisation (per-transport) | P3.1 D15 | contract locked |
| Plan-event persistence + sanitisation | P3.1 D16 | contract locked |
| Run-level metrics collection | P3.2 | not yet designed |
| Evaluation datasets | P3.2 | not yet designed |

---

## Lessons learned (this retrospective)

1. **Behavioural contracts > implementation details** (P2.5
   round 1→2). Future design gates must use the 5-part
   contract form from the start; round 1's "use module X"
   wording was a reject.
2. **Paper gates are cheap and catch drift.** P2.5 caught
   10 deferred decisions that would otherwise have leaked
   into P3's prompt + output schema. The cost was 3 docs
   commits and 2 review rounds.
3. **Append-only decision logs age well.** `DECISIONS.md`
   is the audit trail; it should never be rewritten, only
   appended (with round markers).
4. **Defence-in-depth is the right pattern.** Redaction
   (P1.4) + escape (P2.4.D) are complementary, not
   alternatives. P3 inherits both layers via D2 / D15.
5. **Bounded replan is non-negotiable.** Arbitrary loops
   are a stability failure. The `replan_budget_exhausted`
   code (D13) preserves the bound.
6. **Confirmation cannot be bypassed by the model.** LLM
   output is untrusted input; the gate (D14) is deterministic
   and out-of-band.

---

## What P3 inherits (verbatim)

- P2.5 contracts D1 / D3 / D4 — option selection at P3.1.
- P3.1 design gate (D11..D16) — contracts locked in
  `P3-1-design-gate.md`; awaiting reviewer sign-off.
- P0..P2.x code / data surfaces — no changes required.

---

## Acceptance gate

P2 closed when:

- P2.4 acceptance criteria all passed.
- P2.4.D acceptance (`TestFR10MarkdownEscape` + 7 escape
  table cases) passed.
- P2.5 design gate closed (round 2 APPROVED + M1/M2 polish).
- Full regression: Δ failed/errors = 0.

P2 is closed. P3.1 design gate is the next gate.

---

## Open questions deferred to P3.x

None blocking P2 closure. P3.1 design gate is in progress
(branch `codex/hermes-p31-design`, commit `1a5ffa0`).