---
name: edr-wd
description: Use this skill when working on EDR-WD, a cross-platform MCP-based GUI automation system for HiSecEndpoint/EDR targets. Use it to inspect, modify, deploy, or test Windows/macOS agent-target workflows, target lifecycle scripts, FastMCP tools, GUI automation backends, and HiSec window-pair E2E behavior.
metadata:
  short-description: Work on EDR-WD MCP GUI automation
---

# EDR-WD

EDR-WD has two layers:

- `agent/`: config, SSH/SFTP, lifecycle, and MCP session orchestration.
- `target/`: FastMCP server, GUI automation backends, and target-local scripts.

The agent OS and target OS are independent. A macOS agent can drive Windows or
macOS targets, and a Windows agent can do the same when the target config and
SSH path are valid.

## First Steps

1. Check branch and local changes:

   ```bash
   git status --short --branch
   ```

2. Read only the files needed for the task. Start with:

   - `agent/target_config.py` for target config shape and validation.
   - `agent/subagent/` for per-target controller/session behavior.
   - `agent/lifecycle/` for remote start/stop/install behavior.
   - `target/server.py` for MCP tool registration.
   - `target/automation/` for Windows/macOS GUI behavior.
   - `test_case/` for profile-dispatched and pytest E2E coverage.

3. Never commit real target IPs, usernames, passwords, or local target paths.
   Runtime config belongs in `config/targets.local.json`, which must stay local.

## Target Config

Runtime targets are loaded by `agent.target_config.TargetConfig` from
`config/targets.local.json` or `EDR_WD_CONFIG`.

Use inline password auth for the current trusted intranet workflow:

```json
"ssh": {
  "host": "<TARGET_IP>",
  "port": 22,
  "user": "<TARGET_USER>",
  "auth": {"type": "password", "password": "<TARGET_PASSWORD>"}
}
```

`password_env` and key auth are compatibility paths. TODO security hardening:
move secrets out of local JSON if this leaves the trusted intranet setup.

Useful config commands:

```bash
python -m agent.target_config --init
python -m agent.target_config --validate
python -m agent.target_config --list
python -m agent.target_config --guide
```

## Agent Workflow

Prefer target-scoped subagents for new orchestration:

```python
from agent.subagent import TargetSubAgent

agent = TargetSubAgent.from_name("win-dev")
agent.ensure_running()
agent.initialize_mcp()
print(agent.call_tool("status"))
```

`TargetSubAgent` owns the selected target's lifecycle calls, MCP URL/session,
backend status, and profile/backend validation. Keep lower-level
`target_manager` and `mcp_manager` behavior compatible, but avoid adding new
session state outside the subagent layer.

Convenience wrappers:

```bash
bash agent/edr-wd.sh up
bash agent/edr-wd.sh status
bash agent/edr-wd.sh smoke --gui
bash agent/edr-wd.sh down
```

On Windows agents, use `agent/deploy.ps1` for the same control plane.

## Target Lifecycle

Windows lifecycle:

- `agent/lifecycle/windows.py`
- `target/deploy.ps1`
- `target/scripts/start_server.ps1`
- `target/scripts/stop_server.ps1`
- `target/scripts/install_task.ps1`

macOS lifecycle:

- `agent/lifecycle/macos.py`
- `target/scripts/macos/start_server.sh`
- `target/scripts/macos/stop_server.sh`
- `target/scripts/macos/install_launch_agent.sh`
- `target/scripts/macos/com.edr-wd.target.plist.template`

Target runtime scripts belong under `target/scripts/`, not root `scripts/`.
Root `scripts/` is for developer utilities only.

## MCP And GUI Backends

`target/server.py` is the FastMCP server. Backends are selected with
`EDR_WD_AUTOMATION_BACKEND`:

- `windows_pywinauto`
- `macos_accessibility`

Important tools:

- Session/window: `connect`, `list_windows`, `is_window_open`, `wait_window`,
  `status`
