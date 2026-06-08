# edr-wd Cross-Platform Architecture Design

## 1. Design Goals

`edr-wd` must support four agent × target combinations:

```
Windows agent → Windows target
Windows agent → macOS target
macOS agent   → Windows target
macOS agent   → macOS target
```

Core principles:

```
1. Agent OS and target OS are decoupled.
2. Agent handles connection, deployment, startup, and MCP calls only.
3. Target handles real GUI automation.
4. Windows target uses windows_pywinauto backend.
5. macOS target uses macos_accessibility backend.
6. HiSec/EDR automation is split by target OS into profiles:
     - windows_hisec
     - macos_hisec
7. Parallels Windows VM internal windows are a Windows target, not macOS.
```

---

## 2. Four-Layer Model

```
Agent Layer
  - Runs on Windows or macOS
  - Reads target_config
  - Connects via SSH/MCP
  - Does NOT directly automate GUI

Connection Layer
  - SSH/SFTP, MCP HTTP
  - password/key auth
  - tunnel/direct mode

Target Lifecycle Layer
  - Windows lifecycle / macOS lifecycle
  - Deploys target/
  - Starts/stops MCP server

Target Automation Layer
  - windows_pywinauto
  - macos_accessibility
  - Real window enumeration, click, screenshot, dump tree
```

Correct call chain:

```
agent
  → target_config
  → ssh_runner
  → lifecycle/<platform>.py
  → target/server.py
  → target/automation/<backend>.py
```

Agent must NOT assume target GUI behavior based on agent OS.

---

## 3. Support Matrix

| Agent OS | Target OS | Platform | Lifecycle | Backend | Profile |
|----------|-----------|----------|-----------|---------|---------|
| Windows  | Windows   | `windows` | `lifecycle/windows.py` | `windows_pywinauto` | `windows_hisec` |
| Windows  | macOS     | `macos`  | `lifecycle/macos.py`  | `macos_accessibility` | `macos_hisec` |
| macOS    | Windows   | `windows` | `lifecycle/windows.py` | `windows_pywinauto` | `windows_hisec` |
| macOS    | macOS     | `macos`  | `lifecycle/macos.py`  | `macos_accessibility` | `macos_hisec` |

Key rule: Agent OS only affects SSH/SFTP implementation details. Target OS determines lifecycle and automation backend.

---

## 4. Agent Layer

### Agent Responsibilities

**Does:**
- Load targets.local.json
- Resolve target name and auth
- SSH connect to target
- Deploy target/
- Start target MCP server
- Call MCP tools
- Collect test results

**Does NOT:**
- Directly operate target GUI
- Choose GUI backend based on agent OS
- Operate Parallels Windows VM internal windows from macOS agent
- Assume target is same OS as agent

### Agent OS Differences

**Windows agent:** Paramiko password auth by default, OpenSSH key auth compatibility, no sshpass, PowerShell local execution, Windows path handling.

**macOS agent:** Paramiko password auth by default, OpenSSH key auth compatibility, Unix path handling, launchctl management for remote macOS targets.

**Unified:** All agents use `agent/ssh_runner.py` — caller does NOT care whether underlying transport is Paramiko or OpenSSH.

---

## 5. Target Layer

### Target Responsibilities

**Does:**
- Run target/server.py
- Load correct automation backend
- Provide MCP tools
- Execute real GUI automation
- Return structured results

**Does NOT:**
- Read agent local config
- Know agent OS
- Handle other OS GUI

### Windows Target

**Backend:** `windows_pywinauto`

**Valid for:**
- Windows physical machine
- Windows VM (Parallels, VMware, Hyper-V)
- RDP Windows session

**HiSec automation targets:**
- HisecEndpointAgent.exe
- EDRClient.exe
- 华为智能终端安全系统
- 华为HiSec Endpoint
- 终端安全 / 设备

**Critical requirement:** MCP server must run in **interactive desktop session**, not Session 0, not SSH non-interactive session.

