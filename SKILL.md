---
name: edr-wd
description: |
  Cross-platform GUI automation for EDR/HiSecEndpoint via MCP. The agent and
  target both support Windows and macOS. The agent orchestrates lifecycle,
  SSH/SCP, and MCP sessions; the target hosts the FastMCP server inside the
  target OS's interactive desktop session and exposes GUI / PowerShell tools.

  Typical use:
  1. Define a target in config/targets.local.json
  2. Start the target server from the agent side
  3. Initialize MCP
  4. Drive the GUI and PowerShell
  5. Stop the server when done
---

# EDR-WD

EDR-WD is split into two orthogonal layers:

- Agent side: configuration, lifecycle, SSH/SCP, MCP session management
- Target side: the actual FastMCP server, GUI backend, and OS-specific launch
  strategy

The agent OS can be Windows or macOS. The target OS can be Windows or macOS.
The same Python orchestration APIs are used on both agent platforms; the shell
wrapper `agent/edr-wd.sh` is just a POSIX convenience entrypoint.

## Repository Layout

Use these directories by responsibility:

- `agent/`: agent-side orchestration only. It loads target config, handles
  SSH/SFTP, selects lifecycle backends, and initializes MCP sessions.
- `agent/subagent/`: one target-scoped control object per target. Each
  `TargetSubAgent` owns that target's lifecycle calls, MCP URL/session,
  backend status, and profile/backend validation.
- `agent/lifecycle/`: target lifecycle adapters. These call target scripts
  remotely; they should not contain GUI automation logic.
- `target/`: files copied to or run on the target machine. `target/server.py`
  is the FastMCP server.
- `target/automation/`: GUI automation backend implementations. Target OS
  decides which backend is active.
- `target/scripts/`: target-local startup, stop, health, and installer scripts.
- `test_case/`: profile-dispatched test entrypoints and pytest coverage.
- `scripts/`: developer utilities that are not runtime entrypoints. Do not
  place target runtime scripts here; macOS/Windows target scripts belong under
  `target/scripts/`.
- `docs/architecture/` and `references/`: architecture notes and operation
  references.

Generated files and scratch copies must stay out of Git: `.venv/`,
`__pycache__/`, `.pytest_cache/`, `*.log`, `target/logs/`, `target/server.log`,
`.DS_Store`, local target configs, and copied runtime scripts under
`scripts/macos/`.

## Architecture

```mermaid
flowchart LR
  Agent[Agent host\nWindows or macOS] --> TC[agent.target_config]
  Agent --> TM[agent.target_manager]
  Agent --> MM[agent.mcp_manager]
  Agent --> SSH[agent.ssh_runner]
  TM --> WL[agent.lifecycle.windows]
  TM --> ML[agent.lifecycle.macos]
  WL --> W1[target/deploy.ps1\nWindows target]
  ML --> M1[target/scripts/macos/*\nmacOS target]
  W1 --> S[target/server.py\nFastMCP]
  M1 --> S
  S --> WB[automation.windows_pywinauto]
  S --> MB[automation.macos_accessibility]
  S --> GUI[HiSecEndpoint / EDRClient]
```

## Configuration

Runtime targets live in `config/targets.local.json` and are loaded by
`agent.target_config.TargetConfig`.

For the current intranet SSH workflow, put the target username and password
directly in the target's `ssh` block:

```json
"ssh": {
  "host": "<TARGET_IP>",
  "port": 22,
  "user": "<TARGET_USER>",
  "auth": {
    "type": "password",
    "password": "<TARGET_PASSWORD>"
  }
}
```

Password auth is the preferred path for both Windows and macOS targets.
`password_env` and key auth remain compatibility options, but they are not
required for the current workflow. TODO: revisit secret storage when this moves
outside the trusted intranet setup. Never commit `config/targets.local.json` or
paste real IPs, usernames, passwords, or target paths in logs, commits, PRs, or
chat.

Key fields:

- `platform`: `windows` or `macos`
- `app_profile`: optional, used by the test dispatcher
- `ssh`: host/user/auth
- `mcp`: host/port/path/connect_mode/tunnel
- `windows` or `macos`: platform-specific launch config

