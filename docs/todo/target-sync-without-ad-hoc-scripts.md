# Target Sync Without Ad Hoc Script Writes

## Status

Completed and retained as a design record.

The Windows and macOS lifecycle normal paths no longer upload scripts,
deploy uses tracked-only directory sync, and repair is an explicit opt-in
propagated through `TargetSubAgent` and `target_manager`. The contracts are
covered by `test_lifecycle_no_adhoc_scp.py`,
`test_lifecycle_target_integrity.py`, and
`test_macos_deploy_tracked_only.py`.

## Goal

EDR-WD should have one deployable target payload:

```text
repo target/  ->  configured target root
```

After that payload is present on the target, normal operations must connect to
the target MCP server, initialize MCP, call `status`, and run
baseline/E2E/SOP operations without writing helper scripts, config files, test
files, or generated programs to the target.

Target-side code is allowed only when it is reviewed and committed under one of
these repository paths:

- `target/scripts/`
- `target/scripts/macos/`
- `target/automation/`
- `target/server.py`
- target package/runtime files already tracked by git

The explicit deploy/install phase may write files because its purpose is to
deliver the reviewed target payload. Normal post-install phases must not write
lifecycle scripts.

## Non-Goals

- Do not add MCP-to-MCP as part of this cleanup.
- Do not make normal `connect`, `status`, tests, E2E, or SOP flows repair the
  target automatically.
- Do not generate target-local `.py`, `.ps1`, `.sh`, `.bat`, `.json`, or
  temporary probe files to make a single target pass.
- Do not install Python packages automatically during read-only preflight.
- Do not copy `config/targets.local.json`, credentials, logs, screenshots,
  caches, or local-only files to the target.

## User-Facing Workflow

### First-Time Or Explicit Repair Flow

Use this when the target has no payload, scripts are missing, scheduled task or
LaunchAgent is missing, or a previous deploy is stale.

```bash
python scripts/check_dependencies.py --target <TARGET_NAME> --scope all --platform auto
python -m agent.target_config --validate
python - <<'PY'
from agent import target_manager

target = "<TARGET_NAME>"
print(target_manager.probe_target(target))
print(target_manager.deploy_target(target))
print(target_manager.install_target_task(target))
print(target_manager.ensure_server_running(target, repair=False))
PY
```

Expected behavior:

- `probe_target()` performs read-only SSH/runtime checks.
- `deploy_target()` syncs tracked `target/` files.
- `install_target_task()` registers Task Scheduler on Windows or LaunchAgent on
  macOS using the already synced scripts.
- `ensure_server_running(..., repair=False)` starts only through the already
  installed lifecycle hook and performs no upload.

### Normal Connect Flow

Use this after explicit deploy/install has completed.

```python
from agent.subagent import TargetSubAgent

agent = TargetSubAgent.from_name("<TARGET_NAME>")
running = agent.ensure_running(repair=False)
if not running["ok"]:
    raise RuntimeError(running)

session = agent.initialize_mcp()
if not session["ok"]:
    raise RuntimeError(session)

print(agent.call_tool("status"))
print(agent.call_tool("list_windows"))
```

Expected behavior:

- no `scp_to()`
- no `scp_dir_to()`
- no deploy/install
- no target file writes except runtime artifacts created by an already running
  target MCP server

### Baseline Test Flow

```bash
EDR_WD_TARGET=<TARGET_NAME> python test_case/run_tests.py --profile macos_generic -v
EDR_WD_TARGET=<TARGET_NAME> python test_case/run_tests.py --profile windows_hisec -v
```

Test fixtures must call `ensure_server_running(repair=False)` by default. If
the target payload is missing, tests should fail with a structured lifecycle
error that tells the operator to run explicit deploy/install first.

### E2E/SOP Flow

```python
from agent.subagent import TargetSubAgent

agent = TargetSubAgent.from_name("<TARGET_NAME>")
agent.ensure_running(repair=False)
agent.initialize_mcp()
result = agent.call_tool("activate_edr", {"wait": True, "timeout": 20.0})
print(result)
```

Allowed target-side effects:

- EDR/HiSec application process start.
- screenshots/evidence under the configured artifact directory.
- normal MCP runtime logs.

Forbidden target-side effects:

