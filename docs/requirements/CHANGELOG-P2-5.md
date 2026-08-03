# P2.5 — Design Review Gate (Post-P2 / Pre-P3)

## Status

- **Verdict**: implementation complete (docs only).
- **Code written**: 0
- **Tests added**: 0
- **Branch**: `codex/hermes-p25-design`
- **Commits**: pending review.

## Deliverables

- `docs/requirements/P2-5-design-gate.md` — the design review
  package. 10 decision items (D1..D10), 3 of which are the
  long-standing open decisions from the architecture ledger
  (UUIDv7/ULID, screenshot_bytes, image transport threshold).
  Each item has a proposed resolution; reviewer either agrees,
  proposes alternative, or explicitly defers with rationale.
- `docs/requirements/DECISIONS.md` — single-line log of all
  decisions (P2.5 + prior P0..P2.x). Append-only.
- `docs/requirements/P3-llm-evaluation.md` — updated Status
  block to reference P2.5 as the design gate that must close
  before any P3 implementation begins.

## Decision Items Summary

| ID | Title | Proposed Resolution | Status |
|----|-------|---------------------|--------|
| D1 | UUIDv7 vs ULID | PENDING | reviewer |
| D2 | Redaction layering | KEEP BOTH LAYERS | resolved |
| D3 | `screenshot_bytes()` location | PENDING | reviewer |
| D4 | Image transport threshold | 1 MiB | resolved |
| D5 | E2E harness scope | reuse, do not modify | resolved |
| D6 | Prompt sanitisation | same escape as trace.md | resolved |
| D7 | Metrics sanitisation | registry + fuzz test | resolved |
| D8 | Planner sandbox | enabled_actions_for filter | resolved |
| D9 | Coordinate fallback | reject when semantic exists | resolved |
| D10 | Branch / replan | planner emits replan_created | resolved |

7 of 10 resolved; 3 (D1, D3, plus reviewer confirmation on
all) need reviewer sign-off.

## Acceptance Criteria (reviewer-side)

P2.5 ships no code; the "tests" are reviewer approvals:

1. Reviewer can read D1..D10 and either agree OR explicitly
   defer each with a documented rationale.
2. All three open decisions from the architecture ledger
   (UUIDv7/ULID, screenshot_bytes, transport threshold) have
   a final answer.
3. Reviewer confirms P3.1 / P3.2 scope is unaffected by any
   deferred decision.

If the reviewer asks to flip a proposed resolution, P2.5 edits
`P2-5-design-gate.md` in place and re-submits. No code lands
until the gate is closed.

## Out of Scope

- Any new code under `agent/`, `target/`, or test directories.
- New dependency adoption (LLM SDKs, schema libraries).
- Catalog bumps or backwards-incompatible signature changes.
- Live E2E harness work.
- P3 implementation of any kind.

## Reviewer Stop Point

After sign-off, P3.1 may begin. P3.1 itself produces its own
review package; P2.5 is not a substitute.