Useful commands:

```bash
python -m agent.target_config --init
python -m agent.target_config --validate
python -m agent.target_config --list
python -m agent.target_config --guide
```

## Agent Side Workflow

The agent-side control flow is always the same, regardless of the agent OS:

1. `TargetSubAgent.from_name(name)` creates the per-target controller.
2. `TargetSubAgent.ensure_running()` checks TCP reachability and
   starts the target server through the platform lifecycle backend if needed.
3. `TargetSubAgent.initialize_mcp()` performs MCP initialize and stores a
   session id plus the MCP URL.
4. `TargetSubAgent.call_tool(...)` sends tool calls over that target's owned
   session and can reinitialize once when the session is stale.

The lower-level `target_manager` and `mcp_manager` modules remain available,
but new orchestration should prefer `agent.subagent.TargetSubAgent` or
`TargetSubAgentPool` so sessions and health state are isolated per target.

Example:

```python
from agent.subagent import TargetSubAgent

agent = TargetSubAgent.from_name("win-dev")
agent.ensure_running()
agent.initialize_mcp()
print(agent.call_tool("status"))
```

### Convenience wrapper

On macOS/Linux agents you can use the bundled wrapper:

```bash
bash agent/edr-wd.sh up
bash agent/edr-wd.sh status
bash agent/edr-wd.sh smoke --gui
bash agent/edr-wd.sh down
```

`agent/edr-wd.sh` is a thin shell wrapper around the same Python APIs. On
Windows agents, use [agent/deploy.ps1](agent/deploy.ps1) for the same control
plane, including config guidance, deploy/install, `up/down/status`, `push`,
and `smoke`.

## Target Lifecycle

### Windows targets

Windows lifecycle is handled by `agent/lifecycle/windows.py` and the scripts in
`target/`:

- [target/deploy.ps1](target/deploy.ps1) is the operator-facing lifecycle entrypoint
- `target/scripts/install_task.ps1` registers the scheduled task
- `target/scripts/start_server.ps1` starts the server in the interactive
  desktop session
- `target/scripts/stop_server.ps1` stops the listener on port 8765
- `target/scripts/health.ps1` performs a quick port-level health check

Typical Windows flow — all via `agent/lifecycle/windows.py` through
`target_manager.ensure_server_running()` and `target_manager.stop_server()`.

### macOS targets

macOS lifecycle is handled by `agent/lifecycle/macos.py` and the scripts under
`target/scripts/macos/`:

- `install_launch_agent.sh` registers a LaunchAgent
- `start_server.sh` starts the FastMCP server in the GUI session
- `stop_server.sh` stops the server by port/pidfile
- `com.edr-wd.target.plist.template` is rendered during install

- **Element-level click on macOS**: `dump_tree` and `click` (by control_id) are
  Windows-only. On macOS, write a Swift script that uses the AX API
  (`AXUIElement`, `AXPressAction`, CGEvent) and execute it over SSH. See
  [references/element-click.md](references/element-click.md) for the complete
  pattern — including how to find elements by title/role, AXPress vs CGEvent,
  and a ready-to-use script template.

- `dump_tree` on Windows uses pywinauto/UIA; on macOS it returns normalized
  controls with `role`, `title`, `description`, `value`, `identifier`,
  `rectangle`, and `control_id` (requires Accessibility permission).
- `find_control` works on both platforms. Windows filters the UIA dump; macOS
  filters the Accessibility tree.
- `click` / `click_target` work on macOS for common selectors by resolving a
  matched AX node and falling back to its rectangle center.
- `lock_window` pins subsequent click/drag/scroll actions to the connected or
  specified target window. This follows the same safety principle as maa-fw's
  window locator: before a pointer action, confirm the foreground window still
  matches the intended target, try one activation if needed, and fail closed if
  the active window is different.