**Must verify:**
```
server.py process exists AND
8765 port open AND
pywinauto can see windows AND
list_windows returns real Windows windows AND
dump_tree returns real UIA control tree
```

**Forbidden:** Task Scheduler without interactive session, SSH non-interactive session, background Start-Process where parent exit kills server.

**Recommended:** Use `agent/lifecycle/windows.py` and `target/scripts/start_server.ps1`.

### macOS Target

**Backend:** `macos_accessibility`

**Valid for:**
- Mac physical machine
- Mac VM
- Mac mini / Mac desktop
- Target with active macOS GUI session

**HiSec automation targets:**
- /Applications/HiSecEndpoint.app
- HiSecEndpointAgent
- EDRClient
- 华为智能终端安全系统
- 华为HiSec Endpoint / HiSec Endpoint

**Critical requirement:** MCP server must run in **GUI session via LaunchAgent**, not SSH/nohup.

**Forbidden as formal runtime:**
```
ssh <host> 'nohup python server.py &'
ssh <host> 'python server.py &'
ssh <host> 'python server.py'  (without LaunchAgent)
```

**Required:** `launchctl bootstrap gui/<uid>` / `launchctl kickstart gui/<uid>/...`

**Permissions required:**
- Accessibility
- Screen Recording
- Automation / System Events
- LaunchAgent user must match GUI login session

---

## 6. Parallels Windows VM

Parallels Windows VM is **NOT** part of macOS target. It is a Windows target.

```
platform = windows
app_profile = windows_hisec
backend = windows_pywinauto
NOT macos_accessibility
```

**Wrong path:**
```
mac-dev → macOS Accessibility → Windows App proxy window
```

**Correct path:**
```
agent
  → parallels-win (Windows VM)
    → Windows MCP server
      → windows_pywinauto
        → VM internal real EDR windows
```

**prlctl responsibilities (bootstrap only):**
- Confirm VM is running
- Get VM IP
- Copy files into VM
- Help start MCP server inside VM

**prlctl is NOT a GUI automation backend.**

---

## 7. Profiles

### macos_generic

**Purpose:** Verify Mac target basic GUI capability.

**Tests:**
```
tools/list, status, list_windows, activate_app Finder,
is_window_open Finder, connect by process_name, click_at dry-run
screenshot: optional / skip
```

**Status:** PASSED

---

### macos_hisec

**Purpose:** Verify Mac native HiSecEndpoint.app automation.

**Target windows:**

```
main:
  owner/process: HiSecEndpointAgent
  title candidates:
    - 华为智能终端安全系统
    - 华为安全防护中心
    - BaselineUIMainWindow

client:
  owner/process: EDRClient
  title candidates:
    - 华为HiSec Endpoint
    - HiSec Endpoint
```

**Must NOT match:**
```
Windows App | 设备
Windows App | <IP>
Parallels VM proxy window
```

**Pass criteria:**
```
backend = macos_accessibility
server running in GUI session
HiSecEndpointAgent process_found = true
main.window_found = true
EDRClient process_found = true
client.window_found = true
activate_edr.ok = true
```

**Status:** NOT VALIDATED (requires LaunchAgent GUI session)

---

### windows_hisec

**Purpose:** Verify Windows target HiSec/EDR automation.

**Target processes:**
```
HisecEndpointAgent.exe
EDRClient.exe
```

**Target windows:**
```
华为智能终端安全系统
华为HiSec Endpoint
终端安全
设备
```

**Pass criteria:**
```
backend = windows_pywinauto
server running in interactive desktop session
HisecEndpointAgent.exe process_found = true
main.window_found = true
EDRClient.exe process_found = true
client.window_found = true
dump_tree works
click works
```

**Status:** Windows VM target NOT DEPLOYED

---

## 8. Configuration

### Target Schema

Every target must contain:

