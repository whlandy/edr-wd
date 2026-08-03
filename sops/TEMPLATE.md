---
sop_id: hisec.<short-capability-name>
version: 1
title: <Human-readable verification name>
purpose: <One observable product capability verified by this SOP>
profiles: [windows_hisec, macos_hisec]
platforms: [windows, macos]
primary_window_scope: hisec_agent
risk: read_only
transition_kind: page_navigation
application_state_hint: <sop-specific-end-state>
---

# <SOP Title>

## Pass Condition

State the user-visible outcome that proves the feature works. Do not use
`tool returned ok` as the pass condition.

## Preconditions

- Target profile is one of the declared profiles.
- MCP initialize and `tools/list` succeed.
- Backend matches the target platform.
- Required tools: `<tool names>`.
- Required application/window state: `<state>`.
- Mutation or permission requirements: `<none or explicit requirement>`.

## Platform Mapping

| Scope | Windows | macOS |
|---|---|---|
| `hisec_agent` | `HisecEndpointAgent.exe` | `HiSecEndpointAgent` |
| `edr_client` | `EDRClient.exe` | `EDRClient` |

## Inputs

| Input | Required | Default | Constraint |
|---|---:|---|---|
| `<name>` | yes/no | `<value>` | `<validation>` |

## Fixed Procedure

### S01 - <Operation name>

- Operation: `activate|window_assert|connect|tree_snapshot|semantic_click|content_assert|extract|screenshot`
- Window scope: `none|hisec_agent|edr_client`
- MCP tool: `<tool>`
- Arguments:

  ```json
  {}
  ```

- Required result: `<tool-level contract>`
- Outcome evidence: `<observable postcondition>`
- Retry: `none` or `<poll interval/timeout>`
- On failure: `abort|record-and-continue`

### S02 - <Next operation>

Repeat the same fields. Preserve step IDs after publication; append new IDs
instead of renumbering historical steps.

## Selectors

| Selector ID | Window scope | Windows UIA mapping | macOS AX mapping | Uniqueness rule | Allowed fallback |
|---|---|---|---|---|---|
| `SEL-01` | `<scope>` | `automation_id`, `control_type`, `class_name`, `text` | `identifier`, `role`, `subrole`, `title/description/value` | exactly one | none |

Define mappings independently. Do not reuse native identifiers across
platforms. Omit a field only when the application does not expose it:

```yaml
SEL-01:
  windows:
    automation_id: <exact UIA AutomationId>
    control_type: <UIA control type>
    text: <exact visible text>
  macos:
    identifier: <exact AXIdentifier>
    role: <AXRole>
    title: <exact visible title>
```

`control_id` is snapshot-local for macOS and may be unstable on Windows. It
must not be stored as a persistent SOP selector. A macOS `automation_id` field
is only a compatibility alias for `identifier`, not a cross-platform ID.

For HiSec clicks, include the platform process from the selected window scope as
`expected_process_name`.

## Verification

- Required texts/controls: `<list>`
- Forbidden old-page texts/controls: `<list>`
- Window assertions: `<list>`
- Extracted fields: `<list>`

An empty required list must be justified. Screenshots are supplemental evidence.

## Failure Classification

| Code | Meaning | Retryable | Required action |
|---|---|---:|---|
| `precondition_failed` | Required environment/tool/window unavailable | no | abort |
| `selector_not_found` | No component matched | no | save tree and abort |
| `selector_ambiguous` | More than one component matched | no | narrow selector |
| `click_context_required` | HiSec click omitted process context | no | fix SOP |
| `click_context_mismatch` | Connected process differs from step scope | no | reconnect correct process |
| `outcome_not_observed` | Action ran but business state did not change | one tree refresh | abort after refresh |

## Report Fields

```json
{
  "extracted": {},
  "required_evidence_found": [],
  "forbidden_evidence_found": [],
  "before_tree_ref": null,
  "after_tree_ref": null,
  "screenshot_ref": null
}
```

## Safety

- State whether this SOP is read-only or mutating.
- List every mutating step explicitly.
- Do not include credentials, real IPs, usernames, or machine-specific paths.
- Do not add arbitrary command execution as a step.
