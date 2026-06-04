---
name: edr-wd
description: |
  Windows EDR (HiSecEndpoint) GUI automation via MCP. Controls HiSecEndpoint
  using pywinauto/UIA — clicks, tree dumps, typing, screenshots, and more.

  Trigger scenarios:
  (1) Automating Windows desktop apps (HiSecEndpoint, etc.)
  (2) Reading window control trees and operating on controls
  (3) Remote control of Windows GUI via MCP (direct or SSH tunnel)
  (4) Any MCP client (Hermes / Codex / Claude Desktop / custom)
      cross-platform GUI automation

  Platforms: Windows (MCP Server side), Mac/Linux (MCP Client side)
---

# EDR-WD — Windows EDR GUI Automation

## Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│  Agent side (Mac / Linux)                                        │
│                                                                  │
│  ┌──────────────────┐     ┌──────────────────────────────────┐ │
│  │  hermes / openclaw │────▶│  target_config.py                │ │
│  │                    │     │  build_mcp_url(name)             │ │
│  │  skill / tools    │────▶│  target_manager.py               │ │
│  └──────────────────┘     │  mcp_manager.py                   │ │
│                            │  ssh_runner.py                     │ │
│                            └──────────────┬────────────────────┘ │
│                                           │                     │
│  ┌──────────────────┐     ┌──────────────▼────────────────────┐ │
│  │  test_case/       │────▶│  test runner (run_tests.py)      │ │
│  │  conftest.py      │     │  McpClient(mcp_init_result=...)  │ │
│  └──────────────────┘     └───────────────────────────────────┘ │
│                                           │                      │
│                                           │ SSH + schtasks        │
│  ┌──────────────────┐                   ▼                      │
│  │  SSH tunnel (opt) │    ┌──────────────────────────────┐       │
│  │  :18765 → :8765   │───▶│  Windows target            │       │
│  └──────────────────┘    │  0.0.0.0:8765              │       │
└───────────────────────────│──────────────┬──────────────┘───────┘
                            │              │ Streamable HTTP /mcp
                            │              ▼
                            │  ┌──────────────────────────────┐  │
                            │  │  target/                    │  │
                            │  │    server.py  (fastmcp 3.x) │  │
                            │  │    pywinauto_client.py     │  │
                            │  └──────────────────────────────┘  │
                            │              │ pywinauto UIA        │
                            ▼              ▼
                  HiSecEndpoint GUI (华为)
```

**Key principle:** The agent never starts Python over SSH. Instead it triggers
`schtasks /Run /TN StartEDRMCP` which launches `start_server.ps1` in the
logged-on interactive desktop session.

**Two-layer ready model:**
- `target_manager.ensure_server_running()` → `ready_level: "tcp_only"` (port open)
- `mcp_manager.initialize()` → `ready_level: "mcp_ready"` (MCP session established)

---

## Configuration — `config/targets.local.json`

All target definitions live in `config/targets.local.json`. The example template
`config/targets.example.json` is committed to the repo; copy it to
`targets.local.json` and fill in real values — **never commit `targets.local.json`**.

```
config/targets.example.json   ← repo template (no real passwords)
config/targets.local.json     ← local config (gitignored)
```

### Legacy config — DEPRECATED

`config/test_machines.json` is deprecated. All configuration now flows through
`config/targets.local.json` via `TargetConfig`. Do not add new entries to
`test_machines.json`.

### Generate skeleton

```bash
# First time — generate config from template:
python -m agent.target_config --init
# Created: /Users/edr-test/edr-wd/config/targets.local.json
# Edit the file and fill in ssh.host, ssh.user, windows.python_path, etc.

# List targets:
python -m agent.target_config --list

