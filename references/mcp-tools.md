# MCP Tools Reference

Use this reference when adding tools, debugging tool calls, or changing backend
capabilities.

## Server And Transport

`target/server.py` registers the FastMCP server. Agent-side calls go through
`agent/mcp_manager.py` and `TargetSubAgent.call_tool()`.

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
- Use desktop window handles first for Qt/UIA windows when PID connection is
  unreliable.

macOS backend:

- Uses Accessibility data.
- Supports component discovery/click plumbing through `dump_tree`,
  `find_control`, `click`, and `click_target`.
- `click_at` is dry-run by default. Set `EDR_WD_ALLOW_REAL_CLICKS=1` on the
  target only when real pointer actions are intended.

## Return Parsing

FastMCP tool responses may be wrapped in JSON-RPC content envelopes. Reuse
existing parser/unwrapping logic from `TargetSubAgent` or `test_case` clients
instead of writing ad hoc parsing in new tests.
