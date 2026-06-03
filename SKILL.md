---
name: edr-wd
description: |
  Windows EDR (HiSecEndpoint) GUI automation via MCP. Controls HiSecEndpoint
  using pywinauto/UIA — clicks, tree dumps, typing, screenshots, and more.

  Trigger scenarios:
  (1) Automating Windows desktop apps (HiSecEndpoint, etc.)
  (2) Reading window control trees and operating on controls
  (3) Remote control of Windows GUI via MCP over SSH tunnel
  (4) Any MCP client (OpenClaw / Hermes / Codex / Claude Desktop / custom)
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
│  │  hermes / openclaw  │────▶│  mcp_manager.py             │  │
│  │                     │     │  - ensure_server_running()   │  │
│  │  skill / tools      │     │  - trigger_target_server()  │  │
│  └─────────────────────┘     │  - install_target_task()    │  │
│                                └──────────────┬───────────────┘  │
│                                               │ SSH + schtasks  │
│                                               ▼                 │
│  ┌─────────────────────┐            ┌──────────────────────┐  │
│  │  tunnel.sh           │            │  Windows target       │  │
│  │  (SSH LocalForward)  │──────────▶│  127.0.0.1:8765     │  │
│  │  :18765 → :8765      │            └──────────┬───────────┘  │
│  └─────────────────────┘                       │              │
└─────────────────────────────────────────────────│──────────────┘
                                                │ fastmcp
                                                ▼
                              ┌──────────────────────────────────────┐
                              │  target/                             │
                              │    server.py          ← MCP server  │
                              │    pywinauto_client.py               │
                              │    scripts/                          │
                              │      install_task.ps1  (Task Sched)  │
                              │      start_server.ps1                │
                              │      stop_server.ps1                 │
                              │      restart_server.ps1             │
                              │      health.ps1                      │
                              └──────────────────┬─────────────────┘
                                                  │ pywinauto UIA
                                                  ▼
                                    HiSecEndpoint GUI (华为)
```

**Key principle:** The agent never starts Python over SSH. Instead it triggers
`schtasks /Run /TN StartEDRMCP` which launches `start_server.ps1` in the
logged-on interactive desktop session.

---

## Quick Start

### 1. One-time target setup

```bash
# SSH into Windows and run the task installer:
ssh whl@192.168.3.23
powershell -ExecutionPolicy Bypass -File target/scripts/install_task.ps1

# Or from Mac (agent side), trigger the install remotely:
python -m agent.mcp_manager --install-task
```

### 2. Start the tunnel

```bash
# From this skill's directory:
bash agent/tunnel.sh start

# Verify:
bash agent/tunnel.sh test
```

### 3. Ensure server is running (agent-side)

```python
from agent.mcp_manager import ensure_server_running, check_server_health

# Fast health check (no trigger):
health = check_server_health(local_port=18765)
print(health)
# {'ok': True, 'port_open': True, 'mcp_ok': True, 'session': '...'}

# Ensure server is up (triggers StartEDRMCP if down):
result = ensure_server_running(local_port=18765, host="192.168.3.23",
                               user="whl", pass_file="~/.ssh/.tunnelpass")
print(result)
# {'ok': True, 'session': '...', 'already_running': False}
```

---

## Directory Structure

```
edr-wd/
├── SKILL.md
│
├── agent/                    # Agent-side (runs on Mac/Linux)
│   ├── mcp_manager.py       # ensure_server_running, trigger, health
│   ├── tunnel.sh             # SSH tunnel manager
│   ├── setup-mac.sh          # Mac setup helper
│   └── edr-wd.sh            # Legacy entry script
│
└── target/                   # Target-side (runs on Windows)
    ├── server.py            # MCP server entry (fastmcp + pywinauto)
    ├── pywinauto_client.py  # WindowsGUI class
    ├── __init__.py
    ├── config.json          # (optional) target config
    │
    ├── scripts/
    │   ├── install_task.ps1   # Install StartEDRMCP scheduled task
    │   ├── start_server.ps1   # Start MCP server in interactive session
    │   ├── stop_server.ps1    # Stop MCP server (port 8765 only)
    │   ├── restart_server.ps1  # Restart
    │   └── health.ps1         # Health check (port + MCP initialize)
    │
    ├── logs/                # Server stdout/stderr logs
    │   └── edr-wd.*.log
    └── screenshots/         # Screenshot output