- `unlock_window`, `get_window_lock`, and `verify_window_lock` manage and
  inspect that lock. Recommended GUI flow is `connect` -> `lock_window` ->
  `dump_tree/find_control` -> `click/click_target/click_at` -> `unlock_window`.
- macOS uses Accessibility/System Events plus app/window detection
- macOS `click_at` is dry-run by default; set
  `EDR_WD_ALLOW_REAL_CLICKS=1` on the target server before relying on it for
  real UI interaction.
- The shared action space now includes `double_click_at`, `right_click_at`,
  `middle_click_at`, `hover_at`, `drag`, and `scroll` for drag-and-drop,
  context menus, sliders, and hover-triggered flows.
- `status.action_space` reports which action primitives are actually supported
  by the loaded backend. Use it in tests before assuming a selector or gesture
  exists on a platform.
- `activate_edr` on macOS targets the `EDRClient` application window. It first
  tries `/Applications/HiSecEndpoint.app/Contents/script/root_start_client.sh`
  via non-interactive sudo and only accepts success when an `EDRClient` window
  is detected. If sudo/script startup fails, it first tries `open
  /Applications/HiSecEndpoint.app` to bring the app bundle into the GUI
  session, then falls back to `HiSecEndpointAgent cmd ui` without redirecting
  stdout/stderr. In both fallback paths it explicitly brings the HiSec entry
  window to the foreground before using the Swift Accessibility helper to click
  "前往安全防护中心". The click helper is only meaningful after the HiSec
  entry window is active; do not assume a visible-but-background window is
  clickable.

## MCP Server

`target/server.py` is the cross-platform FastMCP server. The backend is chosen
by `EDR_WD_AUTOMATION_BACKEND`:

- `windows_pywinauto`
- `macos_accessibility`

Primary tools:

- GUI: `connect`, `lock_window`, `unlock_window`, `get_window_lock`,
  `verify_window_lock`, `dump_tree`, `find_control`, `click`, `click_target`, `click_at`,
  `click_window_at`, `double_click_at`, `right_click_at`, `middle_click_at`,
  `hover_at`, `drag`, `scroll`, `type_text`, `select`, `get_text`,
  `screenshot`, `restore_edr`
- Window/app: `activate_app`, `list_windows`, `is_window_open`, `wait_window`,
  `activate_edr`, `status`
- PowerShell: `run_powershell`, `start_powershell`, `get_job`, `cancel_job`
- Debug: `diagnose_windows`

PowerShell tools require `EDR_WD_ENABLE_POWERSHELL=1` on the target server.

## Testing

Smoke test the live server:

```bash
python target/tests/smoke_mcp_client.py --base-url http://127.0.0.1:8765/mcp
python target/tests/smoke_mcp_client.py --base-url http://127.0.0.1:8765/mcp --gui
```

The smoke client is backend-aware:
- Windows backends exercise `run_powershell`, async jobs, `connect`, and `dump_tree`
- macOS backends exercise `list_windows`, `activate_app`, and Finder-based GUI plumbing
- `status` is backend-aware; macOS-only window diagnostics stay on the macOS backend
- `status.backend_kind` is the stable backend selector (`windows_pywinauto` / `macos_accessibility`)
- `status.host` / `status.port` report the actual runtime bind address, not a hard-coded 8765
- `restore_edr` is connect-required and returns a structured window payload when the
  backend exposes a connected app instance; the regression tests now verify the
  returned `rectangle` fields as part of the workflow
- macOS HiSec profile treats `screenshot` as a permission-gated diagnostic: a
  successful file-only capture without inline image payload is skipped rather
  than failed, and the final post-restore visibility check is diagnostic-only
  because `restore_edr` is non-destructive on macOS
- Windows GUI smoke/E2E coverage calls `activate_edr` to make both windows
  visible. The Windows backend opens `HisecEndpointAgent.exe cmd ui`, then
  prefers `EDRClient.exe 17 --show` and only falls back to clicking
  `edrWidget` when the direct client launch does not produce the window. The
  same window-pair E2E is part of the basic pytest integration suite in
  `test_case/test_integration/test_edr_window_pair_e2e.py`.

