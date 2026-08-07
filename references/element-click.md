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

## Scroll, Drag, And Paged Table Actions

Click is not the only GUI mutation an agent performs. `scroll`, `drag`, and
paged-table navigation have their own classifier and verification rules. The
design baseline is `docs/todo/scroll-and-paged-table-actions.md`; this section
is the operating form an agent must follow.

### Core Rule

`ok=true` from `scroll` or `drag` proves only that the pointer primitive was
sent. It does NOT prove the UI moved. A scroll-like request is not successful
until a re-observation shows visible content changed.

Do not treat every phrase such as "往下滑动" or "scroll down" as a raw wheel
action. Classify the target surface first.

### Intent Categories

- `wheel_scroll_region`: content inside a normal scrollable viewport must move.
  Use `scroll(clicks, x, y)` at the viewport center; verify by comparing a
  visible row/text/window snapshot before and after.
- `paged_table_next_page`: the user wants more records in a paginated table. Do
  NOT rely on wheel scrolling to cross page boundaries. Prefer clicking the
  table's next-page control (`nextPageButton` or equivalent), then verify the
  page number or first visible row range changed.
- `drag_scrollbar`: a scrollbar thumb is visible and wheel scroll does not move
  the content. Drag the thumb; verify viewport content changes.
- `drag_slider_or_thumb`: the target is a slider/seek bar/resize handle. Use a
  semantic control action if available, otherwise `drag`; verify value/position
  changed.
- `focus_then_scroll`: the region may ignore wheel events unless focused. Click
  a safe point inside the region with `expected_process_name`, then scroll at
  the same point; verify content changed.

### Decision Order

1. Identify the intended top-level window and `connect` to it.
2. `dump_tree(max_depth=...)` and locate the target region.
3. Classify the region: table/list with pagination controls ->
   `paged_table_next_page`; table/list with a scrollbar and no pagination ->
   `wheel_scroll_region`; visible scrollbar thumb -> `drag_scrollbar`;
   slider/handle -> `drag_slider_or_thumb`; unknown bounded region ->
   `focus_then_scroll`.
4. Establish ownership: prefer `lock_window` when active-window verification
   works. If RDP/window-lock verification fails, use a safe focus click with
   `expected_process_name` and re-check top-level windows.
5. Execute one bounded scroll/drag/page action.
6. Re-observe and verify visible content changed.
7. If no change: do NOT repeat raw scroll blindly; switch to next-page or
   scrollbar strategy when available; return a structured
   `no_scroll_effect` result if no reliable fallback exists.

**Bounded attempts.** Never loop a failed strategy. Cap the whole
strategy chain (wheel -> scrollbar -> focus-then-scroll -> pagination) at
`MAX_SCROLL_ATTEMPTS` (default 3). When every strategy has been tried and
nothing verified a change, stop and report `no_scroll_effect` — a frozen page
or blocked window must not produce an unending scroll loop.

### Paginated Table (HiSec `日志中心 -> 操作日志`)

The motivating case: `EDRClient.exe`, `日志中心` window, `操作日志` tab,
`pagedTable.tableView` with `prePageButton`/`nextPageButton` and hundreds of
rows split across pages. Wheel scrolling moves only within the current page's
visible rows; older records require `nextPageButton`.

Preferred flow:

```python
before = visible_first_row_or_page_number()
agent.call_tool(
    "click",
    {
        "automation_id": "nextPageButton",
        "expected_process_name": "EDRClient.exe",
    },
)
after = visible_first_row_or_page_number()
```

Pass condition: page number changes, OR first visible row timestamp/text moves
to the next expected range, OR next/previous button enabled state changes as
expected.

Fallback order: if semantic `click` cannot resolve `nextPageButton`, use
`click_target` on the uniquely matched button; use `click_window_at` only after
recording the window rectangle and target point.

### RDP / Window-Lock Fallback (Focus Then Scroll)

Use only when strict window lock fails because active-window detection is
unreliable and the target process/window is already identified:

```python
agent.call_tool("unlock_window", {})
agent.call_tool(
    "click_at",
    {"x": cx, "y": cy, "expected_process_name": "EDRClient.exe"},
)
result = agent.call_tool("scroll", {"clicks": -8, "x": cx, "y": cy})
```

Safety: the point must be inside the target window's screen rectangle and the
intended table/list/viewport; `expected_process_name` must be set for the focus
click; re-observe `list_windows` or a screenshot if there is any doubt about
overlap/frontmost state.

### Dragging A Scrollbar

Use only after detecting a visible scrollbar thumb:

```python
before = visible_region_signature()
agent.call_tool(
    "drag",
    {"x1": thumb_x, "y1": thumb_y, "x2": thumb_x, "y2": thumb_y + 80},
)
after = visible_region_signature()
```

Pass condition: visible content moves in the intended direction, and/or the
scrollbar thumb position changes.

### Scroll Verification

Every scroll-like action should verify at least one of: first visible row text
changed; first visible row timestamp changed; page number changed; scroll thumb
position changed; visible item index/range changed; screenshot perceptual
digest changed inside the target region; control-tree normalized digest changed
for the table/list region. For the `操作日志` table, dump visible rows before
and after and compare the first visible `操作时间`; if it does not change after
a bounded wheel scroll, click `nextPageButton` and verify the page/row range
changes.

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
- Treat `scroll.ok=true` or `drag.ok=true` as user-visible success without a
  post-action observation change.
- Rely on raw wheel scrolling to reach records on another page of a paginated
  table; use the pagination controls instead.
- Repeat raw wheel scroll blindly after observing no effect; switch strategy or
  return `no_scroll_effect`.
- Loop a failed scroll strategy past `MAX_SCROLL_ATTEMPTS`; a frozen/blocked
  page must terminate in `no_scroll_effect`, never an unbounded retry loop.
- Fall back to coordinates for a scroll-like action without recording the
  window rectangle and intended region.

## Minimal Evidence To Record

For every precise UI operation, keep these in logs or the report:

- Target process and top-level window title.
- Pre-click candidate node fields: text, class name, control type,
  automation id, depth, rectangle.
- Click result, especially `method`.
- Post-click visible texts or control count.
- Output report path if a report was generated.