- uploading lifecycle scripts.
- generating helper scripts.
- installing dependencies.
- re-registering scheduled tasks or LaunchAgents.

### Manual Debug Push

`agent/edr-wd.sh push` and `agent/deploy.ps1 -Action push` are manual/debug
tools only. They must not be called from normal connect, tests, E2E, SOP, or
relay flows.

If retained, CLI help should label them as `debug-push` or
`manual/debug-only`.

## Lifecycle Phases

### Phase 1: Preflight

Read-only.

Responsibilities:

- validate target config.
- verify Paramiko SSH login for remote targets.
- verify agent dependencies.
- verify target Python and backend dependencies with inline commands.
- inspect MCP port/tunnel state.
- check whether required target files and lifecycle hooks exist.

Must not:

- upload probe scripts.
- create files on the target.
- install Python packages.
- register lifecycle hooks.

### Phase 2: Deploy

Write-capable and explicit.

Responsibilities:

- sync only git-tracked `target/` files to the configured target root.
- exclude logs, screenshots, caches, local config, generated reports, and
  untracked debug files.
- preserve remote runtime directories such as `logs/` and artifact directories
  unless an explicit cleanup operation is requested.

### Phase 3: Install

Write-capable and explicit.

Responsibilities:

- Windows: register the reviewed scheduled task using tracked
  `target/scripts/install_task.ps1`.
- macOS: register the reviewed LaunchAgent using tracked
  `target/scripts/macos/install_launch_agent.sh` and plist template.
- do not perform unrelated deploy or dependency installation.

### Phase 4: Start/Stop

No upload.

Responsibilities:

- Windows start: run the already installed scheduled task with
  `schtasks /Run /TN <task> /I`.
- Windows stop: execute the already present
  `<target_root>/scripts/stop_server.ps1`, or return
  `target_payload_incomplete` if it is missing.
- macOS start: kickstart the already installed LaunchAgent.
- macOS stop: execute the already present
  `<target_root>/scripts/macos/stop_server.sh`, or return
  `target_payload_incomplete` if it is missing.

### Phase 5: Connect

No upload.

Responsibilities:

- repair only local tunnel state when `connect_mode=tunnel`.
- initialize MCP.
- call `status`.
- use existing target MCP server if healthy.
- report structured errors for missing payload, missing lifecycle hook, stale
  listener, disconnected desktop session, or backend mismatch.

### Phase 6: Test/E2E/SOP

No lifecycle writes.

Responsibilities:

- use MCP tools only.
- run profile-aware test suites.
- collect allowed evidence artifacts under configured artifact directories.

## Public API Design

### `agent.target_manager.probe_target(name=None) -> dict`

Read-only target capability check.

Responsibilities:

- resolve config.
- SSH command execution through Paramiko.
- platform identity check.
- Python/runtime/backend dependency import checks.

Must not:

- call `deploy_target()`.
- call `install_target_task()`.
- call `scp_to()` or `scp_dir_to()`.

Result shape:

```python
{
    "ok": True,
    "target": "<TARGET_NAME>",
    "stage": "probe",
    "data": {
        "ssh": {"ok": True},
        "python": {"ok": True},
        "python_dependencies": {"ok": True},
        "identity": {"ok": True},
    },
}
```

### `agent.target_manager.deploy_target(name=None) -> dict`

Explicit write operation.

Responsibilities:

- call platform lifecycle `deploy(cfg)`.
- sync tracked target payload to target root.
- verify key files landed at the correct level.

Windows implementation:

```python
scp_dir_to(ssh_cfg, str(LOCAL_TARGET), target_root, tracked_only=True)
```

macOS implementation should match Windows:

```python
scp_dir_to(ssh_cfg, str(LOCAL_TARGET), macos_root, tracked_only=True)
```

Must not:

- copy untracked files.
- copy local config/secrets.
- install scheduled tasks or LaunchAgents.

### `agent.target_manager.install_target_task(name=None) -> dict`

Explicit write operation.

Responsibilities:

- upload tracked lifecycle installer scripts if deploy did not already sync
  them, or verify that already synced scripts exist.
- register the OS lifecycle hook.

Windows:

- required remote files:
  - `<target_root>/scripts/install_task.ps1`
  - `<target_root>/scripts/start_server.ps1`
  - `<target_root>/scripts/stop_server.ps1`
