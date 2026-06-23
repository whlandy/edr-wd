---
sop_id: hisec.window-pair-visible
version: 1
title: HiSec Window Pair Visibility
purpose: Verify the HiSec entry window and EDRClient window are both visible.
profiles: [windows_hisec, macos_hisec]
platforms: [windows, macos]
primary_window_scope: none
risk: read_only
---

# HiSec Window Pair Visibility

## Pass Condition

`activate_edr` succeeds and both platform-specific windows are independently
confirmed visible by process-aware window detection.

## Preconditions

- MCP initialize succeeds.
- Backend is `windows_pywinauto` or `macos_accessibility`.
- Required tools: `status`, `activate_edr`, `wait_window`, `is_window_open`.
- HiSec is installed in its standard target location.

## Platform Mapping

| Scope | Windows | macOS | Optional title evidence |
|---|---|---|---|
| `hisec_agent` | `HisecEndpointAgent.exe` | `HiSecEndpointAgent` | `华为智能终端安全系统` |
| `edr_client` | `EDRClient.exe` | `EDRClient` | `华为HiSec Endpoint` |

## Fixed Procedure

### S01 - Check Backend

- Operation: `content_assert`
- Window scope: `none`
- MCP tool: `status`
- Required result: `ok=true` and backend matches target platform.
- Retry: none.
- On failure: abort.

### S02 - Activate Window Pair

- Operation: `activate`
- Window scope: `none`
- MCP tool: `activate_edr`
- Arguments: `{"wait": true, "timeout": 20}`
- Required result: `ok=true`, `main.window_found=true`,
  `client.window_found=true`.
- Retry: none; `activate_edr` owns its internal fallback sequence.
- On failure: abort.

### S03 - Wait For HiSec Entry Window

- Operation: `window_assert`
- Window scope: `hisec_agent`
- MCP tool: `wait_window`
- Arguments: platform process, `timeout=15`, `interval=0.5`.
- Required result: `ok=true`, `found=true`.
- Retry: polling is performed by `wait_window`.
- On failure: abort.

### S04 - Wait For EDRClient Window

- Operation: `window_assert`
- Window scope: `edr_client`
- MCP tool: `wait_window`
- Arguments: platform process, `timeout=15`, `interval=0.5`.
- Required result: `ok=true`, `found=true`.
- Retry: polling is performed by `wait_window`.
- On failure: abort.

### S05 - Independently Verify Both Windows

- Operation: `window_assert` twice.
- Window scopes: `hisec_agent`, then `edr_client`.
- MCP tool: `is_window_open`.
- Required result for each: `ok=true`, `found=true`.
- Outcome evidence: two distinct process/window records.
- Retry: none.
- On failure: abort.

## Verification

- Do not infer one window from the other.
- Do not pass when only a PID exists; the desktop window must be visible.
- Record process, PID, title, rectangle, and visibility when returned.
- This SOP is read-only except for making existing windows visible.

## Existing Automation Mapping

- `test_case/test_integration/test_edr_window_pair_e2e.py`
- `test_case/test_e2e/test_windows_hisec_e2e.py`
- `test_case/test_e2e/test_macos_hisec_e2e.py`

## Safety

This SOP does not change EDR policy or configuration. It may launch, restore,
or foreground the two application windows. It must not click page controls.
