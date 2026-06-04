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
│  ┌─────────────────────┐     ┌──────────────────────────────┐  │
│  │  hermes / openclaw  │────▶│  agent/mcp_manager.py        │  │
│  │                     │     │  - ensure_server_running()   │  │
│  │  skill / tools      │     │  - check_server_health()     │  │
│  └─────────────────────┘     │  - install_target_task()      │  │
│                               └──────────────┬───────────────┘  │
│                                               │ SSH + schtasks   │
│                                               ▼                  │
│  ┌─────────────────────┐            ┌──────────────────────┐   │
│  │  SSH tunnel (optional)│           │  Windows target       │   │
│  │  :18765 → :8765      │──────────▶│  0.0.0.0:8765        │   │
│  └─────────────────────┘   direct   └──────────┬───────────┘   │
│                                                  │ fastmcp 3.x  │
└──────────────────────────────────────────────────│──────────────┘
                                                 │ Streamable HTTP
                                                 ▼
                              ┌──────────────────────────────────────┐
                              │  target/                             │
                              │    server.py          ← MCP server  │
                              │    pywinauto_client.py              │
                              │    config.json        ← SSH/server  │
                              │    scripts/                          │
                              │      install_task.ps1 (Task Sched)  │
                              │      start_server.ps1  (launcher)  │
                              │      stop_server.ps1                │
                              │      restart_server.ps1            │
                              │      health.ps1                     │
                              └──────────────────┬─────────────────┘
                                                  │ pywinauto UIA
                                                  ▼
                                    HiSecEndpoint GUI (华为)
```

**Key principle:** The agent never starts Python over SSH. Instead it triggers
`schtasks /Run /TN StartEDRMCP` which launches `start_server.ps1` in the
logged-on interactive desktop session.

---

## Configuration — `target/config.json`

All connection and server settings live in one file. No hardcoded paths or
credentials in scripts.

```json
{
  "ssh": {
    "host": "170.170.11.26",
    "port": 22,
    "user": "admin",
    "password": "whl@123"
  },
  "server": {
    "python_path": "C:\\Program Files\\Python313\\python.exe",
    "host": "0.0.0.0",
    "port": 8765,
    "command": "server.py --http --host 0.0.0.0 --port 8765"
  },
  "task": { "name": "StartEDRMCP" },
  "connection": {
    "preferred": "direct",
    "direct_url": "http://170.170.11.26:8765/mcp",
    "tunnel_url": "http://localhost:18765/mcp"
  }
}
```

- `connection.preferred`: `"direct"` (try 170.170.11.26 first) or `"tunnel"` (tunnel first)
- Both `agent/mcp_manager.py` and `test_case/conftest.py` read this file
- Environment variables (`EDR_WD_HOST`, `EDR_WD_USER`, `EDR_WD_PASS`) override config.json

---

## Quick Start

### 1. One-time task installation

```bash
# From Mac — uploads scripts and registers the scheduled task:
python -c "from agent.mcp_manager import install_target_task; print(install_target_task())"
# → {'ok': True}
```

Or on Windows directly:
```powershell
powershell -ExecutionPolicy Bypass -File target/scripts/install_task.ps1
```

### 2. Start MCP server

```bash
# From Mac — triggers StartEDRMCP via schtasks:
python -c "from agent.mcp_manager import ensure_server_running; print(ensure_server_running())"
# → {'ok': True, 'session': '...', 'already_running': False, 'url': 'http://170.170.11.26:8765/mcp'}
```

Or on Windows:
```powershell
.\target\scripts\start_server.ps1
```

### 3. Run tests

```bash
cd test_case && python3 run_tests.py -v
# → Results: 16 passed, 0 failed
```

---

## Directory Structure

```
edr-wd/
├── SKILL.md
│
├── agent/                    # Agent-side (runs on Mac/Linux)
│   ├── mcp_manager.py       # ensure_server_running, trigger, health, config loader
│   └── tunnel.sh             # SSH tunnel manager (optional)
│
├── target/                    # Target-side (runs on Windows)
│   ├── server.py            # MCP server entry (fastmcp 3.x + pywinauto)
│   ├── pywinauto_client.py  # WindowsGUI class
│   ├── config.json          # SSH / server / connection config
│   │
│   ├── scripts/
│   │   ├── install_task.ps1   # Register StartEDRMCP scheduled task
│   │   ├── start_server.ps1   # Start MCP server (with logs/, dup guard)
│   │   ├── stop_server.ps1    # Stop MCP server (port 8765 only)
│   │   ├── restart_server.ps1  # Restart
│   │   └── health.ps1         # Health check (port + MCP initialize)
│   │
│   ├── logs/                # server.stdout/stderr logs
│   │   ├── start.log        # startup metadata
│   │   └── server.*.log     # server stdout/stderr
│   │
│   └── screenshots/         # Screenshot output
│
├── test_case/
│   ├── run_tests.py         # Test runner
│   └── conftest.py          # McpClient (Streamable HTTP), fixtures
│
└── config/
    └── test_machines.json   # Per-machine test config
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
4. Set required env vars: `EDR_WD_ENABLE_POWERSHELL=1`, `EDR_WD_ENABLE_PYWINAUTO=1`
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
- Protocol version: `2025-11-25`

