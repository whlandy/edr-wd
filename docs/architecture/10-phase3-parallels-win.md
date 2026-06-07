# Phase 3: parallels-win Windows VM Target Deployment

## 1. Goal

Deploy Parallels Windows VM as an independent Windows target for real GUI automation of HiSec/EDR windows inside the VM.

Target chain:

```
Win/Mac agent
  → parallels-win
    → Windows VM MCP server
      → windows_pywinauto backend
        → HisecEndpointAgent.exe
        → EDRClient.exe
        → EDR window internal controls
```

Phase 3 does NOT handle Mac native HiSecEndpoint.app.
Phase 3 does NOT use macOS Accessibility to operate Parallels VM internal windows.

---

## 2. Current State

```
macos_generic: PASSED
macos_hisec: NOT VALIDATED
parallels-win: NOT DEPLOYED
Windows VM MCP server: NOT STARTED
Windows VM 8765: NOT LISTENING
```

Existing boundaries:

```
mac-dev       = macOS target, platform=macos, backend=macos_accessibility
parallels-win = Windows target, platform=windows, backend=windows_pywinauto
```

---

## 3. Design Principles

### 3.1 target OS determines backend

parallels-win is a Windows VM, so it must use:

```
platform = windows
lifecycle = lifecycle/windows.py
backend = windows_pywinauto
app_profile = windows_hisec
```

Must NOT use `macos_accessibility`.

### 3.2 Mac can only act as host or bootstrap helper

For a Parallels Windows VM, Mac only handles:

```
1. VM lifecycle management
2. Network connectivity
3. Optional bootstrap
```

Mac must NOT:
- Enumerate VM internal windows
- Click VM internal controls
- Dump VM internal control tree
- Use macOS Accessibility to control VM EDR

### 3.3 prlctl is NOT a GUI backend

prlctl may be used for:
- Check if VM is running
- Get VM IP
- Execute remote commands
- Assist bootstrapping Windows MCP server

It must NOT be used as a formal GUI automation backend. Formal GUI automation must happen inside the Windows VM:

```
Windows VM MCP server → windows_pywinauto
```

---

## 4. Target Configuration

New target: `parallels-win`

```json
{
  "targets": {
    "parallels-win": {
      "description": "Parallels Windows VM target for HiSec/EDR GUI automation",
      "platform": "windows",
      "app_profile": "windows_hisec",
      "ssh": {
        "host": "<WINDOWS_VM_IP>",
        "port": 22,
        "user": "<WINDOWS_USER>",
        "auth": {
          "type": "password",
          "password_env": "EDR_WD_TARGET_PASSWORD"
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
}
```

### 4.1 No plaintext password

FORBIDDEN: `"password": "<REAL_PASSWORD>"`
REQUIRED: `"password_env": "EDR_WD_TARGET_PASSWORD"`

Agent side only checks: `EDR_WD_TARGET_PASSWORD = SET / NOT SET`
Must NOT print password value, length, or hash.

---

## 5. Config Completeness Check

Before any SSH operation, verify:

```
ssh.host is not a placeholder
ssh.user is not a placeholder
windows.target_root is not a placeholder
auth.password_env exists
EDR_WD_TARGET_PASSWORD is set
```

If incomplete, return:

```json
{
  "ok": false,
  "stage": "config_incomplete",
  "target": "parallels-win",
  "missing": ["ssh.host", "ssh.user", "windows.target_root", "EDR_WD_TARGET_PASSWORD"]
}
```

Do NOT attempt to connect to placeholders.

---

## 6. Deployment Flow

### Step 1: Network Check

Check ports:

```
<TARGET_IP>:22    SSH
<TARGET_IP>:8765 MCP
<TARGET_IP>:3389 RDP (optional)
```

Expected: `22 open`, `8765 may be closed initially`

If 22 is not reachable, stop Phase 3.

### Step 2: SSH Probe

Via `agent/ssh_runner.py`:

```
hostname
whoami
where python
py -0p
powershell -NoProfile -Command "$PSVersionTable.PSVersion"
```

Requirements:
```
1. SSH login succeeds
2. Python is available
3. PowerShell is available
4. Output sanitized
```

On failure:

```json
{
  "ok": false,
  "stage": "ssh_probe_failed",
  "target": "parallels-win",
  "error": "<REDACTED>"
}
```

### Step 3: target Directory Check

```
Test-Path <WINDOWS_TARGET_ROOT>
Test-Path <WINDOWS_TARGET_ROOT>\server.py
Test-Path <WINDOWS_TARGET_ROOT>\automation
Test-Path <WINDOWS_TARGET_ROOT>\scripts
```

If missing, deploy:

```
agent target/ → Windows VM <WINDOWS_TARGET_ROOT>
```

Must NOT upload:
```
config/targets.local.json
.env
*.log
*.local.json
local diagnostic scripts
```

