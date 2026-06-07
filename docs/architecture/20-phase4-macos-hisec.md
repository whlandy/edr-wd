# Phase 4: macos_hisec LaunchAgent GUI Session Validation

## 1. Goal

Validate Mac native HiSecEndpoint.app automation capability.

Target chain:

```
Win/Mac agent
  → mac-dev
    → macOS MCP server
      → macos_accessibility backend
        → /Applications/HiSecEndpoint.app
        → HiSecEndpointAgent
        → EDRClient
```

Phase 4 handles ONLY Mac native applications.
Phase 4 does NOT handle Parallels Windows VM internal windows.

---

## 2. Current State

```
macos_generic: PASSED
macos_hisec: NOT VALIDATED
Mac MCP server: can run
macos_accessibility: can load
activate_edr: returns structured JSON (no timeout)
```

Unresolved:
```
1. Mac MCP server GUI session stability
2. LaunchAgent as formal startup path
3. Mac native HiSec main window detection
4. Mac native EDRClient window detection
5. macos_hisec profile registration and acceptance
```

---

## 3. Core Principles

### 3.1 macos_hisec controls ONLY Mac native HiSec

Target objects:

```
/Applications/HiSecEndpoint.app
HiSecEndpointAgent
EDRClient
```

NOT:
```
Parallels Windows VM
Windows App proxy windows
HisecEndpointAgent.exe
EDRClient.exe
```

### 3.2 Mac server must run in GUI session

BANNED as formal acceptance basis:

```
ssh mac-dev 'nohup python3 server.py --http ...'
ssh mac-dev 'python3 server.py &'
```

These are for temporary diagnosis only.

Formal startup must be:

```
LaunchAgent
launchctl bootstrap gui/<uid>
launchctl kickstart gui/<uid>/...
```

---

## 4. macOS Lifecycle Design

### 4.1 Goal

`agent/lifecycle/macos.py` must handle:

```
1. Deploy target/
2. Install LaunchAgent plist
3. Start Mac MCP server
4. Confirm server in GUI session
5. Confirm 8765 open
6. Confirm backend=macos_accessibility
```

### 4.2 LaunchAgent Configuration

LaunchAgent should set:

```
Label = com.edr-wd.target
ProgramArguments = <PYTHON> server.py --http --host 0.0.0.0 --port 8765
WorkingDirectory = <MAC_TARGET_ROOT>
RunAtLoad = true
KeepAlive = false or controlled
StandardOutPath = <MAC_TARGET_ROOT>/logs/server.log
StandardErrorPath = <MAC_TARGET_ROOT>/logs/server.err.log
```

Environment variables:

```bash
EDR_WD_AUTOMATION_BACKEND=macos_accessibility
EDR_WD_MCP_HOST=0.0.0.0
EDR_WD_MCP_PORT=8765
EDR_WD_TARGET_ROOT=<MAC_TARGET_ROOT>
```

Do NOT print real `<MAC_TARGET_ROOT>` in logs or output.

### 4.3 Startup Commands

Correct startup path:

```
launchctl bootstrap gui/<uid> ~/Library/LaunchAgents/com.edr-wd.target.plist
launchctl kickstart -k gui/<uid>/com.edr-wd.target
```

Stop:

```
launchctl bootout gui/<uid> ~/Library/LaunchAgents/com.edr-wd.target.plist
```

Do NOT use:
- `system` domain
- root LaunchDaemon
- SSH/nohup

---

## 5. GUI Session Verification

After server startup, must verify:

```
1. 8765 open
2. status.backend = macos_accessibility
3. backend_loaded = true
4. interactive_session = true (or equivalent field)
5. list_windows returns Finder and other Mac native windows
6. activate_app Finder succeeds
```

If server opens 8765 but cannot access GUI:

```json
{
  "ok": false,
  "stage": "server_not_in_gui_session",
  "backend": "macos_accessibility"
}
```

This does NOT count as passing.

---

## 6. macos_hisec Window Detection Rules

### 6.1 main window

Candidate processes:

```
HiSecEndpointAgent
```

Candidate titles:

```
华为智能终端安全系统
华为安全防护中心
BaselineUIMainWindow
```

Requirement: owner/process must be `HiSecEndpointAgent`. Do NOT match by title alone.

### 6.2 EDRClient window

Candidate processes:

```
EDRClient
```

Candidate titles:

```
华为HiSec Endpoint
HiSec Endpoint
```

Requirement: owner/process must be `EDRClient`.

### 6.3 Forbidden false matches

These windows must NOT count as macos_hisec:

```
Windows App | 设备
Windows App | <WINDOWS_VM_IP>
Parallels Desktop
Parallels VM proxy windows
```

---

## 7. activate_edr Semantics

`activate_edr` in macOS backend must return structured result, never timeout.

### 7.1 Success

```json
{
  "ok": true,
  "stage": "done",
  "backend": "macos_accessibility",
  "main": {
    "process_found": true,
    "window_found": true,
    "window_title": "华为智能终端安全系统",
    "detected_by": "system_events"
  },
  "client": {
    "process_found": true,
    "window_found": true,
    "window_title": "HiSec Endpoint",
    "detected_by": "system_events"
  }
}
```

