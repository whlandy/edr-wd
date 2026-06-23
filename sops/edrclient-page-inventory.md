---
sop_id: edrclient.page-inventory
version: 1
title: EDRClient Page Component Inventory
purpose: Verify EDRClient is addressable and collect its current visible component inventory.
profiles: [windows_hisec, macos_hisec]
platforms: [windows, macos]
primary_window_scope: edr_client
risk: read_only
---

# EDRClient Page Component Inventory

## Pass Condition

The SOP connects specifically to `EDRClient`, verifies its window context, and
returns a non-empty component inventory owned by that process. Controls from
`HisecEndpointAgent` must not appear as the selected window context.

## Preconditions

- `hisec.window-pair-visible` passes.
- Required tools: `connect`, `lock_window`, `verify_window_lock`, `dump_tree`,
  `screenshot`.
- Accessibility/UIA permission is available for the target GUI session.

## Platform Mapping

| Scope | Windows | macOS |
|---|---|---|
| `edr_client` | `EDRClient.exe` | `EDRClient` |
| forbidden context | `HisecEndpointAgent.exe` | `HiSecEndpointAgent` |

## Fixed Procedure

### S01 - Connect To EDRClient

- Operation: `connect`.
- Window scope: `edr_client`.
- MCP tool: `connect`.
- Arguments: platform `edr_client` process, `timeout=10`,
  `auto_activate=true` where supported.
- Required result: `ok=true` and connected PID/process belongs to EDRClient.
- On failure: abort; do not fall back to HiSecEndpointAgent.

### S02 - Lock EDRClient Context

- Operation: `window_assert`.
- Window scope: `edr_client`.
- MCP tool: `lock_window` followed by `verify_window_lock`.
- Required result: lock/active process is EDRClient and `ok=true`.
- On failure: abort.

### S03 - Capture EDRClient Tree

- Operation: `tree_snapshot`.
- Window scope: `edr_client`.
- MCP tool: `dump_tree`.
- Arguments: `{"max_depth": 20}`.
- Required result: `ok=true`, non-empty controls, process evidence is EDRClient.
- On failure: abort.

### S04 - Extract Current Page Inventory

- Operation: `extract`.
- Window scope: `edr_client`.
- Extract unique visible text plus, when available, control type, class/role,
  automation/AX identifier, enabled state, and rectangle.
- Normalize each control with `logical_kind`, `visible_text`, and `enabled`, but
  retain a platform-specific `native_selector`: Windows uses
  `automation_id/control_type/class_name/text`; macOS uses
  `identifier/role/subrole/title/description/value`.
- Treat `control_id` as snapshot metadata only. Do not publish it as a reusable
  selector or compare it across runs.
- Preserve raw tree by reference rather than duplicating it in every field.
- Do not infer feature status from controls that are absent.

### S05 - Capture Supplemental Screenshot

- Operation: `screenshot`.
- Window scope: `edr_client`.
- MCP tool: `screenshot`.
- Screenshot failure caused only by known display permission may be recorded as
  supplemental evidence unavailable; it does not replace S03.

## Report Extension

```json
{
  "extracted": {
    "window_scope": "edr_client",
    "process_name": "EDRClient(.exe)",
    "window_title": null,
    "visible_texts": [],
    "controls": []
  },
  "tree_ref": null,
  "screenshot_ref": null
}
```

## Safety

This SOP is read-only. It does not click, type, scan, remediate, or change EDR
configuration. Use it as the precondition/base step for a feature-specific
EDRClient SOP.