# Validate:
python -m agent.target_config --validate
```

### Config structure

```json
{
  "default_target": "win-dev",

  "targets": {
    "win-dev": {
      "description": "Development Windows VM",

      "ssh": {
        "host": "170.170.11.26",
        "port": 22,
        "user": "admin",
        "auth": {
          "type": "password",
          "password_env": "EDR_WD_WIN_DEV_PASSWORD"
        }
      },

      "mcp": {
        "host": "0.0.0.0",
        "port": 8765,
        "path": "/mcp",
        "connect_mode": "direct",
        "tunnel": { "enabled": false, "local_port": 18765 }
      },

      "windows": {
        "python_path": "C:\\Program Files\\Python313\\python.exe",
        "target_root": "C:\\Users\\admin\\Desktop\\edr-wd\\target",
        "task_name": "StartEDRMCP",
        "run_with_highest_privileges": true
      }
    }
  }
}
```

### Auth: password via environment variable

Set the password before running:

```bash
export EDR_WD_WIN_DEV_PASSWORD='whl@123'
```

Or per-target env var (add more targets as needed):

```bash
export EDR_WD_WIN_PROD_PASSWORD='ProdPass!'
```

---

## Quick Start

### 1. Generate config

```bash
python -m agent.target_config --init
# then edit config/targets.local.json
export EDR_WD_WIN_DEV_PASSWORD='whl@123'
```

### 2. Validate

```bash
python -m agent.target_config --validate
```

### 3. One-time task installation (on target)

```bash
python -c "from agent.target_manager import TargetManager; print(TargetManager().install_target_task())"
```

### 4. Run tests

```bash
cd test_case && python3 run_tests.py --target win-dev -v
# or via environment variable:
EDR_WD_TARGET=win-dev python3 run_tests.py -v
```

---

## Directory Structure

```
edr-wd/
├── SKILL.md
│
├── agent/                    # Agent-side (runs on Mac/Linux)
│   ├── target_config.py     # Config loader: load, init, validate, build_mcp_url
│   ├── ssh_runner.py        # Pure SSH/SCP executor (no config knowledge)
│   ├── target_manager.py    # Multi-target lifecycle manager (tcp_only)
│   └── mcp_manager.py       # MCP client (Streamable HTTP, mcp_ready)
│
├── target/                    # Target-side (runs on Windows)
│   ├── server.py            # MCP server entry (fastmcp 3.x + pywinauto)
│   ├── pywinauto_client.py  # WindowsGUI class
│   ├── config.json          # Server local config (host/port/backend only)
│   │
│   ├── scripts/
│   │   ├── install_task.ps1   # Register StartEDRMCP scheduled task
│   │   ├── start_server.ps1  # Start MCP server (with logs/, dup guard)
│   │   ├── stop_server.ps1  # Stop MCP server (port 8765 only)
│   │   ├── restart_server.ps1  # Restart
│   │   └── health.ps1         # Health check (port + MCP initialize)
│   │
│   ├── logs/                # server.stdout/stderr logs
│   │   ├── start.log        # startup metadata
│   │   └── server.*.log     # server stdout/stderr
│   │
│   └── screenshots/         # Screenshot output
│
├── config/
│   ├── targets.example.json # Repo template (no real passwords)
│   ├── targets.local.json  # Local config (gitignored, generated by --init)
│   └── test_machines.json  # DEPRECATED — do not use for new targets
│
└── test_case/
    ├── run_tests.py         # Test runner (--target supported)
    └── conftest.py          # McpClient, fixtures, target selection
```

---

## Target Scripts

All scripts use **dynamic path resolution** via `$PSScriptRoot` — no hardcoded
`D:\skill\...` paths. Target root is always `scripts/`'s parent directory.

### `install_task.ps1`

Registers `StartEDRMCP` in Windows Task Scheduler.

```
Task: StartEDRMCP
  Trigger:  Manual (schtasks /Run /TN StartEDRMCP /I)
  Action:   powershell.exe -NoProfile -ExecutionPolicy Bypass
            -File "<target>\scripts\start_server.ps1"
  Start in: <target>
  User:     Only when user is logged on (interactive session)
  Privilege: Highest
```

### `start_server.ps1`

Launches the MCP server inside the logged-on user's interactive session.

1. Dynamically resolve target root from `$PSScriptRoot`
2. Load `config.json` for Python path and port
3. Check port 8765 — skip if already listening (no duplicate start)
4. Set required env vars: `EDR_WD_ENABLE_PYWINAUTO=1`
5. Start `python server.py --http --host 0.0.0.0 --port 8765`
6. Log to `logs/start.log` (PID, timestamp) + `logs/server.*.log` (stdout/stderr)

### `stop_server.ps1`

Stops only the process listening on port 8765. Never kills all Python processes.

### `health.ps1`

```
1. Port 8765 listening check
2. HTTP POST /mcp MCP initialize probe
   → must return Mcp-Session-Id header
   → prints [OK] or [FAIL]
