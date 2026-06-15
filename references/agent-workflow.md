# Agent Workflow Reference

Use this reference when changing agent orchestration, target lifecycle, or
deployment behavior.

## Preferred Entry Point

Use `TargetSubAgent` for new orchestration:

```python
from agent.subagent import TargetSubAgent

agent = TargetSubAgent.from_name("win-dev")
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
- `agent/target_manager.py`: deploy, ensure, stop, restart, and health checks.
- `agent/mcp_manager.py`: FastMCP Streamable HTTP initialize and JSON-RPC calls.
- `agent/lifecycle/windows.py`: Windows SSH, task scheduler, and remote scripts.
- `agent/lifecycle/macos.py`: macOS SSH, launchd, and remote scripts.

Keep these lower-level modules backward compatible where practical.

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
