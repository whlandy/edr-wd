# EDR-WD Documentation Map

Use this page to choose the right document. Keep docs current and delete
completed design plans after their rules are represented in code, tests,
`SKILL.md`, `references/`, or `sops/`.

## Current Architecture

- [`architecture/00-overview.md`](architecture/00-overview.md): current
  cross-platform agent/target architecture, connection modes, backend split,
  and HiSec window-pair contract.

## Todo Designs

- [`todo/target-sync-without-ad-hoc-scripts.md`](todo/target-sync-without-ad-hoc-scripts.md):
  lifecycle cleanup record for making normal connect/test/SOP flows avoid
  implicit target-side script uploads after the tracked `target/` payload is
  deployed.

## Packaging

- [`../packaging/README.md`](../packaging/README.md): packaging directory
  contract for future `mcp.exe` / PyInstaller work.
- [`../packaging/DESIGN.md`](../packaging/DESIGN.md): review and migration
  design for preserving packaging/PyInstaller work while preventing relay,
  auto-ensure, or generated workflow scripts from weakening the target-control
  contract.

## Operational References

- [`../references/activate-edr.md`](../references/activate-edr.md): Windows and
  macOS HiSec activation internals.
- [`../references/agent-workflow.md`](../references/agent-workflow.md):
  deployment, lifecycle, subagent, tunnel, and dependency preflight.
- [`../references/target-config.md`](../references/target-config.md): target
  naming, config schema, credentials, and connect modes.
- [`../references/mcp-tools.md`](../references/mcp-tools.md): MCP tool
  categories and backend capability boundaries.
- [`../references/testing.md`](../references/testing.md): test dispatch,
  pytest/E2E naming, and failure triage.
- [`../references/element-click.md`](../references/element-click.md):
  component-tree click rules.
- [`../references/window-detection.md`](../references/window-detection.md):
  window verification workflow.

## Documentation Assets

- [`assets/diagrams/`](assets/diagrams/): curated diagrams that are useful for
  reports or architecture explanations.

Local render outputs, scratch PPT exports, screenshots, and generated reports
belong in ignored `artifacts/` or `outputs/`, not in the repository root.

## SOPs

- [`../sops/INDEX.md`](../sops/INDEX.md): SOP catalog and design contract.
- [`../sops/TEMPLATE.md`](../sops/TEMPLATE.md): template for new fixed EDR
  operation sequences.

## Cleanup Rule

Delete documents that only describe an already-completed implementation phase,
one-off optimization plan, or obsolete test result. Preserve durable behavior in
the references above instead.
