---
sop_id: hisec.security-center-compliance
version: 1
title: HiSec Security Center Compliance Evidence
purpose: Open the left-side Security Center and collect compliance-page evidence.
profiles: [windows_hisec, macos_hisec]
platforms: [windows, macos]
primary_window_scope: hisec_agent
risk: read_only
---

# HiSec Security Center Compliance Evidence

## Pass Condition

The left-side `安全中心` control in `HisecEndpointAgent` is activated through a
tree-derived selector, and the refreshed component tree contains compliance
content rather than scan-page content.

## Preconditions

- `hisec.window-pair-visible` passes.
- Required tools: `connect`, `lock_window`, `dump_tree`, `click`,
  `verify_window_lock`, `screenshot`.
- Windows left-navigation activation must use a real component click, because
  UIA `toggle` can change tab state without switching the Qt content page.
- macOS real clicks require target permission/configuration; `dry_run` cannot
  satisfy this SOP.

## Platform Mapping

| Scope | Windows | macOS |
|---|---|---|
| `hisec_agent` | `HisecEndpointAgent.exe` | `HiSecEndpointAgent` |
| `edr_client` | `EDRClient.exe` | `EDRClient` |

All steps in this SOP use `hisec_agent`. Do not connect to `edr_client` merely
because both windows are visible.

## Fixed Procedure

### S01 - Connect To HiSec Entry Process

- Operation: `connect`
- Window scope: `hisec_agent`
- MCP tool: `connect`
- Arguments: platform `hisec_agent` process, `timeout=10`,
  `auto_activate=true` where supported.
- Required result: `ok=true` and a connected PID/process.
- On failure: abort.

### S02 - Lock And Verify Window Context

- Operation: `window_assert`
- Window scope: `hisec_agent`
- MCP tools: `lock_window`, then `verify_window_lock`.
- Required result: lock process equals `hisec_agent`; verify returns `ok=true`.
- On failure: abort.

### S03 - Capture Before Tree

- Operation: `tree_snapshot`
- Window scope: `hisec_agent`
- MCP tool: `dump_tree`
- Arguments: `{"max_depth": 20}`.
- Required result: non-empty controls and process evidence for `hisec_agent`.
- Evidence: retain a reference to the before tree.
- On failure: abort.

### S04 - Resolve Left-Side Security Center

- Operation: `content_assert`
- Window scope: `hisec_agent`
- Selector ID: `SEL-SECURITY-CENTER`.
- Windows UIA mapping:
  - `text` exactly `安全中心`;
  - `control_type=CheckBox` when exposed;
  - exact `automation_id` from the current Windows tree when available.
- macOS AX mapping:
  - `title`, `description`, or `value` exactly `安全中心`;
  - exact `role` from the current macOS tree;
  - exact `identifier` (`AXIdentifier`) when available.
- Resolve the mapping for the active platform to exactly one candidate. Do not
  reuse a Windows `automation_id` as a macOS identifier or persist a tree
  `control_id`.
- On zero matches: `selector_not_found` and abort.
- On multiple matches: `selector_ambiguous` and abort.

### S05 - Activate Security Center

- Operation: `semantic_click`
- Window scope: `hisec_agent`
- MCP tool: `click`.
- Required arguments:
  - tree-derived selector from S04;
  - `parent_fallback=false`;
  - `expected_process_name=<platform hisec_agent process>`.
- Required result:
  - `ok=true`;
  - returned process equals `hisec_agent`;
  - Windows method is `click_input` for HiSec left-navigation CheckBox tabs;
  - macOS method is not `dry_run`.
- On failure: abort; do not silently click coordinates.

### S06 - Capture After Tree

- Operation: `tree_snapshot`
- Window scope: `hisec_agent`
- MCP tool: `dump_tree`.
- Arguments: `{"max_depth": 20}` after a bounded refresh wait of at most 10
  seconds.
- Required result: non-empty refreshed controls.
- Retry: tree refresh only, interval 0.5 seconds.
- On failure: abort and retain the before tree plus click result.

### S07 - Verify Compliance Page

- Operation: `content_assert`.
- Window scope: `hisec_agent`.
- Required evidence: at least one recognized compliance state plus inspection
  controls. Known evidence includes:
  - `管理员未配置有效策略`;
  - `重新检查`;
  - `账号安全检查`;
  - `主机安全检查`;
  - `设备安全检查`.
- Forbidden dominant old-page evidence:
  - `快速扫描`;
  - `快速查杀`;
  - `全盘扫描`;
  - `自定义扫描`.
- Pass rule: compliance evidence is present and scan actions are not the main
  page content.
- On failure: `outcome_not_observed`; abort after one tree refresh.

### S08 - Extract Compliance Evidence

- Operation: `extract`.
- Window scope: `hisec_agent`.
- Output:
  - unique visible page texts;
  - matched compliance sections/statuses;
  - clicked selector evidence;
  - before/after tree references;
  - click method and process;
  - optional screenshot reference.
- Do not infer compliance status from missing text.

## Report Extension

```json
{
  "extracted": {
    "page": "安全中心",
    "window_scope": "hisec_agent",
    "visible_texts": [],
    "compliance_sections": [],
    "compliance_statuses": []
  },
  "required_evidence_found": [],
  "forbidden_evidence_found": [],
  "clicked_selector": {},
  "click_method": null,
  "click_process": null
}
```

## Safety

This SOP is read-only with respect to EDR policy/configuration. It may change
the visible page. It must not start a scan, remediation, policy change, or
device-control action.
