# EDR-WD Documentation Map

This page is for maintainers. Operators should start with `SKILL.md` and load
one reference for the task at hand.

## Daily Use

| Need | Source of truth |
|---|---|
| Create or edit a target | [`../references/target-config.md`](../references/target-config.md) |
| Deploy, start, stop, restart, repair | [`../references/agent-workflow.md`](../references/agent-workflow.md) |
| Use or add actions/MCP tools | [`../references/mcp-tools.md`](../references/mcp-tools.md) |
| Select and click controls | [`../references/element-click.md`](../references/element-click.md) |
| Debug HiSec activation | [`../references/activate-edr.md`](../references/activate-edr.md) |
| Run tests and inspect reports | [`../references/testing.md`](../references/testing.md) |
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

Implementation ownership:

```text
agent/execution/       execute and recover
agent/trace/           persist events/evidence and render projections/reports
agent/planner/         create and validate structured plans
agent/eval/            evaluate datasets, metrics, thresholds, and CI gates
target/action_catalog/ define stable actions
target/action_dispatcher/ validate and dispatch target actions
target/observations/   snapshot and resolve observation-local targets
target/automation/     platform-native GUI implementations
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
