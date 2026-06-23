# EDR-WD SOP Catalog And Design

## Purpose

An EDR-WD SOP is a fixed, reviewable sequence of MCP operations used to verify
one product capability in `HisecEndpointAgent` or `EDRClient`.

SOPs are evidence-driven. A tool returning `ok: true` proves only that the tool
ran. The SOP passes only when its declared postconditions are observed in the
correct application window.

## Directory Contract

```text
sops/
├── INDEX.md
├── TEMPLATE.md
├── edrclient-page-inventory.md
├── hisec-window-pair-visible.md
└── hisec-security-center-compliance.md
```

Keep SOP files flat under `sops/`. Use lowercase hyphenated filenames and a
stable `sop_id`. Do not create per-session copies, logs, screenshots, or reports
inside this directory.

## SOP Model

Each SOP contains six contracts:

1. **Identity**: stable ID, version, purpose, supported profiles and platforms.
2. **Preconditions**: target/backend/tools/windows that must be available.
3. **Window ownership**: every GUI step names `HisecEndpointAgent` or
   `EDRClient`; they are never interchangeable.
4. **Fixed steps**: ordered MCP calls with explicit inputs and retry policy.
5. **Postconditions**: observable business evidence, not only tool success.
6. **Report contract**: normalized evidence and failure information.

## Window Ownership

| Scope | Windows | macOS | Typical operations |
|---|---|---|---|
| `hisec_agent` | `HisecEndpointAgent.exe` | `HiSecEndpointAgent` | Entry window, navigation, left-side `安全中心`, `前往安全防护中心` |
| `edr_client` | `EDRClient.exe` | `EDRClient` | Security-center content, scan/configuration/compliance details |

Before `dump_tree`, `find_control`, `click`, `click_target`, or text extraction:

1. Connect to the declared process.
2. Lock or verify that process/window where supported.
3. Pass the same process as `expected_process_name` to every click operation.
4. Reject `click_context_required` and `click_context_mismatch`; never retry in
   the other process.

## Step Semantics

| Operation | Purpose | Required evidence |
|---|---|---|
| `activate` | Make the two HiSec windows visible | `main.window_found`, `client.window_found` |
| `window_assert` | Verify a specific desktop window | `found=true`, process/title evidence |
| `connect` | Select the exact process for following operations | connected PID/process |
| `tree_snapshot` | Capture current controls/page text | process, controls, count |
| `semantic_click` | Invoke one tree-derived component | selector, method, process, click result |
| `content_assert` | Verify business state after an action | required/forbidden texts or controls |
| `extract` | Collect page information | normalized fields plus source controls |
| `screenshot` | Optional visual evidence | path/hash; never the only pass condition |

## Cross-Platform Selector Model

SOPs use a stable logical selector ID, such as `SEL-SECURITY-CENTER`, but map
that ID to separate native selectors for Windows and macOS. Native component
identifiers are not portable between platforms.

| Concept | Windows UIA | macOS Accessibility |
|---|---|---|
| Stable native identifier | `automation_id` when the application exposes one | `identifier` (`AXIdentifier`) when the application exposes one |
| Component kind | `control_type`, `class_name` | `role`, `subrole` |
| Visible label | `text` | `title`, `description`, or `value` |
| Snapshot-local ID | `control_id` may still change across runs or versions | `control_id` is generated during tree traversal and is valid only for that snapshot |

The macOS backend may expose `identifier` again as `automation_id` for API
compatibility. That alias does not make it a Windows UIA AutomationId. Keep the
native field in SOP definitions and let the execution adapter translate it.

Selector rules:

1. Give the business component one cross-platform logical selector ID.
2. Define independent `windows` and `macos` native selector mappings.
3. Prefer a stable native identifier plus component kind and visible label.
4. Never copy a Windows identifier into the macOS mapping or the reverse.
5. Never persist `control_id` as an SOP selector. It may only identify a node
   between operations that explicitly use the same captured tree snapshot.
6. Require exactly one match after applying the platform mapping.

## Verification Rules

- Separate `action_ok` from `outcome_ok` in reports.
- Re-dump the component tree after navigation or state-changing actions.
- Prefer selectors in this order: native stable identifier plus component kind,
  visible label plus class/role, then a uniquely constrained tree path.
- A selector returning multiple controls is a failure until narrowed.
- `click_input`, center click, and coordinates are fallbacks, not semantic proof.
- A coordinate fallback requires an explicit SOP step and reason.
- For a new window, verify with `wait_window` and `is_window_open`.
- For an in-window page change, verify required and forbidden component text.

## Retry And Failure Policy

- Retry polling operations only (`wait_window`, post-click tree refresh).
- Repeat a semantic click once only when its selector is still unique and the
  first call reports a transient execution error.
- Do not retry context mismatch, ambiguous selectors, wrong-page evidence, or
  permission failures.
- Abort dependent steps after a hard failure; still emit the partial report.
- Never hide a failed required step by counting it as skipped.

## Evidence And Report Contract

```json
{
  "sop_id": "hisec.example",
  "sop_version": 1,
  "target": "<redacted-or-canonical-target-name>",
  "platform": "windows|macos",
  "profile": "windows_hisec|macos_hisec",
  "started_at": "ISO-8601",
  "finished_at": "ISO-8601",
  "ok": true,
  "steps": [
    {
      "id": "S01",
      "operation": "connect",
      "window_scope": "hisec_agent",
      "action_ok": true,
      "outcome_ok": true,
      "evidence": {}
    }
  ],
  "extracted": {},
  "error": null
}
```

Do not include passwords, usernames, full target IPs, unrestricted filesystem
paths, or raw environment dumps. Component trees may contain user/device data;
store them only in a caller-selected run artifact location.

## Catalog

| SOP | Purpose | Primary scope |
|-----|---------|---------------|
| [`hisec-window-pair-visible.md`](hisec-window-pair-visible.md) | Verify both HiSec windows can be activated and remain visible | Both windows |
| [`hisec-security-center-compliance.md`](hisec-security-center-compliance.md) | Enter left-side `安全中心` and collect compliance-page evidence | `hisec_agent` |
| [`edrclient-page-inventory.md`](edrclient-page-inventory.md) | Verify EDRClient context and collect its current component inventory | `edr_client` |

Use [`TEMPLATE.md`](TEMPLATE.md) to add another functional verification SOP.

## Future Executor Boundary

Markdown is the review source of truth. If deterministic execution is added,
implement a Python executor under `agent/sop/` that supports only the operation
classes above. Do not execute arbitrary shell, PowerShell, Python, or free-form
MCP tool names from SOP documents.