### 7.2 Failure — main window not found

```json
{
  "ok": false,
  "stage": "main_window_not_found",
  "backend": "macos_accessibility",
  "main": {
    "process_found": true,
    "window_found": false,
    "cmd_ui_attempted": true
  },
  "client": {
    "process_found": false,
    "window_found": false
  }
}
```

### 7.3 Failure — client window not found

```json
{
  "ok": false,
  "stage": "client_window_not_found",
  "backend": "macos_accessibility",
  "main": {
    "process_found": true,
    "window_found": true
  },
  "client": {
    "process_found": true,
    "window_found": false,
    "click_attempted": true
  }
}
```

---

## 8. status Semantics

`status` must return Mac-native HiSec structured fields:

```json
{
  "ok": true,
  "platform": "darwin",
  "backend": "macos_accessibility",
  "backend_loaded": true,
  "windows": {
    "hisec_main": {
      "process_found": true,
      "window_found": false,
      "owner": "HiSecEndpointAgent",
      "pid": null,
      "titles": [],
      "detected_by": null,
      "error": null
    },
    "edr_client": {
      "process_found": true,
      "window_found": true,
      "owner": "EDRClient",
      "pid": 1234,
      "titles": ["HiSec Endpoint"],
      "detected_by": "system_events",
      "error": null
    }
  }
}
```

If `process_found=false` but `window_found=true`, there is a false match. Inspect `owner`, `pid`, `titles`, `detected_by`.

---

## 9. macos_hisec Profile Design

New profile: `macos_hisec`

Test items:

```
1. tools/list
2. status
3. list_windows
4. activate_app Finder
5. activate_edr
6. is_window_open process_name=HiSecEndpointAgent
7. is_window_open process_name=EDRClient
8. connect EDRClient
9. click_at dry-run
10. screenshot (optional/skip)
```

Pass criteria:

```
backend = macos_accessibility
server running in GUI session
HiSecEndpointAgent process_found = true
main.window_found = true
EDRClient process_found = true
client.window_found = true
activate_edr.ok = true
```

`screenshot` may SKIP due to Screen Recording permission missing.

---

## 10. Permissions

macOS target may require:

```
Accessibility
Screen Recording
Automation / System Events
Full Disk Access (optional)
```

Permissions apply to the user/process context running the MCP server.

If server is started from SSH/nohup, permissions may differ from GUI session.
Therefore LaunchAgent verification is mandatory.

---

## 11. Error Classification

Phase 4 must distinguish:

```
server_not_in_gui_session
accessibility_permission_missing
screen_recording_permission_missing
automation_permission_missing
hisec_app_not_found
hisec_agent_process_not_found
main_window_not_found
edr_client_process_not_found
client_window_not_found
window_detection_inconsistent
parallels_window_misdetected
```

Must NOT return: `timeout`, `unknown`, `NoneType object has no attribute ...`

---

## 12. Acceptance Flow

### Step 1: LaunchAgent Install

```
install_launch_agent
```

Check:

```
plist exists
label correct
WorkingDirectory correct
EDR_WD_AUTOMATION_BACKEND=macos_accessibility
```

### Step 2: LaunchAgent Start

```
launchctl bootstrap gui/<uid>
launchctl kickstart -k gui/<uid>/com.edr-wd.target
```

### Step 3: MCP Verification

```
8765 open
mcp_initialize("mac-dev")
status.backend = macos_accessibility
list_windows returns Finder
activate_app Finder ok
```

### Step 4: HiSec Verification

```
activate_edr(timeout=20)
status
list_windows
is_window_open HiSecEndpointAgent
is_window_open EDRClient
```

### Step 5: Profile Registration

Only register `macos_hisec` as passable profile when `activate_edr.ok=true`.

---

## 13. Banned Patterns

```
1. SSH/nohup as formal Mac GUI server startup
2. SSH session osascript result as formal acceptance
3. Windows App proxy window counted as Mac native HiSec
4. Parallels VM internal window counted as macos_hisec
5. process_found substituted for window_found
6. activate_edr timeout instead of structured return
7. Debug diagnose tool default-exposed
```

---

## 14. Deliverables

Code:
```
1. macOS lifecycle LaunchAgent stable startup
2. status GUI session detection
3. macos_hisec window detection
4. macos_hisec profile
```

Documentation:
```
docs/architecture/20-phase4-macos-hisec.md
```

Tests:
```
macos_generic regression
macos_hisec smoke
```

Acceptance record (all sanitized):
```
status JSON
activate_edr JSON
list_windows summary
profile test results
```

---

## 15. Final Acceptance Criteria

Phase 4 passes when ALL of:

```
1. Mac MCP server started by LaunchAgent/gui session
2. backend = macos_accessibility
3. list_windows returns Mac native windows
4. activate_app Finder succeeds
5. HiSecEndpointAgent process_found = true
6. main.window_found = true
7. EDRClient process_found = true
8. client.window_found = true
9. activate_edr.ok = true
10. macos_hisec test passes
```

Do NOT declare Phase 4 complete until all above are satisfied.
