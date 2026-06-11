# Phase 5: Cross-Platform Component Discovery and Action Space

## 1. Goal

Provide a cross-platform automation path that can:

- discover UI components by semantic attributes
- resolve components to on-screen targets
- click those components reliably
- expose a shared action space for click, drag, scroll, hover, and text input
- return structured results that can be used by the agent and tests

This phase exists because macOS targets do not have a pywinauto-like tree
inspector. In this repository, Windows remains the strongest discovery
baseline: `dump_tree`, exact control-id clicks, and control-oriented matching
already exist there. The macOS backend now has a first component-discovery
layer:

- `list_windows`
- `is_window_open`
- `activate_app`
- `connect`
- `dump_tree` through System Events Accessibility
- `find_control`
- selector-based `click` / `click_target`
- `click_at(x, y)` and other coordinate fallback actions

That gives macOS the same high-level workflow shape as Windows: find a
component, choose an action, then execute it with structured fallback data.

---

## 2. Problem Statement

The current macOS backend can answer:

- "Which windows are visible?"
- "Is a known window open?"
- "Can we activate an app?"
- "Can we click a screen coordinate?"
- "Which visible AX components are exposed by the connected app?"
- "Can a common component be clicked through a selector?"

The remaining gaps are:

- "Which ancestor is the best semantic click target?"
- "Can the backend use AXPress directly before coordinate fallback?"
- "Which component identifier is durable across app versions?"

This blocks robust automation for any target that needs:

- element-by-element navigation
- stable fallback when titles change
- repeatable interaction across screen sizes and resolutions

The design goal is therefore not only to click, but to **find then click**.

---

## 3. Design Principles

1. Prefer semantic selectors over coordinates.
2. Keep the macOS API structurally similar to the Windows backend where possible.
3. Preserve the ability to fall back to `click_at(x, y)` when Accessibility
   metadata is incomplete.
4. Return structured JSON for all discovery and click operations.
5. Never assume that a visible window is automatically frontmost or interactive.
6. Treat permissions as part of the contract, not as an implementation detail.

---

## 4. Current Constraints

### 4.1 No pywinauto-equivalent

macOS does not have a Windows-style UIA automation library in this project.
The implementation must use native macOS Accessibility primitives:

- `AXUIElement`
- `System Events`
- `CGWindowList` where useful
- AppleScript / `osascript` for bridging and activation

### 4.2 Existing backend limitations

The existing `macos_accessibility` backend now supports a conservative
System Events component tree, but it is still not as rich as the Windows UIA
path. In particular:

- `dump_tree` is available, but depends on Accessibility permission
- generated `control_id` values are per-dump stable enough for immediate use,
  but should not be treated as durable IDs across app restarts
- semantic clicks use rectangle-center fallback before deeper AXPress support
- macOS `click_at` is dry-run by default unless explicitly enabled

### 4.3 Permission requirements

Any component discovery or click implementation on macOS depends on:

- Accessibility permission
- Screen Recording permission for screenshots
- Automation / System Events permission in some flows

Without these permissions, the backend may see partial trees, generic wrapper
nodes, or no usable nodes at all.

---

## 5. Windows and macOS Parity

This design is intentionally cross-platform. The same high-level automation
concepts should work on both Windows and macOS, even though the underlying
implementation differs. The action-space model is intentionally inspired by
the `maa-fw` hermes branch, where primitive operations such as click, drag,
scroll, hover, and text entry are explicit first-class capabilities rather than
ad-hoc helper functions.

### 5.1 Windows side

Windows already has the stronger UI automation substrate in this repository:

- UIA-style tree inspection
- control-id based clicks
- structured window dumping
- better component-level targeting

Windows should remain the baseline for:

- `dump_tree`
- selector-to-control resolution
- exact control-id click paths
- regression tests for component-level selectors

### 5.2 macOS side

macOS currently lacks a pywinauto-equivalent component tree in this project,
so the backend must implement equivalent behavior using Accessibility APIs and
window geometry. macOS should support the same action vocabulary as Windows,
even when the selector resolution strategy differs.

### 5.3 Shared action space

The shared action space should contain the same primitive concepts on both
platforms:

- component click
- double click
- right click
- middle click
- drag
- scroll
- hover
- focus / activate window
- text input
- select / choose
- screenshot / inspect

The implementation may differ, but the semantics should remain aligned.

### 5.4 Reference model from `maa-fw`

The `maa-fw` hermes branch treats action primitives as a first-class contract
for both Windows and macOS workflows. This design borrows that idea directly:

- component discovery produces candidate targets
- the action space defines what can be done to a target
- the executor chooses the safest action first and falls back when needed

