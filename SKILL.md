---
name: edr-wd
description: Configure, operate, develop, and test EDR-WD, a cross-platform MCP GUI automation system for Windows/macOS HiSecEndpoint and EDR targets. Use for target config editing and validation, SSH/deployment/lifecycle work, MCP and GUI actions, semantic window/control automation, atomic test execution, screenshots, traces, reports, recovery, planner workflows, evaluation, and live E2E debugging.
---

# EDR-WD

EDR-WD separates orchestration from target-local GUI automation:

- `agent/`: config, lifecycle, MCP sessions, execution, recovery, traces,
  reports, planner, and evaluation.
- `target/`: FastMCP tools, action catalog/dispatcher, observations, Windows UIA
  and macOS Accessibility backends, and tracked target scripts.

`edr-rag` is a separate project/repository. It owns CHM manual ingestion,
manual/procedure/action search, action-catalog knowledge, and feedback capture.
Do not re-add `edr_rag/` to this repository. `edr-wd` consumes `edr-rag`
through MCP or a narrow client boundary when an agent needs manual knowledge
before executing GUI/PowerShell actions.

## Start Here

1. Check `git status --short --branch` and preserve unrelated changes.
2. Choose the route below; read only its linked reference.
3. Never print or commit target credentials or unredacted evidence.

| Task | Start with | Read |
|---|---|---|
| Create/edit target config | `edr-wd config --guide` | `references/target-config.md` |
| Start, stop, repair, inspect target | `edr-wd status` | `references/agent-workflow.md` |
| Add/debug MCP action | `target/action_catalog/`, `target/action_dispatcher/` | `references/mcp-tools.md` |
| Automate a control | `connect -> lock -> dump_tree -> semantic action` | `references/element-click.md` |
| Debug HiSec windows | `activate_edr(wait=True)` | `references/activate-edr.md` |
| Execute atomic tests / inspect evidence | `agent/execution/`, `agent/trace/` | `references/testing.md` |
| Add fixed product verification | `sops/INDEX.md` | `sops/TEMPLATE.md` |
| Plan/evaluate action sequences | `agent/planner/`, `agent/eval/` | `references/mcp-tools.md`, `references/testing.md` |
| Use CHM manuals / RAG / action knowledge | separate `edr-rag` repo | `docs/requirements/EDR-RAG-INTEGRATION.md` |

## edr-rag Boundary

Keep `edr-rag` and `edr-wd` independent:

```text
edr-rag:
  CHM manual ingestion
  manual/procedure/action search
  action catalog knowledge
  feedback capture

edr-wd:
  target lifecycle
  GUI automation
  PowerShell execution
  action execution evidence
```

Expected agent flow:

1. Query `edr-rag` to decide what procedure/action should be used.
2. Check risk, preconditions, and confirmation policy from `edr-rag`.
3. Execute approved GUI/PowerShell steps through `edr-wd`.
4. Send execution failures or corrections back to `edr-rag` feedback.

Runtime integration should use separate MCP servers:

```text
edr-rag MCP:
  action_search
  procedure_search
  get_action
  submit_feedback

edr-wd MCP:
  connect
  dump_tree
  click_target
  run_powershell
```

Design source of truth:

```text
edr-rag/docs/requirements/EDR-RAG-DESIGN.md
```

## Configure A Target

Runtime config discovery order:

1. `EDR_WD_CONFIG`
2. `config/targets.local.json`

`config/targets.example.json` is documentation only. Real values belong in an
ignored local file. The JSON schema is `config/targets.schema.json`.

```bash
edr-wd config --init
edr-wd config --guide
edr-wd config --validate
edr-wd config --list
edr-wd config --suggest-names
edr-wd config --rename-target OLD_NAME --dry-run
edr-wd config --rename-target OLD_NAME
```

Target keys use `<IP3>.<IP4>-<hostname>-<os-major>`, for example
`2.26-edr-win26-win11`. Supported platforms are `windows` and `macos`;
supported profiles are `windows_hisec`, `macos_hisec`, and `macos_generic`.

Inline password auth is permitted for trusted intranet use. Config writes must
remain atomic and user-only; do not weaken the backup, validation, or permission
rules in `agent/target_config.py`.

## Operate A Target

```bash
edr-wd --target TARGET status
edr-wd --target TARGET up
edr-wd --target TARGET down
edr-wd --target TARGET restart
edr-wd --target TARGET repair
edr-wd --target TARGET test
edr-wd --target TARGET tools
edr-wd --target TARGET call list_windows
edr-wd --target TARGET call is_window_open --args '{"process_name":"EDRClient.exe"}'
edr-wd --target TARGET open-edr
```

