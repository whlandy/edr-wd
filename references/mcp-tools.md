# MCP Tools Reference

Use this reference when adding tools, debugging tool calls, or changing backend
capabilities.

## Server And Transport

`target/server.py` registers the FastMCP server. Agent-side MCP calls go
through `agent/mcp_manager.py` and `TargetSubAgent.call_tool()`. Agent-target
SSH command execution and file transfer go through Paramiko in
`agent/ssh_runner.py`.

Before deploying or restarting MCP, preflight the full Python runtime:

```bash
python -c 'import fastmcp, paramiko, psutil, PIL, pyautogui; print("agent deps ok")'
<REMOTE_PYTHON> -c 'import fastmcp, psutil, PIL; print("target core deps ok")'
```

Windows targets also need `pywinauto` and `pyautogui`; macOS targets need
`pyautogui` plus Accessibility/GUI permissions. Runtime dependencies come from
`pyproject.toml`. Test-only dependencies come from
`test_case/requirements_test.txt` and are not required on the target MCP runtime
unless tests are being run there directly.

If a FastMCP server is already listening on the configured MCP URL, call
`mcp_manager.initialize(target)` and `status` before deciding to restart it.
Do not treat "port open" as sufficient proof of GUI readiness; check MCP
initialize and backend status.

Do not call the protocol method `tools/list` through `call_tool()`. Use the
client's tools-list path (`tools_list()` or `_do_req("tools/list", {})`) because
`tools/list` is not an MCP tool.

## Tool Groups

Session/window:

- `connect`
- `list_windows`
- `is_window_open`
- `wait_window`
- `status`

HiSec:

- `activate_edr`
- `restore_edr`

GUI actions:

- `dump_tree`
- `find_control`
- `click`
- `click_target`
- `click_at`
- `click_window_at`
- `double_click_at`
- `right_click_at`
- `middle_click_at`
- `hover_at`
- `drag`
- `scroll`
- `type_text`
- `select`
- `get_text`
- `screenshot`

Window safety:

- `lock_window`
- `unlock_window`
- `get_window_lock`
- `verify_window_lock`

Windows PowerShell:

- `run_powershell`
- `start_powershell`
- `get_job`
- `cancel_job`

PowerShell tools require `EDR_WD_ENABLE_POWERSHELL=1` on the target server.
`activate_edr` must not depend on PowerShell availability.

## Backend Capability Rules

Windows backend:

- Uses pywinauto UIA/Win32 paths.
- Component clicks should prefer UIA semantic actions (`uia_invoke` or
  `uia_toggle`) before mouse input.
- HiSec `click`/`click_target` calls require `expected_process_name` so
  `HisecEndpointAgent.exe` and `EDRClient.exe` cannot be confused.
- Use desktop window handles first for Qt/UIA windows when PID connection is
  unreliable.

macOS backend:

- Uses Accessibility data.
- Supports component discovery/click plumbing through `dump_tree`,
  `find_control`, `click`, and `click_target`.
- HiSec `click`/`click_target` calls require `expected_process_name` so
  `HiSecEndpointAgent` and `EDRClient` cannot be confused.
- `click_at` is dry-run by default. Set `EDR_WD_ALLOW_REAL_CLICKS=1` on the
  target only when real pointer actions are intended.

## Return Parsing

FastMCP tool responses may be wrapped in JSON-RPC content envelopes. Reuse
existing parser/unwrapping logic from `TargetSubAgent` or `test_case` clients
instead of writing ad hoc parsing in new tests.