The main difference in EDR-WD is that Windows already has a stronger tree
inspector, while macOS must build the same semantics on top of Accessibility
metadata and geometry.

---

## 6. Proposed Architecture

### 6.1 New macOS component discovery layer

Add a discovery layer inside `target/automation/macos_accessibility.py` that can:

- enumerate the AX tree for the connected window or selected app
- normalize nodes into a platform-neutral schema
- search nodes by title, role, subrole, identifier, value, and text content
- resolve the best clickable ancestor for a matched node

Suggested normalized node schema:

```json
{
  "role": "AXButton",
  "subrole": null,
  "title": "前往安全防护中心",
  "value": null,
  "identifier": "security_center_button",
  "enabled": true,
  "visible": true,
  "frame": {"x": 120, "y": 460, "w": 180, "h": 36},
  "children": []
}
```

### 6.2 Selector model

Selectors should support a small, stable set of fields:

- `title_re`
- `title`
- `role`
- `subrole`
- `identifier`
- `value_re`
- `text_re`
- `parent_title_re`
- `ancestor_role`

The selector model should be explicit and deterministic: the same selector must
return the same top-ranked match when the UI tree is unchanged.

### 6.3 Click model

Add a macOS component click pipeline:

1. discover candidate nodes
2. rank by selector match quality and visibility
3. find the best clickable ancestor
4. prefer AXPress when available
5. fall back to center click using node geometry
6. fall back again to `click_at(x, y)` if the node cannot be resolved

The click result must include:

- matched node metadata
- click strategy used
- whether the click was semantic or coordinate-based
- any fallback reason

### 6.4 Window foregrounding

Before clicking a component, the backend must be able to:

- bring the target app to the front
- bring the target window to the foreground
- verify that the active window matches the discovered target

This is important because a component may exist in the tree but still not be
clickable if the window is backgrounded.

---

## 7. Proposed Backend APIs

The macOS backend can evolve toward the following capabilities:

### 7.1 Discovery APIs

- `dump_tree(window_title_re=None, max_depth=10)` for a normalized AX tree
- `find_control(selector)` for a single best match
- `find_controls(selector)` for multiple matches
- `is_window_open(...)` for window-level sanity checks

### 7.2 Interaction APIs

- `click(selector)` for semantic clicking
- `click_target(selector)` for strongly typed target clicking
- `click_at(x, y)` as a fallback
- `focus_window(selector)` or `activate_app(app_name=...)` for foregrounding

### 7.3 Debug APIs

- `diagnose_windows()` for platform-specific inspection
- `status()` should report whether the backend has sufficient permissions for
  component discovery

---

## 8. Matching Strategy

### 8.1 Primary matching order

1. explicit identifier
2. exact title
3. title regex
4. role + title
5. value / text
6. ancestor context
7. coordinate fallback

### 8.2 Best-match ranking

Score higher when a node is:

- visible
- enabled
- on the active window
- a direct semantic match
- a standard control type such as button, text field, checkbox, or menu item

Score lower when a node is:

- generic wrapper
- hidden
- offscreen
- only matched by broad regex

### 8.3 Clickable ancestor resolution

Many macOS AX trees expose labels and nested wrappers rather than the actual
clickable node. The backend should climb the parent chain to find the nearest
node supporting `AXPress`, `AXShowMenu`, or similar relevant actions.

This is essential for widgets where the visible text is not itself the button.

---

## 9. Fallback Strategy

Even after component discovery is implemented, some apps will still expose
incomplete Accessibility data.

The fallback order should be:

1. semantic selector click
2. clickable ancestor click
3. geometry-based click on the discovered node
4. absolute `click_at(x, y)`

If all fallback stages fail, the backend must return a structured error that
includes:

- selector used
- last matched node
- reason semantic click failed
- reason coordinate fallback failed

---

## 10. Action Space

### 10.1 What "action space" means here

For macOS automation, the action space is the set of atomic operations the
agent may choose from after it has discovered a target UI element or screen
region. It is not the task goal itself. It is the available control vocabulary
used to achieve the goal.

In practical terms:

- discovery finds candidate components
- the action space defines what can be done to them
- a policy or test runner chooses the next action

This is a useful distinction because "find the button" and "click the button"
are different phases. The discovery layer returns a stable target; the action
space describes the operations that can be applied to that target.

### 10.2 Recommended macOS action primitives

The macOS target should expose a small, composable set of action primitives:

- `click` / `click_at`
- `double_click`
- `right_click`
- `drag(start, end)`
- `drag_to(target)`
- `scroll(dx, dy)` or wheel-based scroll
- `hover`
- `press_key`
- `press_keys` / key chord
- `type_text`
- `select`
- `activate_app`
- `focus_window`

