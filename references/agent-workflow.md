# Agent Workflow Reference

Use this reference when changing agent orchestration, target lifecycle, or
deployment behavior.

## Preferred Entry Point

Use `TargetSubAgent` for new orchestration:

```python
from agent.subagent import TargetSubAgent

agent = TargetSubAgent.from_name("2.26-edr-win26-win11")
agent.ensure_running()
agent.initialize_mcp()
status = agent.call_tool("status")
```

`TargetSubAgent` owns exactly one target:

- lifecycle state
- MCP URL and session id
- backend status
- profile/backend validation
- retry after stale session errors

Avoid adding new global MCP session state outside the subagent layer.

## Manager Responsibilities

- `agent/target_config.py`: config discovery, validation, URL building, auth
  resolution.
- `agent/ssh_runner.py`: Paramiko SSH command execution and SFTP for all
  password/key auth paths.
- `agent/target_manager.py`: deploy, ensure, stop, restart, and health checks.
- `agent/mcp_manager.py`: FastMCP Streamable HTTP initialize and JSON-RPC calls.
- `agent/lifecycle/windows.py`: Windows SSH, task scheduler, and remote scripts.
- `agent/lifecycle/macos.py`: macOS SSH, launchd, and remote scripts.

Keep these lower-level modules backward compatible where practical.

## Deployment Preflight

Do not start a session by deploying or restarting the target MCP server. Run
preflight first, then choose the lightest action that is actually needed.

## Target File Contract

Remote targets should not accumulate one-off files during onboarding. The
deployable unit is the repository's tracked `target/` tree:

```text
local repo target/  ->  configured target root
```

After that tree is present, MCP should start through existing tracked lifecycle
scripts and be controlled through MCP tools. Do not write temporary scripts,
throwaway config files, copied test snippets, or helper programs onto the target
to make a new target work. If a helper is genuinely required, add it to the repo
under `target/scripts/`, `target/scripts/macos/`, or `target/automation/`, then
deploy it with the normal `target/` sync.

Allowed target-side writes:

- `logs/` and PID files created by tracked startup scripts.
- screenshot/artifact files under `EDR_WD_ARTIFACT_DIR` or the default target
  artifact directory.
- macOS LaunchAgent plist installation generated from the tracked template.
- Windows scheduled-task registration using tracked scripts.
- application/runtime state created by HiSec/EDR itself.

Disallowed target-side writes:

- ad hoc `.py`, `.ps1`, `.sh`, `.bat`, or `.json` files for probing.
- copied one-off smoke tests outside the deployed `target/` tree.
- generated target config containing credentials.
- permanent helper scripts created only on one target machine.

Dependency preflight is read-only. Use Paramiko to run inline commands such as
`python -c ...`, PowerShell built-ins, and MCP `status`; do not upload probe
files to perform these checks.

Required order:

1. Validate local config.

   ```bash
   python scripts/check_dependencies.py --scope agent --include-test
   python -m agent.target_config --validate
   python -c "import fastmcp, paramiko, psutil, PIL, pyautogui; print('agent deps ok')"
   ```

   The agent dependency set comes from `pyproject.toml`. On Windows agents that
   also run Windows GUI automation locally, include `pywinauto` in this check.

2. Verify Paramiko SSH login and basic target identity.

   ```python
   from agent import target_manager
   print(target_manager.probe_target("2.26-edr-win26-win11"))
   ```

   A failed SSH probe means stop. Do not deploy until auth, host, and platform
   config are fixed.