**Connection priority:**
1. Direct: `http://170.170.11.26:8765/mcp` (preferred)
2. Tunnel fallback: `http://localhost:18765/mcp` (if direct is unreachable)

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

## agent/mcp_manager.py API

### `check_server_health() -> dict`

Lightweight probe — port open + MCP initialize. No side effects.

```python
{"ok": True, "port_open": True, "mcp_ok": True, "session": "...", "url": "http://170.170.11.26:8765/mcp"}
```

### `ensure_server_running() -> dict`

Full lifecycle manager — reads `target/config.json` automatically:

1. Resolve MCP URL (direct first, tunnel fallback)
2. If server already healthy → return immediately
3. Trigger `schtasks /Run /TN StartEDRMCP /I` on target
4. Poll until MCP initialize succeeds (max 60s, 3s interval)
5. On timeout: read `logs/start.log` + latest `logs/server.*.log` for diagnostics
6. Return `{"ok": True, "session": "...", "already_running": False, "url": "..."}`
   or `{"ok": False, "stage": "...", "error": "...", "start_log": "...", "server_log": "..."}`

### `install_target_task() -> dict`

Uploads and runs `install_task.ps1` on the target via SSH.
Returns `{"ok": True}` on success.

### `trigger_target_server() -> CompletedProcess`

Fire `schtasks /Run /TN StartEDRMCP /I` directly. Does NOT wait.

---

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `EDR_WD_HOST` | from config.json | Windows target IP |
| `EDR_WD_USER` | from config.json | SSH username |
| `EDR_WD_PASS` | from config.json | SSH password |
| `EDR_WD_CONN_PREF` | `"direct"` | `"direct"` or `"tunnel"` |

Note: `EDR_WD_ENABLE_POWERSHELL=1` is set automatically by `start_server.ps1`
— no need to set it manually.

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
1. Agent: ensure_server_running() → triggers StartEDRMCP if needed
2. Agent: MCP initialize → verify server ready
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
confirm `EDR_WD_ENABLE_POWERSHELL=1` is set in the server environment.

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
from agent.mcp_manager import install_target_task
install_target_task()
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

```
Integration Tests:  5 passed
E2E EDR Workflow:  11 passed
────────────────────────────────
Total:              16 passed, 0 failed
```

Run with: `cd test_case && python3 run_tests.py -v`

---

## Future

- `exe` packaging: replace `python server.py` with `target/bin/edr-mcp-server.exe`
- Launcher: a long-running process that keeps the MCP server alive
- Status page: HTTP endpoint that returns structured health info