### 10.3 Component-scoped vs coordinate-scoped actions

Actions should be expressible in two ways:

1. **Component-scoped**: act on a discovered node, e.g. click a button.
2. **Coordinate-scoped**: act on a screen region, e.g. drag across a slider.

Component-scoped actions are preferred because they are more stable than
absolute coordinates. Coordinate-scoped actions remain necessary for:

- canvas-like UIs
- custom controls that lack Accessibility metadata
- drag-and-drop flows
- slider knobs
- region selection

### 10.4 Drag and gesture support

Dragging is a first-class action in macOS automation and should be modeled
explicitly. A drag operation typically needs:

- start point
- end point
- optional duration
- optional button/mouse type

Drag support is important for:

- list reordering
- slider adjustment
- file movement in Finder-like UIs
- selection rectangles

If a component exposes a frame but no direct semantic drag handle, the backend
should allow a coordinate-based drag fallback.

### 10.5 Action planning

The agent should not hard-code one action per goal. Instead it should:

1. discover the component
2. identify the action space available on that component
3. select the safest semantic action first
4. fall back to a geometry-based action only when necessary

This makes the system more like a small policy engine and less like a single
scripted click path.

---

## 11. Target-Specific Notes

### 11.1 HiSecEndpoint.app

HiSec on macOS has a two-stage activation flow:

- `HiSecEndpointAgent` entry window
- `EDRClient` target window

The discovery layer must support both windows and preserve the current
foregrounding behavior:

- launch the app bundle first when possible
- fall back to `HiSecEndpointAgent cmd ui`
- foreground the entry window before clicking
- click the "前往安全防护中心" component semantically when possible

### 11.2 Generic macOS apps

For apps like Finder, the new component discovery path should validate:

- toolbar buttons
- menu items
- text fields
- dialogs / sheets

The design should work without any HiSec-specific assumptions.

---

## 12. Test Strategy

### 12.1 Baseline generic tests

Add or extend tests that prove:

- tree enumeration works on standard macOS apps
- selector matching works on buttons and text fields
- coordinate fallback remains available

### 12.2 App-specific tests

For HiSec:

- locate the HiSec entry button by semantic selector
- foreground the entry window
- click the button
- verify the EDRClient window appears

### 12.3 Regression tests

Tests must cover:

- exact match
- regex match
- stale or background window
- missing Accessibility permission
- fallback to coordinate clicking

### 12.4 Acceptance criteria

The phase is complete when the backend can do all of the following on macOS:

- discover a component by semantic metadata
- click that component without manual coordinates
- fall back cleanly when the AX tree is incomplete
- return structured failure output when discovery fails

---

## 13. Security and Safety

Because clicking by selector can trigger destructive UI actions, the backend
must remain conservative:

- require explicit user intent for real clicks
- keep dry-run behavior as the default for generic coordinate clicks
- log the selected strategy and fallback path
- avoid silent clicks when the selector is ambiguous

If the selector resolves to multiple candidates with similar scores, the
backend should fail closed or require a stronger selector rather than
guessing.

---

## 14. Implementation Roadmap

### Step 1

Add AX tree enumeration and normalization for macOS connected windows.

Status: implemented as a conservative System Events based `dump_tree()` path.
The returned controls include role/title/description/value/identifier/enabled
state/frame/path/control_id fields.

### Step 2

Add selector-based search and ranking.

Status: implemented for `find_control()`, `click()`, and `click_target()` using
text, role/class_name, identifier/automation_id, and control_id selectors.

### Step 3

Add clickable ancestor resolution and semantic click execution.

Status: partially implemented. The current macOS path clicks the matched
control's rectangle center through the shared action-space fallback. Future
work should add AXPress and clickable ancestor resolution for controls where
the visible label is not itself the actionable node.

### Step 4

Integrate foregrounding and fallback coordinate click behavior.

Status: implemented for the connected process by setting the process frontmost
before AX enumeration and by routing clicks through `click_at()`.

### Step 5

Add regression tests for generic apps and HiSec-specific workflows.

### Step 6

Update `SKILL.md` and profile docs to describe the new macOS discovery and
click workflow.

---

## 15. Summary

macOS target automation currently has window-level control and coordinate
clicking, but not stable component discovery. Phase 5 closes that gap by
introducing Accessibility-based component lookup, semantic click execution,
and structured fallback logic. The result is a macOS target that can locate a
component by name or role and click it in a repeatable way, without requiring
manual coordinates for every workflow.