```

---

## MCP Endpoint & Transport

**Endpoint:** `http://<host>:<port>/mcp` (NOT root `/`)

**Transport:** FastMCP 3.x Streamable HTTP
- Method: `POST` (all requests)
- Headers: `Content-Type: application/json`, `Accept: application/json, text/event-stream`
- Session: `Mcp-Session-Id` header returned by server, sent back by client
- Protocol version: `2025-03-26`

**Connection priority:**
1. Direct: `http://170.170.11.26:8765/mcp` (preferred)
2. Tunnel fallback: `http://localhost:18765/mcp` (if direct is unreachable)

**`mcp.host` vs `ssh.host`:**
- `mcp.host` (e.g. `0.0.0.0`) is the **server bind address** — what the server listens on
- `ssh.host` (e.g. `170.170.11.26`) is the **agent connection address** — how the agent reaches the server
- In `direct` mode, the agent uses `ssh.host` to build the MCP URL, not `mcp.host`

---

## MCP Tools

### GUI Tools

| Tool | Description |
|------|-------------|
| `connect` | Connect to a window by title regex, process name, or PID |
| `dump_tree` | Dump the full control tree of the connected window |
| `click` | Click by control_id, text, class_name, automation_id, etc. |
| `click_target` | Click matched control centre (uses mouse.click coords) |
| `click_at` | Click absolute screen coordinates (x, y) |
| `click_window_at` | Click window-relative coordinates |
| `type_text` | Type text into an edit control |
| `select` | Select a combo box item by text or index |
| `get_text` | Read text from a control |
| `screenshot` | Take a screenshot (save to `screenshots/` or base64) |
| `restore_edr` | Restore the EDR window if minimized |

### Status Tools

| Tool | Description |
|------|-------------|
| `status` | Return server health: PID, port, backend, session |
| `list_windows` | List all top-level windows (no connect required) |
| `is_window_open` | Check if a window matching criteria exists |
| `wait_window` | Poll until a window appears or timeout |

### PowerShell Tools (always enabled via start_server.ps1)

| Tool | Description |
|------|-------------|
| `run_powershell` | Run PowerShell synchronously, return stdout/stderr |
| `start_powershell` | Start PowerShell as background job, return job_id |
| `get_job` | Poll a background job result |
| `cancel_job` | Cancel a running PowerShell job |

### activate_edr

Launch or activate the HisecEndpoint GUI:
`activate_edr(exe_path=None, wait=True, timeout=15.0)`

---

## agent/target_manager.py API

### `check_server_health(name=None) -> dict`

TCP reachability probe — **does not require SSH auth**.

```python
{
  "ok": True,
  "target": "win-dev",
  "stage": "health_check",
  "data": {
    "port_open": True,
    "mcp_responding": None,   # delegated to mcp_manager
    "ready": True,
    "ready_level": "tcp_only", # MCP initialize is mcp_manager's job
    "mcp_url": "http://170.170.11.26:8765/mcp",
    "check_host": "170.170.11.26",
    "check_port": 8765
  }
}
```

### `ensure_server_running(name=None) -> dict`

Ensure MCP server TCP port is listening. Uses `get_target()` (no auth) for the
TCP check; only calls `get_resolved_target()` (requires auth) if server needs to be started.

```python
{
  "ok": True,
  "target": "win-dev",
  "stage": "ensure",
  "data": {
    "status": "already_running",   # or "started"
    "port": 8765,
    "ready_level": "tcp_only",     # mcp_ready is from mcp_manager.initialize()
    "note": "MCP initialize handled by mcp_manager",
    "mcp_url": "http://170.170.11.26:8765/mcp"
  }
}
```

### `list_targets() -> dict`

Returns all targets and which one is default.

---

## agent/mcp_manager.py API

### `initialize(name=None) -> dict`

Performs MCP initialize handshake. **Does not call `get_resolved_target()`** —
only needs `TargetConfig.build_mcp_url(name)` which requires no SSH credentials.

