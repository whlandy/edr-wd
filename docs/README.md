# EDR-WD Documentation Map

Use this page to choose the right document. Keep docs current and delete
completed design plans after their rules are represented in code, tests,
`SKILL.md`, `references/`, or `sops/`.

## Current Architecture

- [`architecture/00-overview.md`](architecture/00-overview.md): current
  cross-platform agent/target architecture, connection modes, backend split,
  and HiSec window-pair contract.
- [`architecture/01-action-trace-test-report-design.md`](architecture/01-action-trace-test-report-design.md):
  detailed implementation contract for versioned action IDs, atomic test
  execution, chained traces, screenshots, recovery, and Markdown reports.

## Todo Designs

- [`todo/target-sync-without-ad-hoc-scripts.md`](todo/target-sync-without-ad-hoc-scripts.md):
  lifecycle cleanup record for making normal connect/test/SOP flows avoid
  implicit target-side script uploads after the tracked `target/` payload is
  deployed.
- [`todo/llm-action-id-sequences.md`](todo/llm-action-id-sequences.md):
  versioned action IDs, observation-scoped target references, and validated LLM
  action-sequence planning/execution, including chained test traces, screenshot
  evidence, and generated Markdown test reports. Contains the Phase 1-7 →
  P0.1-P3.2 checkpoint mapping.

## Requirements (P-level Review Packages)

Per-level implementation requirements derived from architecture §24. Each
document is the reviewable package for the checkpoints inside that level
and must be approved before any code lands.

- [`requirements/P0-protocol-foundation.md`](requirements/P0-protocol-foundation.md):
  P0.1 Canonical Action Catalog, P0.2 Wire Models And Validation, P0.3
  Observation And Target Identity.
- [`requirements/P1-execution-evidence-mvp.md`](requirements/P1-execution-evidence-mvp.md):
  P1.1 Single-Action Dispatcher And Idempotency, P1.2 Atomic Test Executor,
  P1.3 Append-Only Trace Core, P1.4 Screenshot Evidence And Case Trace
  (MVP milestone).
- [`requirements/P2-recovery-production.md`](requirements/P2-recovery-production.md):
  P2.1 Transition Detection And Checkpoint Policy, P2.2 Recovery Branches,
  P2.3 Run-Level Report And Reruns, P2.4 Production Hardening
  (production-ready milestone).
- [`requirements/P3-llm-evaluation.md`](requirements/P3-llm-evaluation.md):
  P3.1 Structured LLM Planner, P3.2 Evaluation And Metrics (closes the
  design).

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