3. Verify the complete target Python runtime before uploading or starting MCP.

   Preferred executable check:

   ```bash
   python scripts/check_dependencies.py --target <TARGET_NAME> --scope target --platform auto
   python scripts/check_dependencies.py --target <TARGET_NAME> --scope target --platform windows
   python scripts/check_dependencies.py --target <TARGET_NAME> --scope target --platform macos
   ```

   Windows PowerShell wrapper:

   ```powershell
   .\agent\check-deps.ps1 -TargetName <TARGET_NAME> -Scope all -Platform windows -IncludeTest
   .\agent\deploy.ps1 -Action check-deps -TargetName <TARGET_NAME> -Platform windows -IncludeTest
   ```

   Core target runtime imports:

   ```bash
   <REMOTE_PYTHON> -c "import fastmcp, psutil, PIL; print('core target deps ok')"
   ```

   Windows:

   ```powershell
   python scripts/check_dependencies.py --target <WINDOWS_TARGET> --scope target --platform windows
   "<REMOTE_PYTHON>" -c "import sys; print(sys.executable); print(sys.version)"
   "<REMOTE_PYTHON>" -c "import fastmcp, psutil, PIL; print('core deps ok')"
   "<REMOTE_PYTHON>" -c "import pywinauto, pyautogui; print('windows gui deps ok')"
   ```

   macOS:

   ```bash
   python scripts/check_dependencies.py --target <MACOS_TARGET> --scope target --platform macos
   '<REMOTE_PYTHON>' -c 'import sys; print(sys.executable); print(sys.version)'
   '<REMOTE_PYTHON>' -c 'import fastmcp, psutil, PIL; print("core deps ok")'
   '<REMOTE_PYTHON>' -c 'import pyautogui; print("mac gui deps ok")'
   ```

   Required runtime packages are currently defined in `pyproject.toml`:
   `fastmcp`, `psutil`, `Pillow`, `paramiko`, `pywinauto`, and `PyAutoGUI`.
   `paramiko` is only needed where the agent performs SSH/SFTP/tunnel work; the
   target MCP runtime primarily needs the FastMCP, process, image, and GUI
   backend dependencies.

   If any required import fails, install/fix the Python environment first. Do
   not deploy MCP files and hope startup will explain the dependency problem.

   When running pytest suites, also install the test-only dependencies declared
   in `test_case/requirements_test.txt`:

   ```bash
   python -m pip install -r test_case/requirements_test.txt
   python -c "import pytest, httpx; print('test deps ok')"
   ```