- registers `windows.task_name`, default `StartEDRMCP`.
- uses interactive logon for GUI desktop context.

macOS:

- required remote files:
  - `<target_root>/scripts/macos/install_launch_agent.sh`
  - `<target_root>/scripts/macos/start_server.sh`
  - `<target_root>/scripts/macos/stop_server.sh`
  - `<target_root>/scripts/macos/com.edr-wd.target.plist.template`
- registers `macos.launch_name`.
- runs inside the user GUI session.

### `agent.target_manager.ensure_server_running(name=None, repair=False) -> dict`

Default no-write operation.

Proposed signature:

```python
def ensure_server_running(name: Optional[str] = None, *, repair: bool = False) -> dict:
    ...
```

Responsibilities when `repair=False`:

- inspect existing port state.
- inspect target payload integrity.
- inspect scheduled task or LaunchAgent integrity.
- start through existing lifecycle hook when needed.
- perform GUI readiness checks.
- return structured errors with `next_action` hints.

Responsibilities when `repair=True`:

- may call `deploy_target()` and `install_target_task()` if integrity checks
  fail.
- must report every write operation in `data.repair_actions`.
- should be used only by explicit repair commands, not by tests or normal
  connect flows.

Forbidden when `repair=False`:

- `deploy_target()`
- `install_target_task()`
- `scp_to()`
- `scp_dir_to()`
- writing any target helper file

### `agent.target_manager.stop_server(name=None, repair=False) -> dict`

Default no-upload operation.

Proposed signature:

```python
def stop_server(name: Optional[str] = None, *, repair: bool = False) -> dict:
    ...
```

Responsibilities:

- execute already present remote stop script.
- verify port is closed.
- if script is missing and `repair=False`, return
  `target_payload_incomplete`.
- if script is missing and `repair=True`, explicit repair may deploy/install
  first, then stop.

### `agent.target_manager.restart_server(name=None, repair=False) -> dict`

Equivalent to:

```python
stop_server(name, repair=repair)
ensure_server_running(name, repair=repair)
```

Default `repair=False`.

### `agent.subagent.TargetSubAgent.ensure_running(repair=False) -> dict`

Target-scoped wrapper over `target_manager.ensure_server_running()`.

Proposed signature:

```python
def ensure_running(self, *, repair: bool = False) -> dict:
    ...
```

State updates:

- set `state.server_running` from result.
- set `state.mcp_url` when available.
- set `state.ready_level`.
- set `state.backend_kind`.
- set `state.health`.
- set `state.last_error` on failure.

Must not hide lifecycle errors. If the result is
`target_payload_incomplete`, the subagent should return that error unchanged.

### `agent.subagent.TargetSubAgent.ensure_ready(repair=False) -> dict`

Proposed signature:

```python
def ensure_ready(self, *, repair: bool = False) -> dict:
    running = self.ensure_running(repair=repair)
    if not running.get("ok"):
        return running
    return self.initialize_mcp()
```

Default `repair=False`.

### `agent.mcp_manager.initialize(name=None) -> dict`

MCP-only operation.

Responsibilities:

- build MCP URL.
- perform Streamable HTTP initialize handshake.
- return session ID and URL.

Must not:

- start target server.
- deploy files.
- install lifecycle hooks.

### `agent.mcp_manager.call_mcp_tool(...) -> dict`

MCP-only operation.

Responsibilities:

- forward JSON-RPC `tools/call`.
- parse SSE response.
- return structured result.

Must not:

- call lifecycle APIs.
- repair target state.

## Platform Lifecycle Design

### Windows Target Integrity

Add a read-only helper:

```python
def _target_integrity(self, cfg: dict) -> dict:
    ...
```

Checks:

- `<target_root>/server.py`
- `<target_root>/automation/__init__.py`
- `<target_root>/scripts/start_server.ps1`
- `<target_root>/scripts/stop_server.ps1`
- `<target_root>/scripts/install_task.ps1`

Result:

```python
{
    "ok": True,
    "missing": [],
    "target_root": "<redacted or structured path>",
}
```

If missing and `repair=False`, return:

```python
{
    "ok": False,
    "stage": "ensure",
    "code": "target_payload_incomplete",
    "error": "Target payload is incomplete; run explicit deploy/install.",
    "details": {"missing": ["scripts/start_server.ps1"]},
    "next_action": "Run deploy_target() then install_target_task().",
}
```

### Windows Task Integrity

Add a read-only helper:

```python
def _task_integrity(self, cfg: dict) -> dict:
    ...
```

Checks:

- scheduled task exists.
- task action points to the target root start script.
- principal logon type is interactive.
- run level is highest when configured.

If invalid and `repair=False`, return:

```python
{
    "ok": False,
    "stage": "ensure",
    "code": "scheduled_task_invalid",
    "error": "Scheduled task is missing or does not point to target payload.",
    "next_action": "Run install_target_task().",
}
```

### Windows Start

No upload path:

```python
task_name = win_cfg.get("task_name", "StartEDRMCP")
run_ssh(ssh_cfg, f'schtasks /Run /TN "{task_name}" /I')
```

After start:

- wait for port.
- verify process session is not 0.
- verify RDP/desktop session is active.
- initialize MCP through `mcp_manager.health_detail()`.
- check `list_windows_count > 0`.

### Windows Stop

No upload path:

```python
remote_stop = f"{target_root}/scripts/stop_server.ps1"
run_ssh(
    ssh_cfg,
    f'powershell -NoProfile -ExecutionPolicy Bypass -File "{remote_stop}" -Port {port}',
)
```

If `remote_stop` missing, return `target_payload_incomplete`.

### macOS Target Integrity

Add a read-only helper:

```python
def _target_integrity(self, cfg: dict) -> dict:
    ...
```

Checks:

- `<root>/server.py`
- `<root>/automation/__init__.py`
- `<root>/scripts/macos/start_server.sh`
- `<root>/scripts/macos/stop_server.sh`
- `<root>/scripts/macos/install_launch_agent.sh`
- `<root>/scripts/macos/com.edr-wd.target.plist.template`

### macOS LaunchAgent Integrity

Add a read-only helper:

```python
def _launchagent_integrity(self, cfg: dict) -> dict:
    ...
```

Checks:

- plist exists in `~/Library/LaunchAgents/<launch_name>.plist`.
- plist program path references the target root start script.
- label matches `macos.launch_name`.

If missing and `repair=False`, return `launchagent_invalid`.

### macOS Deploy

Use tracked-only sync:

```python
from agent.ssh_runner import scp_dir_to

scp_dir_to(ssh_cfg, str(LOCAL_TARGET), macos_root, timeout=60, tracked_only=True)
```

This mirrors Windows and prevents copying untracked caches/logs/screenshots.

### macOS Start

No upload path:

```bash
launchctl kickstart -k "gui/${UID}/${launch_name}"
```

After start:

- wait for MCP port.
- verify listener is managed by current target root.
- initialize MCP through `mcp_manager.health_detail()`.
- verify backend/list windows.

### macOS Stop

No upload path:

```bash
bash '<target_root>/scripts/macos/stop_server.sh' --port <port>
```

If missing, return `target_payload_incomplete`.

## Dependencies

### Agent Runtime Dependencies

Declared in `pyproject.toml`:

- `paramiko`: SSH/SFTP/SCP transport.
- `fastmcp`: MCP client/server compatibility and local development.
- `psutil`: local process/port checks where applicable.
- `Pillow`: image/screenshot handling.
- `PyAutoGUI`: local or target GUI automation dependency.
- `pywinauto`: Windows GUI automation dependency when the agent is also a
  Windows target or when imports are validated.

Required external tools on the agent:

- `git`: tracked-only payload enumeration via `git ls-files`.
- Python 3.9 or newer.

### Target Common Dependencies

- Python 3.9 or newer.
- `fastmcp`
- `psutil`
- `Pillow`
- `PyAutoGUI`

### Windows Target Dependencies

- `pywinauto`
- PowerShell
- OpenSSH server or another Paramiko-compatible SSH endpoint
- Task Scheduler
- active RDP/local desktop session
- inbound firewall rule for MCP port when `connect_mode=direct`

Important environment variables used by target scripts:

- `EDR_WD_ENABLE_PYWINAUTO=1`
- `EDR_WD_ENABLE_POWERSHELL=1`
- `EDR_WD_AUTOMATION_BACKEND=windows_pywinauto`
- `EDR_WD_TARGET_ROOT=<target_root>`
- `EDR_WD_MCP_HOST=<bind_host>` optional
- `EDR_WD_MCP_PORT=<port>` optional
- `EDR_WD_PYTHON=<python.exe>` optional

### macOS Target Dependencies

- Accessibility permission for the terminal/launch context running MCP.
- Screen Recording permission for screenshots.
- `launchctl`
- `osascript`
- `screencapture`
- `lsof`
- active local/RDP/VNC desktop session.

Important environment variables:

- `EDR_WD_AUTOMATION_BACKEND=macos_accessibility`
- `EDR_WD_TARGET_ROOT=<target_root>`
- `EDR_WD_ARTIFACT_DIR=<artifact_dir>` optional
- `EDR_WD_MCP_HOST=<bind_host>` optional
- `EDR_WD_MCP_PORT=<port>` optional

## Target File Contract

Allowed tracked payload:

```text
target/
  server.py
  artifacts.py
  pywinauto_client.py
  automation/
  scripts/
  scripts/macos/
  tests/
  tmp/.gitkeep
  tmp/screenshots/.gitkeep
```

Allowed target runtime writes:

- `logs/`
- PID files.
- screenshots/evidence under configured artifact directory.
- Windows scheduled task registration during explicit install.
- macOS LaunchAgent plist during explicit install.

Forbidden normal-flow writes:

- ad hoc `.py`, `.ps1`, `.sh`, `.bat`, `.json` helper files.
- generated probe scripts.
- local target config copied from the agent.
- untracked debug scripts.
- dependency installation side effects during preflight/connect/test.

## Structured Error Codes

Add or preserve these codes:

- `target_payload_incomplete`: required tracked target file is missing.
- `scheduled_task_invalid`: Windows scheduled task missing or wrong.
- `launchagent_invalid`: macOS LaunchAgent missing or wrong.
- `server_start_failed`: lifecycle hook failed to start.
- `server_start_timeout`: MCP port did not open.
- `stale_listener`: port is occupied by unmanaged process.
- `desktop_session_disconnected`: GUI session is disconnected.
- `session0_or_desktop_unavailable`: Windows process is in Session 0 or cannot
  access desktop.
- `gui_not_ready`: MCP is reachable but windows/backend are not ready.
- `backend_mismatch`: live backend does not match selected profile.
- `script_upload_failed`: allowed only in explicit deploy/install/repair.

Every no-write error should include:

```python
{
    "ok": False,
    "stage": "...",
    "code": "...",
    "error": "...",
    "details": {...},
    "next_action": "Run deploy_target() then install_target_task().",
}
```

## Implementation Plan

1. Add `repair=False` plumbing:

   - `target_manager.ensure_server_running(name=None, *, repair=False)`
   - `target_manager.stop_server(name=None, *, repair=False)`
   - `target_manager.restart_server(name=None, *, repair=False)`
   - `TargetSubAgent.ensure_running(repair=False)`
   - `TargetSubAgent.ensure_ready(repair=False)`
   - test fixtures call non-repair mode.

2. Add read-only integrity helpers:

   - Windows `_target_integrity()`
   - Windows `_task_integrity()`
   - macOS `_target_integrity()`
   - macOS `_launchagent_integrity()`

3. Remove implicit uploads from Windows normal paths:

   - no `scp_to(start_server.ps1)` in `ensure_server_running()`.
   - no `scp_to(stop_server.ps1)` in `stop_server()`.
   - if script/task missing and `repair=False`, return structured error.

4. Remove implicit uploads from macOS normal paths:

   - no `scp_to(start_server.sh)` in `ensure_server_running()`.
   - no `scp_to(stop_server.sh)` in `stop_server()`.
   - if script/LaunchAgent missing and `repair=False`, return structured error.

5. Make macOS deploy tracked-only:

   - replace `scp_to(LOCAL_TARGET, macos_root)` with
     `scp_dir_to(..., tracked_only=True)`.

6. Add explicit repair path:

   - either `ensure_server_running(..., repair=True)` or a wrapper
     `repair_target(name)`.
   - repair path may deploy/install and must report `repair_actions`.

