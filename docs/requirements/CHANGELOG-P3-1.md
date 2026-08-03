# P3.1 — Design Review Gate (Pre-Implementation)

## Status

- **Round**: 1 (initial draft).
- **Verdict**: implementation complete (docs only).
- **Code written**: 0
- **Tests added**: 0
- **Branch**: `codex/hermes-p31-design`

## Deliverables

- `docs/requirements/P3-1-design-gate.md` — design review
  package. 6 new decision items (D11..D16) in 5-part contract
  form. 3 inherited from P2.5 (D1 / D3 / D4) carried forward
  with notes that P3.1 review will record option choices.
- `docs/requirements/DECISIONS.md` — append-only log updated
  with P3.1 section + time line.
- `docs/requirements/P3-llm-evaluation.md` — updated Status
  block to reference P3.1 design gate.

## Decision Items Summary

| ID | Contract | Status |
|----|----------|--------|
| D11 | Schema from P0.2 + `additionalProperties: false` + JSON pointer errors | contract locked |
| D12 | 5-stage validation (schema → catalog → action_code → target_ref → DAG) | contract locked |
| D13 | Replan on stale/unexpected; bounded; `replan_budget_exhausted` at limit | contract locked |
| D14 | High-risk confirmation; deterministic gate; LLM cannot bypass | contract locked |
| D15 | Per-transport encoding; renderer-generated un-escaped | contract locked |
| D16 | Plan events through `apply_text`; trace chain | contract locked |
| D1 (inherited) | Id format | option at P3.1 review |
| D3 (inherited) | screenshot_bytes contract | option at P3.1 review |
| D4 (inherited) | Transport threshold | option at P3.1 review |

All 6 P3.1-specific contracts locked. 3 inherited contracts
carry forward from P2.5.

## Acceptance Criteria (reviewer-side)

1. Each D item has the 5-part structure.
2. Contract vs implementation is separated.
3. D11..D16 cover the P3.1 spec surfaces that were deferred.
4. D1 / D3 / D4 inherited unchanged with P3.1 review notes appended.
5. No D item re-implements a P0..P2.x contract without a migration note.

P3.1 design gate closes when all 5 criteria hold AND reviewer signs off.

## Out of Scope

- Any new code under `agent/`, `target/`, or test directories.
- LLM-provider SDK choice.
- Catalog bumps or signature changes.
- Live E2E harness work.
- P3.1 implementation of any kind.

## Reviewer Stop Point

After sign-off, P3.1 may begin. P3.1 implementation produces
its own review package that records option choices for D1 /
D3 / D4.