```json
{
  "platform": "windows | macos",
  "app_profile": "windows_hisec | macos_hisec | macos_generic",
  "ssh": {
    "host": "<TARGET_HOST>",
    "port": 22,
    "user": "<TARGET_USER>",
    "auth": {
      "type": "password",
      "password": "<TARGET_PASSWORD>"
    }
  },
  "mcp": {
    "host": "<TARGET_HOST>",
    "port": 8765,
    "path": "/mcp",
    "connect_mode": "direct"
  }
}
```

### Windows Target Example

```json
{
  "parallels-win": {
    "description": "Parallels Windows VM target",
    "platform": "windows",
    "app_profile": "windows_hisec",
    "ssh": {
      "host": "<WINDOWS_VM_IP>",
      "port": 22,
      "user": "<WINDOWS_USER>",
      "auth": {
        "type": "password",
        "password": "<TARGET_PASSWORD>"
      }
    },
    "mcp": {
      "host": "<WINDOWS_VM_IP>",
      "port": 8765,
      "path": "/mcp",
      "connect_mode": "direct"
    },
    "windows": {
      "target_root": "<WINDOWS_TARGET_ROOT>",
      "python_path": "python"
    }
  }
}
```

### macOS Target Example

```json
{
  "mac-dev": {
    "description": "macOS target",
    "platform": "macos",
    "app_profile": "macos_generic",
    "ssh": {
      "host": "<MAC_TARGET_IP>",
      "port": 22,
      "user": "<MAC_USER>",
      "auth": {
        "type": "password",
        "password": "<TARGET_PASSWORD>"
      }
    },
    "mcp": {
      "host": "<MAC_TARGET_IP>",
      "port": 8765,
      "path": "/mcp",
      "connect_mode": "direct"
    },
    "macos": {
      "target_root": "<MAC_TARGET_ROOT>",
      "python_path": "<MAC_PYTHON>"
    }
  }
}
```

### default_target Rules

Must verify before setting:
```
host is not a placeholder
user is not a placeholder
target_root is not a placeholder
auth.password is set
```

Otherwise return:
```json
{ "ok": false, "stage": "config_incomplete" }
```

---

## 9. Auth Design

### Default Policy

All targets use by default:
```json
{
  "type": "password",
  "password": "<TARGET_PASSWORD>"
}
```

For the current intranet workflow, inline password auth in ignored local config
is preferred. `password_env` and key auth are compatibility paths only. TODO:
harden credential storage if this leaves the trusted intranet setup.

### Agent Compatibility

**Windows agent:**
```
password auth → Paramiko
key auth     → OpenSSH
```

**macOS agent:**
```
password auth → Paramiko
key auth     → OpenSSH
```

**Unified entry:** `agent/ssh_runner.py`

### Security Requirements

**FORBIDDEN:**
```
cat targets.local.json
git diff targets.local.json
print(config)
sshpass -p "<password>"
```

**ALLOWED:**
```
auth.password: SET / NOT SET
password_env: compatibility fallback
host=<REDACTED>
user=<REDACTED>
target_root=<REDACTED>
```

---

## 10. Lifecycle Design

### Dispatch Rule

```python
if platform == "windows":
    lifecycle = WindowsLifecycle()
elif platform == "macos":
    lifecycle = MacOSLifecycle()
else:
    raise UnsupportedPlatform
```

**NOT based on agent OS.**

### Windows Lifecycle

Responsibilities:
- SSH/SFTP to Windows target
- Check Python
- Deploy target/
- Start server
- Confirm 8765 open
- Confirm backend=windows_pywinauto

### macOS Lifecycle

Responsibilities:
- SSH/SFTP to macOS target
- Check Python
- Deploy target/
- Install LaunchAgent
- bootstrap/kickstart gui/<uid>
- Confirm 8765 open
- Confirm backend=macos_accessibility

---

## 11. MCP Tool Design

### Universal Tools (all backends)

```
status, list_windows, is_window_open, wait_window, activate_app, click_at, screenshot
```

Unavailable → structured error, not NoneType.

### Windows-Only Tools

```
run_powershell, dump_tree, connect, click
```

