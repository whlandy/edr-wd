---
name: edr-wd
description: Use this skill when working on EDR-WD, a cross-platform MCP GUI automation system for HiSecEndpoint/EDR targets. Use it to inspect, modify, deploy, or test Windows/macOS agent-target workflows, target lifecycle scripts, FastMCP tools, GUI automation backends, HiSec window-pair E2E behavior, and component-tree UI actions.
---

# EDR-WD

EDR-WD has two layers:

- `agent/`: target config, SSH/SFTP deployment, lifecycle orchestration, and
  per-target MCP sessions.
- `target/`: FastMCP server, GUI automation backends, and target-local scripts.

The agent OS and target OS are independent. A macOS agent can drive Windows or
macOS targets, and a Windows agent can do the same when the target config and
SSH path are valid.

## First Steps

1. Check branch and local changes:

   ```bash
   git status --short --branch
   ```

2. Read only the files needed for the task. Common entry points:

   - `agent/target_config.py`
   - `agent/subagent/`
   - `agent/lifecycle/`
   - `target/server.py`
   - `target/automation/`
   - `test_case/`

3. This skill is for trusted intranet use. Real target IPs, usernames, and
   passwords may be stored in local runtime config, but must not be committed or
   printed in responses.

## Core Contracts

### HiSec Window Pair

`activate_edr(wait=True)` must make both windows visible:

- Entry/main window: `HisecEndpointAgent.exe` on Windows or
  `HiSecEndpointAgent` on macOS.
- Client window: `EDRClient.exe` on Windows or `EDRClient` on macOS.

Read `references/activate-edr.md` before changing activation logic or debugging
window-pair E2E failures.

### Component-Tree Clicks

Precise UI actions must be component-tree driven:

1. Verify and connect to the exact target window/process.
2. `dump_tree(max_depth=...)`.
3. Select one unique node with the platform-native mapping: Windows UIA uses
   `automation_id + control_type/class_name + text`; macOS AX uses
   `identifier + role/subrole + title/description/value`.
4. Use `click(expected_process_name=<connected process>)` and require a
   semantic component result where available
   (`uia_invoke` / `uia_toggle` on Windows, AX action on macOS).
5. Re-run `dump_tree()` and verify the resulting page text.

Do not start with `click_at`, `click_window_at`, `click_target`, or a bare
text-only click when a component-tree selector is available. Read
`references/element-click.md` before implementing or debugging click behavior.
Do not persist `control_id` or reuse native component identifiers across
Windows and macOS.

### Fixed Verification SOPs

When a feature is defined as a fixed sequence of EDR window operations, read
`sops/INDEX.md`, select an existing SOP, and follow its window ownership,
evidence, retry, and failure contracts exactly. Use `sops/TEMPLATE.md` for a new
verification flow. Every SOP must distinguish `HisecEndpointAgent` from
`EDRClient` at each GUI step.

### Target Scripts

Target runtime scripts belong under `target/scripts/`. Root `scripts/` is for
developer utilities only.

### Target File Contract

A new target should become usable by syncing the repository's existing
`target/` tree to the configured target root and starting MCP from that tree.
Do not create ad hoc scripts, config files, test files, or helper programs on
the target to make a workflow pass. If a target-side helper is needed, add it to
the repository under `target/scripts/` or `target/automation/`, review it, and
deploy it as part of `target/`.

Allowed target-side writes are limited to runtime artifacts created by existing
tracked code: `logs/`, PID files, screenshots/artifacts under the configured
artifact directory, LaunchAgent plist installation on macOS, and Windows
scheduled-task registration. Dependency checks must be read-only; they may run
remote Python/PowerShell commands but must not write probe scripts to the
target.

### Deployment Preflight

Do not start by deploying or restarting MCP. Before `deploy_target()`,
`ensure_running()`, or `TargetSubAgent.ensure_running()`, verify:

0. Run the executable dependency preflight when available:
   `python scripts/check_dependencies.py --target <TARGET_NAME> --scope all --platform auto`
   or, from PowerShell, `.\agent\check-deps.ps1 -TargetName <TARGET_NAME>`.
   Use `--platform windows` or `--platform macos` when checking a specific
   target dependency profile.