4. Check port `8765` and firewall before expecting MCP to answer.

   - `connect_mode=direct`: the target must listen on `ssh.host:mcp.port`, and
     host firewall must allow inbound TCP `8765`.
   - `connect_mode=tunnel`: the agent connects to `127.0.0.1:tunnel.local_port`;
     target firewall does not need to expose 8765 externally.
   - `connect_mode=local`: MCP is local to the agent/target machine; still
     check local port ownership before starting.

   Windows firewall preparation for direct mode:

   ```powershell
   Get-NetFirewallRule -DisplayName "EDR-WD MCP 8765" -ErrorAction SilentlyContinue
   New-NetFirewallRule -DisplayName "EDR-WD MCP 8765" `
     -Direction Inbound -Action Allow -Protocol TCP -LocalPort 8765
   ```

   Run the firewall command with an account that has permission to change
   firewall rules. If that is not possible, use `connect_mode=tunnel`.

   Also verify port ownership:

   Windows:

   ```powershell
   Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue
   ```

   macOS/Linux:

   ```bash
   lsof -iTCP:8765 -sTCP:LISTEN -n -P
   ```

5. If port `8765` is already open, test FastMCP before restarting.

   ```python
   from agent import mcp_manager, target_manager
   print(target_manager.check_server_health("2.26-edr-win26-win11"))
   print(mcp_manager.initialize("2.26-edr-win26-win11"))
   ```

   If initialize/status works, use the existing server. Redeploy only when the
   target code is stale or the requested change requires it. If the port is held
   by an unmanaged process, resolve that conflict before deploying.

Only after these checks pass should you call deployment/startup actions such as
`deploy_target()`, `install_target_task()`, or `ensure_running()`.

## Lifecycle Operation Contract

Use the least invasive operation that satisfies the request:

| Operation | Target writes | Contract |
|---|---:|---|
| `probe_target`, `check_server_health`, `health_detail` | No | Inspect identity, transport, MCP, and GUI readiness. |
| `ensure_server_running(repair=False)` | No upload/install | Validate tracked payload and scheduler registration, then start the installed service if needed. |
| `stop_server(repair=False)` | No upload/install | Validate tracked payload and invoke its installed stop script. |
| `restart_server(repair=False)` | No upload/install | Stop, then ensure; stop failures short-circuit. |
| `deploy_target` | Yes | Upload only Git-tracked files from `target/`. |
| `install_target_task` | Yes | Register Windows Task Scheduler or macOS LaunchAgent using tracked scripts. |
| `repair_target(repair=True)` | Yes | Run `deploy_target -> install_target_task -> ensure_server_running(repair=True)`. |
| `stop_server(repair=True)` | Conditional | Repair only `target_payload_incomplete`, then retry stop. |
| CLI `push` | Yes, unrestricted | Debug escape hatch; bypasses tracked-only filtering. |

`ensure_server_running` must reject incomplete payload with
`target_payload_incomplete`, invalid Windows registration with
`scheduled_task_invalid`, and invalid macOS registration with
`launchagent_invalid`. Keep SSH/probe errors distinct from missing state.

Successful repair results include ordered `repair_actions` and per-step
`repair_results`. Results must remain JSON-serializable. A repair failure or a
post-repair stop retry failure must return `recoverable=False`; only a completed
repair and successful retry may be marked recoverable.

`TargetSubAgent.ensure_running(repair=...)` and `ensure_ready(repair=...)` only
forward the flag. Keep repair branching in `agent/target_manager.py`, not in the
subagent or platform lifecycle backend.

## Convenience Wrappers

```bash
bash agent/edr-wd.sh up
bash agent/edr-wd.sh status
bash agent/edr-wd.sh smoke --gui
bash agent/edr-wd.sh down
bash agent/edr-wd.sh repair
```

Treat `bash agent/edr-wd.sh push ...` and `agent/deploy.ps1 -Action push` as
debug-only commands. Production synchronization must use tracked deployment or
repair.

On Windows agents, use `agent/deploy.ps1` for the same control plane.

## Target Runtime Scripts

Windows lifecycle files:

- `target/deploy.ps1`
- `target/scripts/start_server.ps1`
- `target/scripts/stop_server.ps1`
- `target/scripts/install_task.ps1`

macOS lifecycle files:

- `target/scripts/macos/start_server.sh`
- `target/scripts/macos/stop_server.sh`
- `target/scripts/macos/install_launch_agent.sh`
- `target/scripts/macos/com.edr-wd.target.plist.template`

Target runtime scripts belong under `target/scripts/`, not root `scripts/`.
Root `scripts/` is for developer utilities only.

## Local Agent/Target Mode

The agent and MCP target can run on the same machine. Use `connect_mode=local`
in config for that scenario; do not encode localhost assumptions into
deployment or MCP client code.

Local mode still needs normal MCP initialization and backend status checks.
Treat it as another target mode, not as a test shortcut.

## Tunnel Mode

`agent/tunnel.py` manages local port forwarding through Paramiko. `agent/tunnel.sh`
is only a compatibility wrapper around that Python entry point. Do not add
OpenSSH or `sshpass` paths for tunnel mode.

The MCP initializer automatically repairs one owned stale tunnel and retries
the handshake once. A stale tunnel is a process whose local listener still
exists while `/mcp` no longer responds. It is safe to replace only when its PID
file proves EDR-WD ownership; an unrelated listener on the configured port must
remain untouched and produce a structured error.

For normal interactive use, prefer the packaged CLI instead of temporary
Python scripts:

```bash
edr-wd --target TARGET tools
edr-wd --target TARGET call TOOL --args '{"key":"value"}'
edr-wd --target TARGET open-edr
```

`open-edr` verifies `EDRClient.exe` together with the exact HiSec main-window
title before connecting and taking a screenshot. This prevents another window
owned by the same process, such as `日志中心`, from becoming the screenshot
target. The screenshot is decoded and stored on the agent under
`result-report/<timestamp>/screenshots/`.