- HiSec: `activate_edr`, `restore_edr`
- GUI actions: `dump_tree`, `find_control`, `click`, `click_target`,
  `click_at`, `click_window_at`, `double_click_at`, `right_click_at`,
  `middle_click_at`, `hover_at`, `drag`, `scroll`, `type_text`, `select`,
  `get_text`, `screenshot`
- Safety: `lock_window`, `unlock_window`, `get_window_lock`,
  `verify_window_lock`
- Windows PowerShell: `run_powershell`, `start_powershell`, `get_job`,
  `cancel_job`

PowerShell tools require `EDR_WD_ENABLE_POWERSHELL=1` on the target server.
`activate_edr` does not depend on PowerShell.

## HiSec Window-Pair Contract

`activate_edr(wait=True)` must make both windows visible:

- Entry/main window: `HisecEndpointAgent.exe` on Windows or
  `HiSecEndpointAgent` on macOS.
- Target/client window: `EDRClient.exe` on Windows or `EDRClient` on macOS.

Windows flow:

1. Ensure `HisecEndpointAgent.exe cmd ui` is visible.
2. Prefer `EDRClient.exe 17 --show`.
3. Fall back to clicking `edrWidget` in the entry window.
4. Connect by desktop window handle first; PID connection is fallback.

macOS flow:

1. Ensure `HiSecEndpointAgent` is visible with `open
   /Applications/HiSecEndpoint.app`, then `HiSecEndpointAgent cmd ui`.
2. Prefer `/Applications/HiSecEndpoint.app/Contents/script/root_start_client.sh`
   via non-interactive `sudo -n`.
3. Fall back to Swift Accessibility helper clicking “前往安全防护中心”.

For details, read `references/activate-edr.md` only when changing activation
logic.

## macOS Element Clicks

For macOS, `dump_tree`, `find_control`, `click`, and `click_target` use
Accessibility data. When a standard selector is insufficient, use a Swift AX
helper under `target/scripts/macos/`. Read `references/element-click.md` before
changing Swift helper behavior.

macOS `click_at` is dry-run by default. Set `EDR_WD_ALLOW_REAL_CLICKS=1` on the
target server only when real pointer actions are intended.

## Testing

Live MCP smoke:

```bash
python target/tests/smoke_mcp_client.py --base-url http://127.0.0.1:8765/mcp
python target/tests/smoke_mcp_client.py --base-url http://127.0.0.1:8765/mcp --gui
```

Profile-dispatched suites:

```bash
python test_case/run_tests.py --target win-dev
python test_case/run_tests.py --target mac-dev
```

Focused local checks:

```bash
python3 target/tests/test_window_lock_contract.py
python3 -m pytest --collect-only -q test_case/test_integration test_case/test_e2e
python3 scripts/test_profile_resolution.py
python3 scripts/test_target_config_platforms.py
```

When changing GUI behavior, update or run the matching profile:

- Windows HiSec: `test_case/run_windows_hisec.py` and
  `test_case/test_e2e/test_windows_hisec_e2e.py`
- macOS HiSec: `test_case/run_macos_hisec.py` and
  `test_case/test_e2e/test_macos_hisec_e2e.py`
- Generic macOS plumbing: `test_case/run_macos_generic.py`

## References

Load these only when relevant:

- `references/activate-edr.md`: Windows/macOS HiSec activation internals.
- `references/window-detection.md`: window verification and debugging workflow.
- `references/element-click.md`: macOS Swift AX element-click pattern.
- `docs/architecture/`: historical architecture notes and deeper context.

## Housekeeping

Generated files are not project structure. Remove local artifacts such as
`.venv/`, `__pycache__/`, `.pytest_cache/`, `target/logs/`, root `*.log`,
`target/server.log`, and `.DS_Store` before packaging or publishing.

Keep `SKILL.md` concise. Move detailed examples, coordinate notes, and
platform-specific internals into `references/` instead of duplicating them in
the skill body.
