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

## Convenience Wrappers

```bash
bash agent/edr-wd.sh up
bash agent/edr-wd.sh status
bash agent/edr-wd.sh smoke --gui
bash agent/edr-wd.sh down
```

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
