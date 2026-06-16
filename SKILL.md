---
name: edr-wd
description: Use this skill when working on EDR-WD, a cross-platform MCP GUI automation system for HiSecEndpoint/EDR targets. Use it to inspect, modify, deploy, or test Windows/macOS agent-target workflows, target lifecycle scripts, FastMCP tools, GUI automation backends, HiSec window-pair E2E behavior, and component-tree UI actions.
metadata:
  short-description: Work on EDR-WD MCP GUI automation
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
3. Select one unique node using `automation_id`, `control_id`, or
   `text + class_name + control_type`.
4. Use `click()` and require a semantic component result where available
   (`uia_invoke` / `uia_toggle` on Windows, AX action on macOS).
5. Re-run `dump_tree()` and verify the resulting page text.

Do not start with `click_at`, `click_window_at`, `click_target`, or a bare
text-only click when a component-tree selector is available. Read
`references/element-click.md` before implementing or debugging click behavior.

### Target Scripts

Target runtime scripts belong under `target/scripts/`. Root `scripts/` is for
developer utilities only.

### Deployment Preflight

Do not start by deploying or restarting MCP. Before `deploy_target()`,
`ensure_running()`, or `TargetSubAgent.ensure_running()`, verify:

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

## Agent Workflow

Prefer target-scoped subagents for orchestration:

```python
from agent.subagent import TargetSubAgent

agent = TargetSubAgent.from_name("win-dev")
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
python test_case/run_tests.py --target win-dev
python test_case/run_tests.py --target mac-dev
python3 -m pytest --collect-only -q test_case/test_integration test_case/test_e2e
```

Read `references/testing.md` before changing test profile dispatch, adding E2E
cases, or interpreting live target failures.

## References

Load these only when relevant:

- `references/activate-edr.md`: Windows/macOS HiSec activation internals.
- `references/element-click.md`: component-tree click SOP for Windows UIA and
  macOS AX, including the HiSec "安全中心" compliance template.
- `references/window-detection.md`: window verification and debugging workflow.
- `references/target-config.md`: runtime target schema, auth, and connect modes.
- `references/agent-workflow.md`: subagent, lifecycle, deployment, and wrappers.
- `references/mcp-tools.md`: MCP tool categories and backend capability rules.
- `references/testing.md`: profile-aware test commands and failure triage.
- `docs/architecture/`: historical architecture notes and deeper context.

## Housekeeping

Generated files are not project structure. Remove local artifacts such as
`.venv/`, `__pycache__/`, `.pytest_cache/`, `target/logs/`, root `*.log`,
`target/server.log`, and `.DS_Store` before packaging or publishing.