```python
{
  "ok": True,
  "target": "win-dev",
  "stage": "mcp_initialize",
  "data": {
    "session_id": "62ee9cf7f72046a1...",
    "mcp_url": "http://170.170.11.26:8765/mcp",
    "protocol_version": "2025-03-26",
    "ready_level": "mcp_ready"
  }
}
```

### `get_mcp_tools(session_id, mcp_url) -> dict`

Calls `tools/list` on the given session.

### `call_mcp_tool(session_id, mcp_url, tool_name, arguments=None) -> dict`

Calls an MCP tool on the given session.

### Session caching

`mcp_initialize()` results are cached **per target** in `conftest.py`. Repeated
calls for the same target return the cached session. Different targets get
independent sessions.

---

## test_case/conftest.py API

### `get_target_name() -> str`

Returns the effective target name: CLI `--target` > `EDR_WD_TARGET` env var >
`default_target` in config.

### `ensure_server_running(target=None) -> (bool, str)`

Wrapper around `target_manager.ensure_server_running()`. Returns `(ok, message)`.

### `mcp_initialize(target=None) -> dict`

Wrapper around `mcp_manager.initialize()`. Cached per target.

### `McpClient`

JSON-RPC-over-HTTP client using FastMCP 3.x Streamable HTTP transport.

```python
# Preferred: pass pre-initialized session
client = McpClient(mcp_init_result=init_result)

# Or: let it resolve target and initialize
client = McpClient(target="win-dev")

# Low-level debug only:
client = McpClient(base_url="http://170.170.11.26:8765/mcp")
```

Priority: `mcp_init_result` > `target` > `base_url`. Mixing `base_url` with
`target` or `mcp_init_result` raises `ValueError`.

---

## Running Tests

All commands run from the repository root.

```bash
# Linux/Mac: Run all tests with default target
python3 test_case/run_tests.py

# Linux/Mac: Run with explicit target
python3 test_case/run_tests.py --target win-dev

# Windows PowerShell: Run with explicit target
python test_case\run_tests.py --target win-dev -v

# Windows PowerShell: via environment variable
$env:EDR_WD_TARGET = "win-dev"
python test_case\run_tests.py -v
```

### Test flow

```
target_manager.ensure_server_running(target)
    → TCP port open (ready_level: "tcp_only")

mcp_manager.initialize(target)
    → MCP session ready (ready_level: "mcp_ready")

McpClient(mcp_init_result=init_result)
    → run tests...
```

---

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `EDR_WD_TARGET` | `default_target` in config | Target name to use |
| `EDR_WD_WIN_DEV_PASSWORD` | from config | SSH password for win-dev |
| `EDR_WD_WIN_PROD_PASSWORD` | from config | SSH password for win-prod |

Note: `EDR_WD_ENABLE_PYWINAUTO=1` is set automatically by `start_server.ps1`
— no need to set it manually.

---

## Windows Agent

The agent side (`agent/`) runs on both Linux/Mac and Windows agents. Target is always Windows.

### Auth requirements

| Agent OS | Recommended auth | Notes |
|----------|-----------------|-------|
| Linux / Mac | password (`sshpass`) or key | both work |
| Windows | **key only** | `sshpass` is not available on Windows |

On Windows agent, if `auth.type=password` is used and `sshpass` is not found, the error message will be:

```
Command not found: sshpass. sshpass is not available on Windows agent.
Use key auth instead (set auth.type='key' in config).
```

### OpenSSH on Windows

Windows agent requires OpenSSH Client (not PowerShell remoting):

```powershell
# Check if OpenSSH is installed
ssh -V

# Install if missing
Get-WindowsCapability -Online | Where-Object Name -like 'OpenSSH.Client*'
Add-WindowsCapability -Online -Name OpenSSH.Client~~~~0.0.1.0
```

### Key auth setup on Windows

1. Generate a key (as the agent user):
   ```powershell
   ssh-keygen -t ed25519 -f $env:USERPROFILE\.ssh\id_edr_wd
   ```