Full profile-dispatched tests:

```bash
python test_case/run_tests.py --target win-dev
python test_case/run_tests.py --target mac-dev
```

If you need low-level config validation:

```bash
python -m agent.target_config --validate
python -m agent.target_config --list
python -m agent.target_config --guide
```

## Important Notes

- The target server must run in a logged-on interactive desktop session.
- A pure SSH background session is not sufficient for GUI automation.
- `target_manager.ensure_server_running()` only guarantees TCP readiness;
  `mcp_manager.initialize()` is what makes the MCP session usable.
- The agent OS is orthogonal to the target OS: the same Python orchestration
  modules work on Windows and macOS agents.
- Protocol version for the current agent/test clients is `2025-03-26`.

## Useful Files

Primary agent entrypoints:

- [agent/deploy.ps1](agent/deploy.ps1): Windows-shell control plane.
- [agent/edr-wd.sh](agent/edr-wd.sh): POSIX convenience wrapper.
- [agent/target_config.py](agent/target_config.py): config validation,
  initialization, and target lookup.
- [agent/target_manager.py](agent/target_manager.py): deploy/install/start/stop
  orchestration.
- [agent/mcp_manager.py](agent/mcp_manager.py): MCP initialize and tool calls.

Lifecycle and transport:

- [agent/lifecycle/base.py](agent/lifecycle/base.py): lifecycle backend contract.
- [agent/lifecycle/windows.py](agent/lifecycle/windows.py): Windows target lifecycle.
- [agent/lifecycle/macos.py](agent/lifecycle/macos.py): macOS target lifecycle.
- [agent/ssh_runner.py](agent/ssh_runner.py): SSH/SFTP execution abstraction.

Target runtime:

- [target/server.py](target/server.py): cross-platform FastMCP server.
- [target/deploy.ps1](target/deploy.ps1): target-side Windows lifecycle wrapper.
- [target/automation/base.py](target/automation/base.py): automation backend contract.
- [target/automation/windows_pywinauto.py](target/automation/windows_pywinauto.py):
  Windows UIA backend.
- [target/automation/macos_accessibility.py](target/automation/macos_accessibility.py):
  macOS Accessibility backend.
- [target/scripts/](target/scripts): target-local start/stop/health/install scripts.

Testing and diagnostics:

- [target/tests/smoke_mcp_client.py](target/tests/smoke_mcp_client.py): live MCP smoke client.
- [test_case/run_tests.py](test_case/run_tests.py): profile-dispatched test runner.
- [test_case/run_windows_hisec.py](test_case/run_windows_hisec.py): Windows HiSec flow.
- [test_case/run_macos_generic.py](test_case/run_macos_generic.py): macOS generic flow.
- [scripts/redact_config.py](scripts/redact_config.py): safe local config inspection.

References:

- [references/element-click.md](references/element-click.md): macOS element-level
  click via Swift + AX API — AXPress, CGEvent fallback, AX tree traversal.
- [references/activate-edr.md](references/activate-edr.md): HiSecEndpoint activation
  internals on both Windows and macOS.

Compatibility helpers:

- [agent/tunnel.sh](agent/tunnel.sh): legacy/manual SSH tunnel helper.
- [agent/setup-mac.sh](agent/setup-mac.sh): one-off macOS SSH config helper.

Avoid treating generated files as project structure. If you see `.venv/`,
`__pycache__/`, `.pytest_cache/`, `target/logs/`, root `*.log`, or `.DS_Store`,
delete them locally; they are runtime artifacts.

## Troubleshooting

- Empty `dump_tree` usually means the server was not started in an interactive
  desktop session.
- If `mcp_manager.initialize()` fails, check the MCP URL, port forwarding, and
  protocol version.
- On macOS, Accessibility / Screen Recording permissions are required for
  backend features that enumerate or capture windows.
- On Windows, `EDR_WD_ENABLE_POWERSHELL=1` must be set for PowerShell tools
  and HiSec activation.
- On Windows, use `agent/deploy.ps1 -Action config-guide` to generate a concise setup walkthrough.
