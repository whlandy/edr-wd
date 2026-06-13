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
           "class_name": c.get("class_name"),
           "control_type": c.get("control_type"),
           "automation_id": c.get("automation_id"),
           "rectangle": c.get("rectangle"),
           "depth": c.get("depth"),
       })
   ```

5. Require a stable selector before clicking.

   Preferred selector order:

   - `automation_id` exact match
   - `control_id` only if stable across runs
   - `text + class_name + control_type`
   - `auto_id_contains` or `auto_id_suffix` for generated IDs

   If more than one candidate matches, do not click yet. Narrow by process,
   connected window, class/control type, parent, or automation id.

6. Click through a semantic component action.

   Windows `click()` now prefers UIA semantic activation for interactive
   controls. A successful component click should report `method` as
   `uia_invoke` or `uia_toggle`, not `click_input` or `coordinate_fallback`.

   ```python
   result = agent.call_tool("click", {
       "text": "安全中心",
       "class_name": "CheckBox",
       "automation_id": "<exact automation_id from dump_tree>",
       "parent_fallback": False,
   })
   assert result.get("method") in {"uia_invoke", "uia_toggle"}, result
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

## HiSec "安全中心" Compliance Template

Use this template when the task asks to click the left-side "安全中心" in
`HisecEndpointAgent` and collect compliance data.

```python
import json
from pathlib import Path
from agent.subagent import TargetSubAgent

agent = TargetSubAgent.from_name("win-dev")
agent.initialize_mcp(force=True)

agent.call_tool("activate_edr", {"wait": True, "timeout": 20}, timeout=30)
agent.call_tool("connect", {
    "process_name": "HisecEndpointAgent.exe",
    "timeout": 10,
}, timeout=20)

tree = agent.call_tool("dump_tree", {"max_depth": 20}, timeout=40)
controls = tree.get("controls") or []
candidates = [
    c for c in controls
    if (c.get("text") or c.get("title") or c.get("name")) == "安全中心"
    and c.get("class_name") == "CheckBox"
]
if len(candidates) != 1:
    raise RuntimeError(f"Expected one 安全中心 node, got {len(candidates)}")

node = candidates[0]
click = agent.call_tool("click", {
    "text": "安全中心",
    "class_name": node.get("class_name"),
    "automation_id": node.get("automation_id"),
    "parent_fallback": False,
}, timeout=20)
if click.get("method") not in {"uia_invoke", "uia_toggle"}:
    raise RuntimeError(f"Click was not semantic UIA activation: {click}")

after = agent.call_tool("dump_tree", {"max_depth": 20}, timeout=40)
texts = []
for c in after.get("controls") or []:
    text = c.get("text") or c.get("title") or c.get("name")
    if text and text not in texts:
        texts.append(text)

report = {
    "window_process": "HisecEndpointAgent.exe",
    "clicked_node": node,
    "click_result": click,
    "visible_texts": texts,
    "tree": after,
}
Path("/tmp/edr-wd-hisec-security-center-report.json").write_text(
    json.dumps(report, ensure_ascii=False, indent=2),
    encoding="utf-8",
)
```

Expected post-click evidence for the compliance page includes texts like:

```text
管理员未配置有效策略
重新检查
账号安全检查
主机安全检查
设备安全检查
```

If the page still shows scan actions such as `快速扫描`, `全盘扫描`, or
`自定义扫描` as the main content, the click likely landed on the wrong page or
the old mouse-input path was used. Re-dump the tree and check the click method.

## Windows UIA Notes

- `click()` should return `uia_invoke` or `uia_toggle` for `Button`,
  `CheckBox`, `RadioButton`, `TabItem`, `Hyperlink`, `MenuItem`, and `ListItem`
  when those controls expose UIA patterns.
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
- Hide a failed semantic action by falling back silently to coordinates.

## Minimal Evidence To Record

For every precise UI operation, keep these in logs or the report:

- Target process and top-level window title.
- Pre-click candidate node fields: text, class name, control type,
  automation id, depth, rectangle.
- Click result, especially `method`.
- Post-click visible texts or control count.
- Output report path if a report was generated.