2. Copy the public key to the target (step 1: upload, step 2: append):
   ```powershell
   # Step 1: upload the public key to a temp location on the target
   scp -P 22 $env:USERPROFILE\.ssh\id_edr_wd.pub admin@<target-ip>:C:\Users\admin\id_edr_wd.pub

   # Step 2: append it to authorized_keys via PowerShell on the target
   ssh -p 22 admin@<target-ip> `
     'powershell -NoProfile -Command "New-Item -ItemType Directory -Force $env:USERPROFILE\.ssh; Get-Content $env:USERPROFILE\id_edr_wd.pub | Add-Content $env:USERPROFILE\.ssh\authorized_keys"'
   ```
3. Verify the key works:
   ```powershell
   ssh -i $env:USERPROFILE\.ssh\id_edr_wd -p 22 admin@<target-ip> hostname
   ```
4. Set `auth.type=key` and `auth.key_path="C:\Users\<username>\.ssh\id_edr_wd"` in `targets.local.json`.

### Running tests on Windows agent

```powershell
# From the edr-wd root directory
python test_case\run_tests.py --target win-dev -v

# Or via environment variable
$env:EDR_WD_TARGET = "win-dev"
python test_case\run_tests.py -v
```

Note: use `python` (or `py`) on Windows, not `python3`.

### Path separator

- Windows agent uses backslash `\` in config files (`target_root`, `key_path`).
- Agent code uses `pathlib.Path` which handles both `/` and `\` via `os.sep`.
- Remote paths (target is always Windows) use backslash in SCP/SSH commands.

---

## Deployment Flow

### First-time setup (once per target)

```
1. Agent: copy edr-wd to Windows (git / scp / share)
2. Agent: install_target_task() → registers StartEDRMCP
3. User: log into Windows desktop interactively (so Task Scheduler has a session)
```

### Daily use

```
1. Agent: ensure_server_running() → ensures TCP port is listening (tcp_only)
2. Agent: mcp_manager.initialize() → MCP handshake (mcp_ready)
3. Agent: call MCP tools to automate EDR GUI
```

---

## Troubleshooting

### "MCP server not reachable"

1. Check port: `telnet 170.170.11.26 8765`
2. Check server process on Windows:
   ```powershell
   Get-NetTCPConnection -LocalPort 8765 -State Listen
   ```
3. If not listening, trigger manually:
   ```powershell
   .\target\scripts\start_server.ps1
   ```
4. Check logs: `Get-Content target/logs/start.log` and `target/logs/server.*.log`

### "PowerShell disabled"

This should not happen with the current `start_server.ps1`. If seen,
confirm `EDR_WD_ENABLE_PYWINAUTO=1` is set in the server environment.

### "connect timeout — no window found"

- The EDR window must be open before `connect()` is called
- Use `is_window_open()` or `wait_window()` first to wait for the window

### SSH tunnel drops

```bash
bash agent/tunnel.sh stop
bash agent/tunnel.sh start
```

### Port 8765 already in use

```powershell
.\target\scripts\stop_server.ps1
```

Or manually:
```powershell
Get-NetTCPConnection -LocalPort 8765 -State Listen | Stop-Process -Force
```

### Task Scheduler task not found

Re-run installation:
```python
from agent.target_manager import TargetManager
TargetManager().install_target_task()
```

---

## Rejected Patterns

**Do NOT do this** — runs in non-interactive SSH session, pywinauto won't work:

```powershell
ssh target "python server.py --http --port 8765"
```

**Do NOT do this** — kills ALL Python processes:

```powershell
Get-Process python | Stop-Process -Force
```

**Do NOT use root path** — FastMCP 3.x uses `/mcp` endpoint:

```
http://170.170.11.26:8765/     ✗
http://170.170.11.26:8765/mcp  ✓
```

---

## Test Results

### Current status (Phase 4)

```
Integration Tests:  5 passed
E2E EDR Workflow:  4 passed / 6 failed (GUI runtime / EDR state)

Failed steps are GUI-layer issues (EDR application state, RDP session),
not multi-target architecture problems.
```

Run with: `cd test_case && python3 run_tests.py --target win-dev -v`

---

## Future

- `exe` packaging: replace `python server.py` with `target/bin/edr-mcp-server.exe`
- Launcher: a long-running process that keeps the MCP server alive
- Status page: HTTP endpoint that returns structured health info
- GUI-layer E2E stability: investigate `activate_edr`, `screenshot`, `restore_edr` failures
- Windows agent: key-auth docs and guidance added; runtime validation pending
