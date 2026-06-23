# Component-Tree Click SOP

Use this SOP whenever an EDR-WD task says "click a button/control/tab" or asks
for page data after a click. The goal is to act on the component tree, not on
guessed screen coordinates.

## Core Rule

Do not start with `click_at`, `click_window_at`, or a bare
`click(text="...")`.

Start with window verification, connect to the exact process/window, dump the
tree, select one unique node, then trigger the node through its semantic action.
Only use coordinates as an explicitly documented fallback after semantic actions
fail.

## HiSec Click Contexts

Treat the two HiSec processes as separate click scopes:

| Requested UI | Windows process | macOS process |
|---|---|---|
| Entry window, navigation, left-side `安全中心`, `前往安全防护中心` | `HisecEndpointAgent.exe` | `HiSecEndpointAgent` |
| EDR security-center content and client-only controls | `EDRClient.exe` | `EDRClient` |

Every precise HiSec `click()` or `click_target()` call must pass
`expected_process_name`. The backend returns `click_context_required` when it
is omitted and `click_context_mismatch` when the connected process differs.

## Required Sequence

1. Make the target window visible.

   ```python
   agent.call_tool("activate_edr", {"wait": True, "timeout": 20})
   agent.call_tool("wait_window", {"process_name": "HisecEndpointAgent.exe", "timeout": 10})
   ```

2. Connect to the exact window.

   ```python
   agent.call_tool("connect", {
       "process_name": "HisecEndpointAgent.exe",
       "timeout": 10,
   })
   ```

   Use `EDRClient.exe` only when the requested control is inside the EDRClient
   window. For the HiSec entry window, always use `HisecEndpointAgent.exe`.

3. Dump the component tree.

   ```python
   tree = agent.call_tool("dump_tree", {"max_depth": 20}, timeout=40)
   controls = tree.get("controls") or []
   ```

4. Find candidate nodes from the tree and print the evidence.

   ```python
   candidates = [
       c for c in controls
       if (c.get("text") or c.get("title") or c.get("name")) == "安全中心"
   ]
   for c in candidates:
       print({
           "text": c.get("text"),
           "title": c.get("title"),
           "class_name": c.get("class_name"),
           "control_type": c.get("control_type"),
           "automation_id": c.get("automation_id"),
           "identifier": c.get("identifier"),
           "role": c.get("role"),
           "rectangle": c.get("rectangle"),
           "depth": c.get("depth"),
       })
   ```

5. Require a stable selector before clicking.

   Use a platform-native selector:

   - Windows: exact `automation_id`, then `control_type/class_name` and `text`
   - macOS: exact `identifier`, then `role/subrole` and
     `title/description/value`

   `control_id` is not a persistent selector. On macOS it is generated for the
   current tree traversal; on Windows it may also change between runs or app
   versions. The macOS `automation_id` compatibility field aliases
   `identifier`; it is not a Windows UIA AutomationId.

   If more than one candidate matches, do not click yet. Narrow by process,
   connected window, class/control type, parent, or automation id.

6. Click through a semantic component action.

   Windows `click()` prefers UIA semantic activation for ordinary interactive
   controls. HiSec left-navigation tabs such as `安全中心` are an exception:
   they are exposed as `CheckBox`, but UIA `toggle` can change state without
   switching the Qt page. For these tab-like controls, require `click_input`
   followed by page-content verification.

   ```python
   result = agent.call_tool("click", {
       "text": "安全中心",
       "class_name": "CheckBox",
       "automation_id": "<exact automation_id from dump_tree>",
       "parent_fallback": False,
       "expected_process_name": "HisecEndpointAgent.exe",
   })
   assert result.get("method") == "click_input", result
   ```

   macOS `click()` uses Accessibility data. Prefer `click` with a tree-derived
   selector. Use Swift AX helpers only when the MCP macOS backend lacks the
   needed action for that control.

7. Verify the click by dumping the tree again.

   ```python
   after = agent.call_tool("dump_tree", {"max_depth": 20}, timeout=40)
   texts = []
   for c in after.get("controls") or []:
       text = c.get("text") or c.get("title") or c.get("name")
       if text and text not in texts:
           texts.append(text)
   print(texts)
   ```

   Verification must use the resulting page contents, not only `"ok": true`
   from the click call.

## HiSec "安全中心" Compliance SOP

For the fixed sequence that clicks the left-side `安全中心` in
`HisecEndpointAgent` and collects compliance evidence, follow
`sops/hisec-security-center-compliance.md`. Keep selector, evidence, retry, and
report changes in that SOP rather than duplicating the workflow here.

## Windows UIA Notes

- `click()` should return `uia_invoke` or `uia_toggle` for ordinary `Button`,
  `CheckBox`, `RadioButton`, `TabItem`, `Hyperlink`, `MenuItem`, and `ListItem`
  controls when those controls expose UIA patterns.
- HiSec left-navigation `CheckBox` tabs are special: use `click_input` and
  verify the resulting page tree, because `toggle` alone may not change the Qt
  content page.
- `click_input` is a mouse-backed action even though it starts from a component.
  Treat it as a fallback, not as a successful component-tree click when precision
  matters.
- `coordinate_fallback`, `click_target`, `click_at`, and `click_window_at` are
  coordinate paths. Use them only after documenting why semantic activation is
  unavailable.
- Qt controls can have broad or surprising rectangles. Never trust rectangle
  center alone when a semantic pattern exists.

## macOS AX Notes

macOS uses Accessibility (AX) rather than UIA.

Do not copy selectors from Windows. Resolve the SOP's logical selector ID to a
macOS mapping built from `identifier`, `role/subrole`, and visible AX values.

Preferred order:

1. `connect` to the target process.
2. `dump_tree` or `find_control`.
3. `click` with an AX tree-derived selector.
4. Swift helper with `AXUIElementPerformAction(..., kAXPressAction)` only when
   the MCP backend cannot express the required action.
5. CGEvent center click only as the final fallback.

When a Swift helper is needed, search by app/process plus role/title/value, then
try `AXPress` before CGEvent.

## Anti-Patterns

Do not do these first:

- Click by raw screen coordinate because a screenshot "looks right".
- Click by text only when multiple windows or multiple controls can contain that
  text.
- Connect to `EDRClient.exe` when the requested control is in
  `HisecEndpointAgent.exe`.
- Treat `ok: true` from a click as proof. Always verify with a post-click
  `dump_tree`.
- Omit `expected_process_name` in a HiSec click, allowing a selector to run
  against whichever process was connected most recently.
- Hide a failed semantic action by falling back silently to coordinates.

## Minimal Evidence To Record

For every precise UI operation, keep these in logs or the report:

- Target process and top-level window title.
- Pre-click candidate node fields: text, class name, control type,
  automation id, depth, rectangle.
- Click result, especially `method`.
- Post-click visible texts or control count.
- Output report path if a report was generated.