```

---

## Target Scripts

### install_task.ps1

Installs the `StartEDRMCP` Windows Task Scheduler task. Must run in an
interactive desktop session (RDP or Console).

```
Task: StartEDRMCP
  Trigger: Manual (schtasks /Run /TN StartEDRMCP)
  Run only when user is logged on (LogonType: Interactive)
  Run with highest privileges
```

### start_server.ps1

Runs inside the user's interactive session (via the scheduled task).

1. Create `logs/` and `screenshots/` if absent.
2. Check port 8765 — skip if already listening.
3. Start `python -m server --http --port 8765` with stdout/stderr → `logs/`.

### stop_server.ps1

Stop only the process listening on port 8765. Never kills all Python processes.

### health.ps1

1. Port 8765 listening check.
2. HTTP MCP initialize probe — must return `Mcp-Session-Id` header.

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
| `screenshot` | Take a screenshot (base64 or save to file) |
| `restore_edr` | Restore the EDR window if minimized |

### Status Tools

| Tool | Description |
|------|-------------|
| `status` | Return server health: PID, port, backend, can_see_hisec_agent, session |
| `list_windows` | List all top-level windows (no connect required) |
| `is_window_open` | Check if a window matching criteria exists |
| `wait_window` | Poll until a window appears or timeout |

### PowerShell Tools (requires EDR_WD_ENABLE_POWERSHELL=1)

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

## Agent Manager API

### `check_server_health(local_port=18765) -> dict`

Lightweight probe — port open + MCP initialize. No side effects.

```python
{"ok": True, "port_open": True, "mcp_ok": True, "session": "..."}
```

### `ensure_server_running(local_port, host, user, pass_file) -> dict`

Full lifecycle manager:

1. If server already healthy → return immediately.
2. Trigger `schtasks /Run /TN StartEDRMCP` on target.
3. Poll until MCP initialize succeeds (max 60s).
4. Return `{"ok": True, "session": "...", "already_running": False}` on success.
5. Return `{"ok": False, "stage": "wait_mcp_ready", "error": "..."}` on failure.

### `trigger_target_server(host, user, pass_file) -> CompletedProcess`

Fire `schtasks /Run /TN StartEDRMCP` directly. Does NOT wait for server to start.

### `install_target_task(host, user, pass_file) -> dict`

Run `install_task.ps1` on the target via SSH. One-time setup.

---

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `EDR_WD_HOST` | `192.168.3.23` | Windows target IP |
| `EDR_WD_USER` | `whl` | SSH username |
| `EDR_WD_LOCAL_PORT` | `18765` | Local SSH tunnel port |
| `EDR_WD_REMOTE_PORT` | `8765` | Remote MCP server port |
| `EDR_WD_ENABLE_POWERSHELL` | `0` | Enable PowerShell tools (1=enable) |

---

## Deployment Flow

### First-time setup

```
1. Agent: sync edr-wd to target (git clone or rsync)
2. Agent: run install_task.ps1 on target (one-time)
3. User: log into Windows desktop once (so Task Scheduler has a session)
```

### Daily use

```
1. Agent: start tunnel.sh
2. Agent: check_server_health() — if OK, skip to 5
3. Agent: ensure_server_running() → triggers StartEDRMCP via schtasks
4. Agent: poll until MCP initialize succeeds
5. Agent: call MCP tools to automate EDR GUI
```

---

## Troubleshooting

### MCP server won't start

- Check `target/logs/edr-wd.*.log` on Windows for stderr.
- Confirm Windows user is logged into an interactive desktop.
- Run `target/scripts/health.ps1` manually on Windows.

### schtasks /Run succeeds but server never starts

- Task may be running in a different session.
- Re-run `install_task.ps1` from an RDP session with the correct user.

### SSH tunnel drops

```bash
bash agent/tunnel.sh stop
bash agent/tunnel.sh start
```

### Port 8765 already in use

Run `target/scripts/stop_server.ps1` on Windows, or find and stop the rogue process:

```powershell
Get-NetTCPConnection -LocalPort 8765 -State Listen
```

---

## Rejected Patterns

**Do NOT do this** — runs in non-interactive SSH session, pywinauto won't work:

```powershell
ssh target "python -m server --http --port 8765"
```

**Do NOT do this** — kills ALL Python processes, including unrelated ones:

```powershell
Get-Process python | Stop-Process -Force
```

---

## Future

- `exe` packaging: replace `python server.py` with `target/bin/edr-mcp-server.exe`
- Launcher: a long-running process that keeps the MCP server alive
- Status page: HTTP endpoint that returns structured health info