7. Update wrappers and docs:

   - `agent/edr-wd.sh`
   - `agent/deploy.ps1`
   - `SKILL.md`
   - `references/agent-workflow.md`

8. Add regression tests.

## Regression Test Plan

Add `test_case/test_lifecycle_no_implicit_uploads.py`.

Required tests:

- Windows ensure with missing payload and `repair=False`:
  - returns `target_payload_incomplete`.
  - does not call `deploy()`.
  - does not call `install()`.
  - does not call `scp_to()`.

- Windows ensure with invalid scheduled task and `repair=False`:
  - returns `scheduled_task_invalid`.
  - does not call `install()`.
  - does not call `scp_to()`.

- Windows stop with missing stop script and `repair=False`:
  - returns `target_payload_incomplete`.
  - does not upload `stop_server.ps1`.

- macOS deploy:
  - calls `scp_dir_to(..., tracked_only=True)`.
  - does not call generic directory `scp_to()`.

- macOS ensure with missing LaunchAgent and `repair=False`:
  - returns `launchagent_invalid`.
  - does not upload `start_server.sh`.

- macOS stop with missing stop script and `repair=False`:
  - returns `target_payload_incomplete`.
  - does not upload `stop_server.sh`.

- `TargetSubAgent.ensure_running()`:
  - forwards `repair=False` by default.
  - preserves structured lifecycle errors.

- test fixtures:
  - use non-repair ensure.
  - do not call deploy/install implicitly.

Suggested monkeypatch targets:

- `agent.lifecycle.windows.scp_to`
- `agent.lifecycle.windows.scp_dir_to`
- `agent.lifecycle.macos.scp_to`
- `agent.lifecycle.macos.scp_dir_to`
- `WindowsLifecycle.deploy`
- `WindowsLifecycle.install`
- `MacOSLifecycle.deploy`
- `MacOSLifecycle.install`
- `agent.target_manager.health_detail`
- `agent.ssh_runner.run_ssh`

## Live Validation Plan

Windows target:

```bash
python scripts/check_dependencies.py --target <WINDOWS_TARGET> --scope all --platform windows
python - <<'PY'
from agent import target_manager
t = "<WINDOWS_TARGET>"
print(target_manager.deploy_target(t))
print(target_manager.install_target_task(t))
print(target_manager.ensure_server_running(t, repair=False))
PY
```

Then verify no upload during normal connect:

```python
from agent.subagent import TargetSubAgent

agent = TargetSubAgent.from_name("<WINDOWS_TARGET>")
print(agent.ensure_running(repair=False))
print(agent.initialize_mcp())
print(agent.call_tool("status"))
print(agent.call_tool("list_windows"))
```

macOS target:

```bash
python scripts/check_dependencies.py --target <MACOS_TARGET> --scope all --platform macos
python - <<'PY'
from agent import target_manager
t = "<MACOS_TARGET>"
print(target_manager.deploy_target(t))
print(target_manager.install_target_task(t))
print(target_manager.ensure_server_running(t, repair=False))
PY
```

Then run:

```bash
EDR_WD_TARGET=<MACOS_TARGET> python test_case/run_tests.py --profile macos_generic -v
```

## Acceptance Criteria

The cleanup is complete when:

- connecting to an existing target performs no remote file upload.
- starting an installed target performs no remote file upload.
- stopping an installed target performs no remote file upload.
- running baseline tests performs no remote file upload.
- running HiSec E2E/SOP performs no remote lifecycle file upload.
- explicit deploy/install/repair commands are the only operations that sync
  tracked target files or install lifecycle hooks.
- Windows and macOS deploy both use tracked-only target sync.
- missing target files produce actionable structured errors instead of hidden
  repair.
- `push` is documented as manual/debug only and is not used by automated flows.
- regression tests fail if `scp_to()` is reintroduced into normal start/stop
  paths.

## Open Questions

- Should `repair=True` live on `ensure_server_running()` or be exposed only as
  a separate `repair_target()` command?
- Should `install_target_task()` trust `deploy_target()` to sync scripts, or
  continue uploading installer scripts as an explicit install-side write?
- Should `push` be renamed to `debug-push` now, or only documented as
  debug-only for compatibility?
- Should target integrity errors redact full target paths, or include them for
  trusted intranet debugging?