Non-Windows backend must return:
```json
{
  "ok": false,
  "error": "run_powershell is only supported on Windows backend",
  "backend": "macos_accessibility"
}
```

### macOS-Specific Tools

```
activate_app, macOS Accessibility window inspection,
AppleScript/System Events based operations
```

Debug tools (e.g. `diagnose_windows`) must NOT be default-exposed. Only if `EDR_WD_ENABLE_DIAGNOSTICS=1` and results must be sanitized.

---

## 12. Testing Matrix

| Test profile | Windows target | macOS target |
|-------------|---------------|--------------|
| generic    | optional      | `macos_generic` |
| HiSec      | `windows_hisec` | `macos_hisec` |

### Agent × Target Combinations to Cover

```
Windows agent → Windows target
Windows agent → macOS target
macOS agent   → Windows target
macOS agent   → macOS target
```

Each combination must verify:
```
1. target_config resolve
2. auth resolve
3. SSH reachable
4. MCP initialize
5. status.backend correct
6. list_windows available
7. profile-specific smoke test
```

---

## 13. Error Classification

Must distinguish:
```
config_incomplete        — missing/placeholder config fields
auth_missing            — auth.password not set and password_env unresolved
ssh_failed              — cannot connect
target_not_deployed     — target/ not on remote
python_missing          — no Python on target
server_start_failed     — server.py failed to start
server_not_in_gui_session — macOS server not in LaunchAgent GUI
mcp_initialize_failed   — MCP handshake failed
backend_not_loaded      — automation backend import failed
permission_missing      — Accessibility/Screen Recording absent
window_not_found        — expected window not visible
process_not_found       — HiSecEndpointAgent/EDRClient not running
session0_detected       — Windows server in Session 0
unsupported_tool        — tool not available on this backend
```

Must NOT return:
```
NoneType object has no attribute ...
timeout
unknown
```

---

## 14. Privacy & Security Rules

**MUST NOT print:**
```
Real IP addresses
Real usernames
Real passwords
Real target_root paths
targets.local.json raw content
```

**MUST use:**
```
<TARGET_IP>, <TARGET_USER>, <TARGET_ROOT>, <REDACTED>
```

**MUST NOT commit:**
```
targets.local.json
.env files
Runtime logs
Temporary diagnostic scripts
Debug builds
```

---

## 15. Banned Patterns

```
1. macOS Accessibility → Parallels VM internal windows
2. SSH/nohup as formal macOS GUI server startup
3. run_powershell on macOS backend via shell wrappers
4. prlctl exec as GUI automation backend
5. process_found as window_found
6. Windows App proxy window as EDRClient
7. Temporary diagnostic scripts committed to repo
8. Debug tools default-exposed in MCP tools/list
9. Confusing mac-dev (macOS) with parallels-win (Windows)
10. Using agent OS to decide lifecycle/backend
```

---

## 16. Implementation Phases

### Phase 1: Config Layer Unification
- target_config supports agent × target matrix
- Unified target schema with password auth default
- Placeholder validation
- default_target protection

### Phase 2: Lifecycle Layer Unification
- Windows lifecycle independently completed
- macOS lifecycle independently completed
- Dispatch by target.platform only (not agent OS)

### Phase 3: P0 parallels-win
- Complete parallels-win config
- SSH verify Windows VM
- Deploy target/
- Start Windows MCP server
- Confirm backend=windows_pywinauto
- Run windows_hisec

### Phase 4: P1 macos_hisec
- Mac server switched to LaunchAgent GUI session
- Run macos_generic regression
- Fix Mac native HiSec window detection
- Register macos_hisec
- Run macos_hisec

### Phase 5: Four-Quadrant Regression
```
Windows agent → Windows target
Windows agent → macOS target
macOS agent   → Windows target
macOS agent   → macOS target
```

---

## 17. Final Principles

```
Agent OS determines SSH/SFTP implementation details
Target OS determines lifecycle and automation backend
App profile determines EDR operation flow
VM internal windows must be handled as VM target
Mac native windows must be handled as Mac target
These two must never be mixed
```