1. Target config validates and Paramiko SSH login works.
2. Agent Python can import the dependencies declared in `pyproject.toml`,
   especially `paramiko` for SSH/SFTP/tunnel.
3. Target Python path exists and can import all required runtime dependencies
   from `pyproject.toml`, not only `fastmcp`.
4. Backend dependencies are present (`pywinauto`/`pyautogui` on Windows,
   `pyautogui` plus Accessibility/GUI permissions on macOS).
5. When running tests, test-only dependencies from
   `test_case/requirements_test.txt` are installed on the runner.
6. Port `8765` state is known. For `connect_mode=direct`, the target firewall
   must allow inbound TCP `8765` before the agent expects MCP to answer.
7. If an existing FastMCP server is already responding, initialize/status it
   before deciding to redeploy or restart.

Read `references/agent-workflow.md` before changing deployment or lifecycle
flow.

## Target Config

Runtime targets are loaded by `agent.target_config.TargetConfig` from
`EDR_WD_CONFIG` first, then `config/targets.local.json`. For this trusted
intranet workflow, prefer inline username/password auth in the local config.
All SSH command execution and file transfer goes through Paramiko. `password_env`
and key auth are compatibility paths, not the default path.

Read `references/target-config.md` when adding targets, changing
`connect_mode`, or touching auth/platform validation.

Target keys must use `<IP第3段>.<IP第4段>-<hostname>-<os版本>`. Keep only the
major OS version (`win11`, `macos14`), not build numbers or release suffixes.
Example: `2.26-edr-win26-win11` for the documentation-only IP `192.0.2.26`.

## Agent Workflow

Prefer target-scoped subagents for orchestration:

```python
from agent.subagent import TargetSubAgent

agent = TargetSubAgent.from_name("2.26-edr-win26-win11")
agent.ensure_running()
agent.initialize_mcp()
print(agent.call_tool("status"))
```

`TargetSubAgent` owns one target's lifecycle, MCP URL/session, backend status,
and profile/backend validation. Keep lower-level manager modules compatible, but
avoid adding new session state outside the subagent layer.

Read `references/agent-workflow.md` when changing lifecycle, deployment, or
agent-side command flow.

## MCP And GUI Backends

`target/server.py` registers FastMCP tools. Backends are selected with
`EDR_WD_AUTOMATION_BACKEND`:

- `windows_pywinauto`
- `macos_accessibility`

Read `references/mcp-tools.md` when adding tools, debugging tool calls, or
changing backend capabilities.

## Testing

Use profile-aware tests; do not route a macOS target into Windows HiSec tests.

```bash
python test_case/run_tests.py --target 2.26-edr-win26-win11
python test_case/run_tests.py --target 2.29-edr-mac29-macos14
python3 -m pytest --collect-only -q test_case/test_integration test_case/test_e2e
```

Read `references/testing.md` before changing test profile dispatch, adding E2E
cases, or interpreting live target failures.

## References

Load these only when relevant:

- `references/activate-edr.md`: Windows/macOS HiSec activation internals.
- `references/element-click.md`: reusable component-tree click rules for
  Windows UIA and macOS AX.
- `references/window-detection.md`: window verification and debugging workflow.
- `references/target-config.md`: runtime target schema, auth, and connect modes.
- `references/agent-workflow.md`: subagent, lifecycle, deployment, and wrappers.
- `references/mcp-tools.md`: MCP tool categories and backend capability rules.
- `references/testing.md`: profile-aware test commands and failure triage.
- `sops/INDEX.md`: fixed functional-verification SOP design and catalog.
- `sops/TEMPLATE.md`: required template for new EDR operation sequences.
- `docs/README.md`: documentation map and cleanup rules.
- `docs/architecture/00-overview.md`: current architecture overview.

## Housekeeping

Generated files are not project structure. Remove local artifacts such as
`.venv/`, `__pycache__/`, `.pytest_cache/`, `target/logs/`, root `*.log`,
`target/server.log`, and `.DS_Store` before packaging or publishing.
