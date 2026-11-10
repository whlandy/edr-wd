# EDR-WD Documentation Map

This page is for maintainers. Operators should start with `SKILL.md` and load
one reference for the task at hand.

## Daily Use

| Need | Source of truth |
|---|---|
| Create or edit a target | [`../references/target-config.md`](../references/target-config.md) |
| Deploy, start, stop, restart, repair | [`../references/agent-workflow.md`](../references/agent-workflow.md) |
| Use or add actions/MCP tools | [`../references/mcp-tools.md`](../references/mcp-tools.md) |
| Transfer files through MCP | [`file-transfer.md`](file-transfer.md) |
| Select and click controls | [`../references/element-click.md`](../references/element-click.md) |
| Debug HiSec activation | [`../references/activate-edr.md`](../references/activate-edr.md) |
| Run tests and inspect reports | [`../references/testing.md`](../references/testing.md) |
| Record flows and replay golden traces | [`../references/recording.md`](../references/recording.md) |
| Add a fixed verification flow | [`../sops/INDEX.md`](../sops/INDEX.md) |

Configuration assets:

- [`../config/targets.example.json`](../config/targets.example.json): safe example.
- [`../config/targets.schema.json`](../config/targets.schema.json): editor schema.
- `config/targets.local.json`: ignored runtime config containing real values.

## Current Architecture

- [`architecture/00-overview.md`](architecture/00-overview.md): agent/target,
  platform, lifecycle, and backend boundaries.
- [`architecture/01-action-trace-test-report-design.md`](architecture/01-action-trace-test-report-design.md):
  action IDs, observations, execution, trace, recovery, planner, and reports.
- [`architecture/03-desktop-record-script-golden-replay-design.md`](architecture/03-desktop-record-script-golden-replay-design.md):
  manual desktop action capture, executable test generation, and semantic/visual golden replay.
- [`architecture/09-cross-window-sequence-and-instance-binding-design.md`](architecture/09-cross-window-sequence-and-instance-binding-design.md):
  instance/window separation, causal window transitions, and per-run binding.
- [`architecture/10-cross-window-compiler-disambiguation-r0.md`](architecture/10-cross-window-compiler-disambiguation-r0.md):
  same-title window disambiguation and ready-gate semantics.
- [`architecture/11-recording-basic-actions-usability-and-integrity-design.md`](architecture/11-recording-basic-actions-usability-and-integrity-design.md):
  language menus/multi-window identity, capture completeness rules (C01–C13),
  menu-layout structural locators, layout-drag classification.
- [`architecture/12-trace-self-correction-and-agent-rerecord-design.md`](architecture/12-trace-self-correction-and-agent-rerecord-design.md):
  failed-trajectory recovery: evidence-constrained repair, full operation-point
  step plans, agent re-record with record-before-act, budgeted conclusions.
- [`todo/desktop-recording-golden-replay.md`](todo/desktop-recording-golden-replay.md):
  phased implementation and verification tracker for desktop recording and replay.

## In-Progress Architecture

- [`architecture/04-product-adapter-window-hybrid-locator-design.md`](architecture/04-product-adapter-window-hybrid-locator-design.md):
  product adapters, window identity/lifecycle, hybrid localization, and intent-driven PC GUI execution (implementation and acceptance tracker).

Implementation ownership:

```text
agent/execution/       execute and recover
agent/trace/           persist events/evidence and render projections/reports
agent/recording/       compile, repair, step-plan, re-record loop, ledger
agent/planner/         create and validate structured plans
agent/eval/            evaluate datasets, metrics, thresholds, and CI gates
target/action_catalog/ define stable actions
target/action_dispatcher/ validate and dispatch target actions
target/observations/   snapshot and resolve observation-local targets
target/automation/     platform-native GUI implementations
target/windowing/      instance tracking, binding, effects, lifecycle
target/recording/      capture sources, correlator, evidence, result store
```

## Historical Records

Files under `docs/requirements/` and `docs/todo/` are implementation and review
records. They are not operating instructions. Consult them only when auditing a
protocol decision or unfinished acceptance item.

- `docs/requirements/`: P0-P3 contracts, decisions, and design gates.
- `docs/todo/`: historical implementation plans and remaining acceptance.
- [`todo/scroll-and-paged-table-actions.md`](todo/scroll-and-paged-table-actions.md):
  scroll, drag, paginated-table, RDP fallback, and verification design notes.
- [`archive/changelogs/`](archive/changelogs/): module implementation history.
- [`archive/reports/`](archive/reports/): one-time integration reports.

New current behavior belongs in code, tests, `SKILL.md`, `references/`, or
`sops/`. Do not add new module-level changelogs or completed phase plans to the
daily-use navigation.