### Step 4: Start Windows MCP Server

Use Windows lifecycle:

```
agent/lifecycle/windows.py
target/scripts/start_server.ps1
```

Server must set:

```powershell
$env:EDR_WD_AUTOMATION_BACKEND = "windows_pywinauto"
$env:EDR_WD_ENABLE_PYWINAUTO = "1"
$env:EDR_WD_ENABLE_POWERSHELL = "1"
```

After startup, verify:

```
<TARGET_IP>:8765 open
tools/list works
status.backend = windows_pywinauto
```

---

## 7. Windows GUI Session Requirement

GUI automation requires server running in an **interactive desktop session**, not just process existence.

**Invalid scenarios:**
```
1. Session 0
2. SSH non-interactive session
3. Background Start-Process where parent exits
4. Task Scheduler without interactive desktop
5. 8765 open but pywinauto cannot see windows
```

**Pass criteria:**
```
1. 8765 open
2. backend = windows_pywinauto
3. list_windows returns real Windows windows
4. dump_tree returns control tree
5. activate_edr activates HisecEndpointAgent.exe / EDRClient.exe
```

---

## 8. MCP Acceptance Flow

### 8.1 Initialize

```
mcp_initialize("parallels-win")
```

Expected:

```json
{ "ok": true, "target": "parallels-win", "session_id": "<SESSION_ID>" }
```

### 8.2 status

Expected:

```json
{
  "ok": true,
  "platform": "win32",
  "backend": "windows_pywinauto",
  "backend_loaded": true
}
```

### 8.3 list_windows

Expected: returns real Windows windows inside the VM, NOT Mac Windows App proxy windows.

### 8.4 activate_edr

Expected:

```json
{
  "ok": true,
  "main": { "process_found": true, "window_found": true },
  "client": { "process_found": true, "window_found": true }
}
```

### 8.5 dump_tree

```
connect
dump_tree
```

Expected: returns EDR window internal control tree.

---

## 9. windows_hisec Smoke Test

Test items:

```
1. tools/list
2. status
3. list_windows
4. is_window_open process_name=HisecEndpointAgent.exe
5. activate_edr
6. is_window_open process_name=EDRClient.exe
7. connect main window
8. dump_tree
9. click dry-run
10. screenshot (optional)
```

Pass criteria:

```
backend = windows_pywinauto
HisecEndpointAgent.exe process_found = true
main.window_found = true
EDRClient.exe process_found = true
client.window_found = true
dump_tree returns controls
click returns structured result
```

---

## 10. Error Classification

Phase 3 must distinguish:

```
config_incomplete
auth_missing
ssh_failed
python_missing
target_not_deployed
deploy_failed
server_start_failed
server_not_running
mcp_initialize_failed
backend_not_loaded
session0_or_desktop_unavailable
hisec_agent_not_found
hisec_main_window_not_found
edr_client_not_found
edr_client_window_not_found
dump_tree_failed
```

Must NOT return: `timeout`, `unknown`, `NoneType object has no attribute ...`

---

## 11. Privacy Requirements

FORBIDDEN:
```
Real IP, user, password, target_root in output
cat targets.local.json
git diff targets.local.json
Commit targets.local.json, .env, runtime logs, diagnostic scripts
```

ALLOWED:
```
target=parallels-win
platform=windows
backend=windows_pywinauto
host=<REDACTED>
EDR_WD_TARGET_PASSWORD=SET/NOT SET
```

---

## 12. Banned Patterns

```
1. Use mac-dev to operate Windows VM EDR
2. Use macOS Accessibility to penetrate Parallels VM
3. Use prlctl exec as GUI backend
4. Use RDP as MCP automation acceptance substitute
5. Treat 8765 open as GUI automation success
6. Treat process_found as window_found
7. Plaintext password in config
```

---

## 13. Phase 3 Deliverables

Code/config:
```
1. parallels-win target configuration skeleton
2. placeholder validation
3. Windows lifecycle startup verification
4. windows_hisec smoke test
```

Documentation:
```
docs/architecture/10-phase3-parallels-win.md
```

Acceptance record (all sanitized):
```
SSH probe result
MCP initialize result
status result
list_windows result
activate_edr result
dump_tree result
```

---

## 14. Final Acceptance Criteria

Phase 3 passes when ALL of:

```
1. parallels-win config complete with password_env
2. SSH login succeeds
3. target/ deployed successfully
4. Windows MCP server started
5. <TARGET_IP>:8765 open
6. backend = windows_pywinauto
7. list_windows能看到 VM 内 Windows 窗口
8. activate_edr.ok = true
9. main.window_found = true
10. client.window_found = true
11. dump_tree available
12. click available or dry-run available
```

Do NOT declare Phase 3 complete until all above are satisfied.
