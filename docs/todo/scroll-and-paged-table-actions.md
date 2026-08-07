# Scroll, Drag, And Paged Table Remaining TODO

## Status

Partially implemented.

The original scroll design has been reduced to the unfinished work only. The
completed PR1/PR2/PR3 baseline is now code, not TODO:

- `target/scroll/results.py`: `ScrollResult`, `Reason`, and `Strategy`.
- `target/scroll/observer.py`: before/after observation diffing.
- `target/scroll/scroll_and_verify.py`: one-dispatch verify wrapper.
- `target/scroll/policy.py`: bounded `ScrollPolicy` and single-attempt executor.
- `target/scroll/state_machine.py`: deterministic scroll state machine.
- `target/scroll/detect.py`: `PageDetector` and `PageStructure`.
- `target/scroll/controller.py`: `PageController`.
- `target/scroll/verifier.py`: `PageVerifier` and `ScrollCoordinator`.
- `target/scroll/backend.py`: backend bridge for the composite chain.
- `target/server.py`: MCP `scroll_region` tool.
- `test_case/test_scroll/`: current unit/contract coverage.

Verification gate at cleanup time:

```bash
python -m pytest test_case/test_scroll -q
```

Result: `141 passed`.

## Remaining Work

### T2: `page_table`

Add a pagination-only MCP/composite tool.

Required behavior:

- Dispatch only semantic pagination clicks (`gui.click`, A020).
- Never emit `pointer.scroll` (A029) or `pointer.drag` (A028).
- Use `PageDetector` once to confirm `PAGINATED` or `LOAD_MORE`.
- Return `NOT_DISPATCHED` for `FLAT`, `INFINITE`, `TREE_LAZY`, disabled next,
  or no enabled pagination owner.
- Verify page movement with `PageVerifier`/`Observer`.
- Keep one page turn per call; draining all pages remains a caller loop.

Suggested signature:

```python
page_table(
    table_ref: dict | None = None,
    process_name: str | None = None,
    direction: str = "next",  # "next" | "prev" | "first"
    verify: bool = True,
    max_attempts: int | None = None,
) -> ScrollResult
```

Tests to add:

- PAGINATED next emits exactly one A020 click and returns moved when verified.
- LOAD_MORE emits exactly one A020 click.
- FLAT/INFINITE/TREE_LAZY emit no pointer primitive and return
  `NOT_DISPATCHED`.
- Disabled next/prev returns `NOT_DISPATCHED`.
- Verification churn does not count as movement.

### T3: `scroll_until_visible`

Add a target-driven bounded loop.

Required behavior:

- Probe for the target before the first dispatch.
- Each iteration composes the existing `scroll_region`/policy step plus one
  target probe.
- Stop immediately on `NOT_DISPATCHED` or `NO_SCROLL_EFFECT`.
- Count only moved-but-unmatched steps against `max_steps`.
- Translate `direction="down"` to next / negative wheel clicks.
- Translate `direction="up"` to prev / positive wheel clicks.
- Never report success unless the target text/control was actually observed.

Suggested signature:

```python
scroll_until_visible(
    target_text_re: str,
    region_ref: dict | None = None,
    direction: str = "down",  # "down" | "up"
    max_steps: int = 8,
    verify: bool = True,
) -> ScrollResult
```

Tests to add:

- Target already visible performs zero dispatches.
- Target on page N performs exactly N bounded advances.
- Target never appears terminates within `max_steps`.
- `direction="down"` maps to negative wheel clicks and pagination next.
- `direction="up"` maps to positive wheel clicks and pagination previous.
- `verify=False` never returns `moved=True`.

### T4: `drag_target`

Add a drag-specific composite for sliders, handles, splitter bars, resize grips,
list reordering, and precise virtual-list nudges.

Required behavior:

- Resolve `target_ref` to a control rectangle.
- Verify window ownership before dispatch.
- Compute exactly one drag from a safe grab point to a release point.
- Dispatch only `pointer.drag` (A028).
- Never fall back to wheel or semantic click.
- Verify actual state/position/content movement via `Observer`.

Suggested signature:

```python
drag_target(
    target_ref: dict,
    dx: int | None = None,
    dy: int | None = None,
    x2: int | None = None,
    y2: int | None = None,
    grab: str = "handle",  # "handle" | "center"
    verify: bool = True,
    expected_process_name: str | None = None,
) -> ScrollResult
```

Tests to add:

- Invalid endpoint arguments fail without dispatch.
- Unlocked or mismatched window fails without dispatch.
- A dry-run/no-op drag cannot return `moved=True`.
- Thumb/handle controls use the handle rect, not rail center.
- Drag success requires an observation change.

## Remaining Design Decisions

- Decide whether `verify=False` should keep returning `NO_SCROLL_EFFECT` or add
  a distinct `Reason.UNVERIFIED`.
- Decide whether target-already-visible needs a distinct
  `Reason.TARGET_VISIBLE` instead of `NOT_DISPATCHED`.
- Decide how forced `strategy` conflicts with detected structure are reported;
  likely add `Reason.COMPOSITION_MISMATCH`.
- Decide horizontal scroll support. Current normalized directions are
  `next`, `prev`, `first`, plus tool-facing `down`/`up`.
- Improve churn filtering so transient spinner/reflow changes do not count as
  content movement.
- Improve virtualized-list verification where row reuse may keep the tree
  digest stable.
- Define platform-specific scrollbar-thumb coordinate extraction for Windows
  UIA and macOS AX.
- Decide whether `AutomationBackend.drag(...)` should accept
  `expected_process_name`, or whether `drag_target` relies only on
  `verify_window_lock`.

## Live E2E Still Needed

- Windows HiSec `日志中心 -> 操作日志`: detect pagination, click next page,
  verify first-row timestamp/page changed, and report before/action/after.
- Windows RDP occlusion case: another window covers the target coordinate; the
  action must recover safely or return a target-ownership failure.
- macOS generic scroll area: focus region, bounded scroll, verify visible
  content changed or return `NO_SCROLL_EFFECT`.
