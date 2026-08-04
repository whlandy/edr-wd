# P2.5 — Design Review Gate (Post-P2 / Pre-P3)

## Status

- **Round**: 2 (round 1 was CHANGES REQUESTED).
- **Verdict**: ✅ APPROVED; paper-only gate closed after round 2.
- **Code written**: 0
- **Tests added**: 0
- **Branch**: `codex/hermes-p25-design`

## Round 1 → Round 2 changes

Round 1 reviewer flagged:

| Issue | Round 2 fix |
|-------|-------------|
| D1 missing decision criteria | Added "Implementation freedom" with (a/b/c) options + 5 criteria |
| D3 missing options | Added (a/b/c) options for module ownership |
| D4 too absolute (1 MiB) | Reframed as contract: post-encoding measurement, configurable threshold, digest always verified |
| D7 conflated text/numeric | Reframed as contract: field-type aware (text → redaction; numeric → preserved; keys → controlled namespace) |
| D8/D9/D10 too implementation-bound | Reframed as behavioural contracts (planner MUST NOT, contract is emitter-agnostic) |
| D6 implied markdown escape = universal | Rephrased: per-transport encoding, Markdown is one of several |
| No per-D structure | Added 5-part structure (Problem / Contract / Non-Goals / Implementation Freedom / Migration Impact) to every D |
| No explicit exit condition | Added explicit "P2.5 closes when..." block |

## Deliverables

- `docs/requirements/P2-5-design-gate.md` — design review
  package. 10 decision items (D1..D10) in 5-part structure.
- `docs/requirements/DECISIONS.md` — single-line decision
  log. Contract vs implementation explicitly separated.
- `docs/requirements/P3-llm-evaluation.md` — updated Status
  block.

## Decision Items Summary (round 2)

| ID | Contract | Status |
|----|----------|--------|
| D1 | Unique + lex-sortable + JSON-round-trippable + stable | contract locked; option at P3.1 |
| D2 | Layer 1 sanitisation + layer 2 escape, both required | contract locked |
| D3 | Backend returns raw PNG bytes + capability declared | contract locked; module at P3.1 |
| D4 | Post-encoding threshold; inline ≤ threshold; managed transfer above; digest verified | contract locked; threshold value at P3.1 |
| D5 | P3 reuses existing E2E without modifying | contract locked |
| D6 | Per-transport encoding; renderer-generated structure un-escaped | contract locked |
| D7 | Field-type aware sanitisation; numeric preserved; keys controlled | contract locked |
| D8 | Planner MUST NOT emit out-of-set actions; existing `unknown_action_id` reused | contract locked; access mechanism at P3.1 |
| D9 | Semantic target preferred; coordinate fallback rejected when unique semantic exists | contract locked; detection mechanism at P3.1 |
| D10 | Replan event with trigger/snapshot/plan/reason; emitter-agnostic; lands in trace chain | contract locked; schema at P3.1 |

All 10 contracts locked. Option / module / schema selections
deferred to P3.1 implementation (with decision criteria or
existing reusable path noted in each D).

## Acceptance Criteria (reviewer-side)

1. Each D item has the 5-part structure.
2. Contract vs implementation is separated.
3. The 3 architecture open decisions have contracts in D1 / D3 / D4.
4. No D item re-implements a P0..P2.x contract without a migration note.

P2.5 closes when all 4 criteria hold AND reviewer signs off.

## Out of Scope

- Any new code under `agent/`, `target/`, or test directories.
- New dependency adoption (LLM SDKs, schema libraries).
- Catalog bumps or backwards-incompatible signature changes.
- Live E2E harness work.
- P3 implementation of any kind.

## Reviewer Stop Point

After sign-off, P3.1 may begin. P3.1 itself produces its own
review package; P2.5 is not a substitute.