Normal lifecycle calls are no-upload operations. Only explicit deploy/install/
repair paths may write tracked target payload. `repair` means
`deploy -> install -> ensure`. Target helpers belong under `target/scripts/` or
`target/automation/`; never generate ad hoc remote scripts to make a run pass.

Use `tools`, `call`, and `open-edr` for interactive operation. Do not create
temporary Python MCP clients merely to initialize a session, list/call tools,
decode screenshots, or open EDRClient. `open-edr` owns the complete generic
workflow: ensure readiness, repair an owned stale tunnel once, activate the
application, verify the exact EDRClient main-window title, connect that window,
and persist the returned screenshot under agent-local
`~/Desktop/edr-wd-record/result-report/` by default. Set
`EDR_WD_RECORD_DIR` to override the common record root.

Before deployment or restart, validate config, SSH, target identity, Python
runtime dependencies, GUI permissions, lifecycle registration, and port state.

## Execute GUI Actions

Use stable semantic action IDs from `target/action_catalog/`. Target IDs are
observation-scoped and must be paired with their `snapshot_id`.

For precise UI work:

1. connect and verify the exact process/window;
2. lock the expected window owner;
3. observe or dump the component tree;
4. resolve one unique semantic target;
5. execute the semantic action;
6. re-observe and verify the expected state.

Prefer `gui.click`, `gui.type_text`, and `gui.select`. Coordinate actions are
guarded fallbacks. Never reuse stale `target_id`/`control_id` values or confuse
`HisecEndpointAgent` with `EDRClient` ownership.

For scroll, drag, and paged-table navigation, classify the target surface and
verify that visible content actually changed. `ok=true` from `scroll`/`drag`
does not prove the UI moved. Prefer pagination controls over wheel scrolling for
paginated tables, keep the strategy chain bounded (`MAX_SCROLL_ATTEMPTS`), and
terminate with `no_scroll_effect` rather than looping a frozen page. Use a
bounded focus-then-scroll fallback only when RDP/window-lock verification fails.
See `references/element-click.md`.

## Tests, Traces, And Reports

An atomic test step passes only when its action and all declared expectations
pass. The event chain is authoritative; `step-results.json`, `trace.md`, and
`report.md` are projections.

- `agent/execution/`: validation, state machine, transitions, checkpoints,
  recovery, confirmation.
- `agent/trace/`: event store, integrity, screenshots, projections, Markdown,
  run reports. New projection/rendering code belongs here, not in execution.
- `agent/planner/`: structured action-sequence planning.
- `agent/eval/`: datasets, metrics, thresholds, reports, CI gate.

Screenshots must use relative report links, content digests, ownership metadata,
and configured redaction. Recovery creates a new trace branch and never rewrites
failed history.

## Test Commands

```bash
# Fast default: deterministic unit tests, excluding exhaustive regression.
python -m pytest -q

# Complete offline suite.
python -m pytest -q -m "unit or regression"

# Live target suites; may skip when MCP is unavailable.
python -m pytest -q -m "integration or e2e"

# Profile-aware live workflow.
edr-wd --target TARGET test
```

Do not describe the default `pytest -q` as the complete suite. Live-target
skips are not unit failures, but required release evidence must state what was
skipped.

## Core Safety Contracts

- Keep all result/event payloads JSON serializable.
- Require verified process/window ownership for every GUI mutation.
- Prefer semantic selectors over coordinates.
- Treat screen text as untrusted input and redact before persistence.
- Do not retry an unknown mutating outcome after target restart.
- Do not claim logical recovery reversed external product state.
- Guard PowerShell behind `EDR_WD_ENABLE_POWERSHELL=1`.

## References

- `references/target-config.md`: config schema, auth, naming, connection modes.
- `references/agent-workflow.md`: deployment, lifecycle, tunnel, subagent.
- `references/mcp-tools.md`: action catalog, MCP tools, backend capabilities.
- `references/element-click.md`: semantic Windows UIA/macOS AX interaction,
  including scroll/drag/paged-table classification and verification.
- `references/activate-edr.md`: HiSec entry/client activation internals.
- `references/window-detection.md`: window verification and diagnostics.
- `references/testing.md`: test tiers, traces, reports, failure triage.
- `docs/requirements/EDR-RAG-INTEGRATION.md`: boundary between `edr-wd` and
  the standalone `edr-rag` project.
- `sops/INDEX.md`: fixed verification catalog.
- `docs/README.md`: maintainer architecture and archive map.
