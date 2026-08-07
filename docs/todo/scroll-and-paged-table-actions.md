# Scroll, Drag, And Paged Table Actions

## Status

Design todo. This document records the dedicated scroll/drag behavior that is
not fully captured by the generic action catalog.

The current implementation already exposes these tools:

- `drag(x1, y1, x2, y2, duration=0.25)`
- `scroll(clicks, x=None, y=None)`

The missing layer is intent-aware behavior for pages where "scroll down" can
mean one of several different operations:

- wheel-scroll a normal scrollable region.
- drag a scrollbar thumb or slider.
- click a pagination button such as `nextPageButton`.
- refocus a table before scrolling.
- recover when window-lock active detection is unreliable in RDP sessions.

## Source Incident

The motivating live case was the Windows HiSec `日志中心 -> 操作日志` page.

Observed window/control facts:

- Top-level window: `日志中心`
- Process: `EDRClient.exe`
- Page/tab: `操作日志`
- Table: `pagedTable.tableView`
- Pagination: `prePageButton`, `nextPageButton`, page-size combo box
- Visible columns: `操作条目`, `操作类型`, `操作来源`, `操作时间`
- Record count: hundreds of rows, split across pages

The first attempt used `scroll(clicks=-5, x=520, y=340)` at the table center.
The tool returned `ok=true`, but the user did not see meaningful movement.

Root causes:

- The target was the `日志中心` window, not the main `华为HiSec Endpoint`
  window.
- `pyautogui.scroll` sends wheel events to the topmost window at the pointer
  location. If another window is frontmost or overlaps the point, the event can
  be delivered to the wrong surface.
- Windows RDP active-window detection can make strict `window_lock` verification
  fail even when the desired window is visible.
- The table is paginated. Wheel scrolling can move only within the current
  page's visible row area; to view older records across pages, the correct
  semantic action is `nextPageButton`, not more wheel clicks.

## Existing Action Mapping

Current action catalog entries:

| Action ID | Code | Tool | Meaning |
|---|---|---|---|
| `pointer.drag` | `A028` | `drag` | pointer drag between two screen coordinates |
| `pointer.scroll` | `A029` | `scroll` | wheel scroll by signed clicks, optionally at a point |

Current MCP tool contract:

```python
scroll(clicks: int, x: int | None = None, y: int | None = None)
drag(x1: int, y1: int, x2: int, y2: int, duration: float = 0.25)
```

Current backend behavior:

- Windows: `pyautogui.moveTo()` then `pyautogui.scroll()` or
  `pyautogui.dragTo()`.
- macOS: same primitive behavior, but real mouse actions are dry-run unless
  `EDR_WD_ALLOW_REAL_CLICKS=1`.
- Both platforms check `window_lock` before pointer primitives when a lock is
  present.

## Required Intent Model

The agent should not treat every user phrase such as "往下滑动" or "scroll down"
as a raw wheel action. It should first classify the target surface.

### Intent Categories

`wheel_scroll_region`

- The user wants content inside a normal scrollable viewport to move.
- Use `scroll(clicks, x, y)` at the viewport center.
- Verify by comparing visible row/text/window snapshot before and after.

`paged_table_next_page`

- The user wants to see more records in a paginated table.
- Prefer clicking the table's next-page control.
- Verify page number or visible row timestamp/text changes.

`drag_scrollbar`

- A scrollbar thumb is visible and wheel scroll does not move the content.
- Drag the scrollbar thumb down/up.
- Verify viewport content changes.

`drag_slider_or_thumb`

- The target is a slider, seek bar, resize handle, or other draggable control.
- Use semantic control action if available; otherwise use `drag`.
- Verify value/position changed.

`focus_then_scroll`

- The target region may ignore wheel events unless focused.
- Click a safe point inside the region with `expected_process_name`, then
  scroll at the same point.
- Verify content changes.

## Decision Algorithm

Use this order before executing a scroll-like request:

1. Identify the intended top-level window.
2. `connect` to the window or process.
3. `dump_tree(max_depth=...)` and locate the target region.
4. Classify the region:
   - table/list with pagination controls -> `paged_table_next_page`.
   - table/list with scrollbar and no pagination -> `wheel_scroll_region`.
   - visible scrollbar thumb -> `drag_scrollbar` fallback.
   - slider/handle -> `drag_slider_or_thumb`.
   - unknown but bounded region -> `focus_then_scroll`.
5. Establish target ownership:
   - prefer `lock_window` when active-window verification works.
   - if RDP/window-lock verification fails, use safe focus click with
     `expected_process_name` and re-check top-level windows.
6. Execute one bounded scroll/drag/page action.
7. Re-observe and verify that visible content changed.
8. If no change:
   - do not repeat raw scroll blindly.
   - switch to next-page or scrollbar strategy when available.
   - return a structured "no_scroll_effect" result if no reliable fallback
     exists.

## Recommended Execution Recipes

### Normal Scrollable Region

```python
agent.call_tool("connect", {"title_re": "<window title>", "timeout": 10})
agent.call_tool("lock_window", {"title_re": "<window title>", "activate": True})
before = agent.call_tool("dump_tree", {"max_depth": 8})
result = agent.call_tool("scroll", {"clicks": -5, "x": cx, "y": cy})
after = agent.call_tool("dump_tree", {"max_depth": 8})
```

Pass condition:

- `result.ok is True`
- at least one visible row/text/control position changed

### RDP Fallback: Focus Then Scroll

Use this only when strict window lock fails because active-window detection is
unreliable, and the target process/window is already identified.

```python
agent.call_tool("unlock_window", {})
agent.call_tool(
    "click_at",
    {"x": cx, "y": cy, "expected_process_name": "EDRClient.exe"},
)
result = agent.call_tool("scroll", {"clicks": -8, "x": cx, "y": cy})
```

Safety requirements:

- The point must be inside the target window's screen rectangle.
- The point must be inside the intended table/list/viewport.
- `expected_process_name` must be set for the focus click.
- Re-observe the `list_windows` or screenshot if there is any doubt about
  overlap/frontmost state.

### Paginated Table

For controls like `pagedTable.tableView`, do not rely on wheel scrolling to
show the next page of records.

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

Pass condition:

- page number changes, or
- first visible row timestamp/text changes to the next expected range, or
- previous/next button enabled state changes as expected

Fallback:

- If semantic `click` cannot resolve `nextPageButton`, use `click_target` on the
  uniquely matched button.
- If the button cannot be resolved semantically, use `click_window_at` only
  after recording the window rectangle and target point.

### Dragging A Scrollbar

Use only after detecting a visible scrollbar thumb.

```python
before = visible_region_signature()
agent.call_tool(
    "drag",
    {"x1": thumb_x, "y1": thumb_y, "x2": thumb_x, "y2": thumb_y + 80},
)
after = visible_region_signature()
```

Pass condition:

- visible content moves in the intended direction.
- scrollbar thumb position changes if observable.

## Verification Strategy

`ok=true` from `scroll` or `drag` proves only that the pointer primitive was
sent. It does not prove the UI moved.

Every scroll-like action should verify one of these:

- first visible row text changed.
- first visible row timestamp changed.
- page number changed.
- scroll thumb position changed.
- visible item index/range changed.
- screenshot perceptual digest changed inside the target region.
- control tree normalized digest changed for the table/list region.

For the `日志中心 -> 操作日志` table, the strongest available verification is:

- dump visible table rows before and after.
- compare the first visible `操作时间`.
- if the first timestamp does not change after bounded wheel scroll, click
  `nextPageButton` and verify the page/row range changes.

## Proposed Tool Extensions

These are not implemented yet; they are the target shape for a robust
scroll/drag layer. When implemented, they must satisfy the enforceability
constraints below, not just the signatures.

### Enforceability Constraints (Code Review)

These are hard contracts for the implementation, adopted from review of the
operating docs. Without them the docs remain advisory.

1. **Verify is not optional.** A composite scroll action must return a result
   that separates "primitive dispatched" from "content changed", so a caller
   cannot treat a bare `ok=true` as success. Shape:

   ```python
   from dataclasses import dataclass
   from enum import Enum

   class Reason(Enum):
       NEXT_PAGE = "next_page"
       WHEEL_MOVED = "wheel_moved"
       SCROLLBAR_DRAGGED = "scrollbar_dragged"
       FOCUS_THEN_SCROLL = "focus_then_scroll"
       NO_SCROLL_EFFECT = "no_scroll_effect"   # dispatched, but nothing verified as moved
       NOT_DISPATCHED = "not_dispatched"       # primitive/control action was not sent

   @dataclass(frozen=True)
   class ScrollResult:
       dispatched: bool   # pointer primitive or control action was sent
       moved: bool        # verification observed real content change
       reason: Reason

       def __post_init__(self) -> None:
           # moved cannot be true if nothing was dispatched.
           if self.moved and not self.dispatched:
               raise ValueError("moved=True requires dispatched=True")
           # no_scroll_effect is inherently a "dispatched but unmoved" outcome.
           if self.reason is Reason.NO_SCROLL_EFFECT and self.moved:
               raise ValueError("reason=NO_SCROLL_EFFECT requires moved=False")
           if self.dispatched and self.reason is Reason.NOT_DISPATCHED:
               raise ValueError("reason=NOT_DISPATCHED requires dispatched=False")

       @property
       def success(self) -> bool:
           """The only success predicate. Moved content is the goal;
           a dispatched-but-unmoved scroll is NOT a success."""
           return self.dispatched and self.moved

       @property
       def outcome(self) -> str:
           # log/test convenience only; `reason` remains the authoritative
           # fine-grained signal.
           if self.success:
               return "moved"
           return "dispatched_only" if self.dispatched else "not_dispatched"
   ```

   The enum names above are the proposed member set for PR1; extend it during
   review if a new failure class emerges (every new member must gate on a test).
   `SUCCESS` is NOT an enum member — success is derived as `dispatched and
   moved`, never stored.

   The low-level MCP tools stay backward compatible and report only dispatch.
   Concretely, the underlying envelope is the existing `ActionReceipt`
   (`target/action_dispatcher/receipts.py`): every `dispatch()` call returns one
   with fields `ok`, `code`, `action_id`, `action_code`. The primitives
   `pointer.scroll` (catalog id **A029**) and `pointer.drag` (catalog id
   **A028**) in `target/action_catalog/actions_v1.py` return `ok=true` purely as
   a dispatch signal — their `ActionReceipt` carries no movement/verification
   notion. Verification is the composite layer's job. Prefer that each
   verification failure/outcome maps onto a distinct `Reason` member, and use
   the *composite* tools whenever `moved` matters — a bare primitive `ok=true`
   is never evidence of UI movement.

   **Invariants (testable):**
   - `not success implies (not dispatched or not moved)` — derivable, but a
     white-box test asserts `ScrollResult(dispatched=True, moved=False,
     reason=Reason.NO_SCROLL_EFFECT).success is False`.
   - `dispatched=True` is reachable while `moved=False` (dispatch succeeded but
     verification saw no change); this is the `no_scroll_effect` case, a
     *distinct* outcome from "dispatch failed" (`dispatched=False`,
     `reason=NOT_DISPATCHED`).
   - `reason == NO_SCROLL_EFFECT` implies `moved is False` — enforced in
     `__post_init__` and asserted again in a test that constructs
     `ScrollResult(dispatched=True, moved=True, reason=Reason.NO_SCROLL_EFFECT)`
     and expects a `ValueError`.
   - `moved` implies `dispatched` — same enforcement (an unmoved-but-never-
     dispatched result is invalid, not merely "not_dispatched").
   - Every `reason` value is a `Reason` enum member (the field is typed
     `Reason`, so a bare string is a type error); callers match on
     `Reason.NO_SCROLL_EFFECT`, never a hand-typed string.
   - `moved` is computed by the `Observer`'s diff/convergence check (see
     enforceability item 6); the composite tools only *construct* the
     `ScrollResult` from `Observer` output. The primitives
     (`pointer.scroll` A029 / `pointer.drag` A028) never return a
     `ScrollResult` at all.

   **Open questions for the implementer:**
   - Should `moved` be strict (the digest changed *at all*) or tolerance-based
     (digest changed beyond a per-surface epsilon, e.g. a partially-visible row
     appearing)? Strict equality can flip on a transient render artifact; a
     blanket epsilon can mask a genuine half-page. Recommend an explicit
     `tolerance` parameter on the verify step, defaulting to "any change", and
     revisit after the first HiSec regression run.
   - Where does `ScrollResult` live? It must be importable by both the composite
     tool layer and the test suite without pulling in an MCP transport
     (suggest a pure `result_models` module beside `receipts.py`).

2. **Bounded attempts, via a `ScrollPolicy` (not a hard-coded constant).** Any
   retry/failover loop over strategies (wheel -> scrollbar drag -> focus-then-
   scroll -> pagination) must be bounded per-UI and terminate with
   `reason=Reason.NO_SCROLL_EFFECT` when every dispatchable strategy ran but none
   verified a change. Never re-try the same failed strategy endlessly, and never
   loop a frozen page. A module-level integer is too rigid: browser scroll,
   table paging, and virtual lists need different limits, so the bound travels
   with the caller, not the loop.

   Define the breadth of one "attempt" precisely: **an attempt = dispatch of one
   strategy + one observer verify**. The chain below is walked front-to-back,
   at most `policy.max_attempts` times total, each strategy appearing at most
   once per invocation (no re-running a strategy that just failed to move).

   ```python
   from enum import Enum
   from dataclasses import dataclass

   class Strategy(Enum):
       WHEEL            = "wheel"            # primitive: pointer.scroll (A029)
       SCROLLBAR_DRAG   = "scrollbar_drag"   # primitive: pointer.drag   (A028)
       FOCUS_THEN_SCROLL = "focus_then_scroll"  # composite: activate + A029
       PAGINATION       = "pagination"       # semantic: click next-page control

   @dataclass(frozen=True)
   class ScrollPolicy:
       max_attempts: int = 3          # defaults must mirror MAX_SCROLL_ATTEMPTS
       strategy_order: tuple[Strategy, ...] = (
           Strategy.WHEEL, Strategy.SCROLLBAR_DRAG,
           Strategy.FOCUS_THEN_SCROLL, Strategy.PAGINATION,
       )
       # per-surface policy; e.g. a virtual list may raise max_attempts for a
       # long wheel run, while a paginated table needs exactly one PAGINATION.

   def scroll_with_policy(policy: ScrollPolicy, verify, dispatch) -> ScrollResult:
       # dispatch(strategy) -> ActionReceipt (from target/action_dispatcher/dispatch.py)
       # verify()          -> bool, owned by the Observer diff engine (item 6)
       dispatched_any = False
       for strategy in policy.strategy_order[: policy.max_attempts]:
           receipt = dispatch(strategy)
           if receipt.ok:
               dispatched_any = True
               if verify():
                   return ScrollResult(dispatched=True, moved=True, reason=...)
       # bounded exhaustion
       if dispatched_any:
           return ScrollResult(dispatched=True, moved=False,
                               reason=Reason.NO_SCROLL_EFFECT)
       return ScrollResult(dispatched=False, moved=False,
                           reason=Reason.NOT_DISPATCHED)
   ```

   The success return is gated on `receipt.ok` **and** `verify()` — `dispatched`
   is only ever `True` when a receipt actually armed; a strategy that failed to
   dispatch (`ok=False`) can never contribute to a `moved=True` result, and its
   outcome is only visible via the exhaustion branches (`dispatched_any`).

   Two termination classes are intentionally distinct (mirrors item 1):
   - **exhausted-but-armed** — at least one strategy sent but none moved →
     `dispatched=True, moved=False, reason=Reason.NO_SCROLL_EFFECT`;
   - **nothing-dispatchable** — the whole chain never armed (e.g. the next-page
     control and the scroll region both resolved to nothing) → `dispatched=False,
     moved=False, reason=Reason.NOT_DISPATCHED`. Do NOT report `NO_SCROLL_EFFECT`
     for this second case; it was never dispatched.

   Mapping to the current code: the primitives come from the catalog
   `target/action_catalog/actions_v1.py` — wheel is `pointer.scroll` (**A029**),
   scrollbar drag is `pointer.drag` (**A028**); both return an `ActionReceipt`
   whose `ok`/`action_id`/`action_code` (`target/action_dispatcher/receipts.py`)
   signals *dispatch only*, never movement (see item 1). `FOCUS_THEN_SCROLL` and
   `PAGINATION` are composite — they reuse the primitives/semantic actions and
   are **not** in the catalog. There is **no `ScrollPolicy` or strategy-executor
   code in the tree today**; this entire block is the PR2 proposal, not existing
   behavior mapped from `target/automation/base.py`.

   `MAX_SCROLL_ATTEMPTS` in `SKILL.md` (operating doc) names the agent-side
   default for the bounded strategy chain; its value (3) is pinned by this
   doc's acceptance criteria and feeds `ScrollPolicy.max_attempts` as the
   fallback. The code must take a `ScrollPolicy` so different UIs can override
   it.

   Note on the default chain length: the default `max_attempts=3` is *shorter*
   than the default `strategy_order` (4 strategies), so under the default config
   `PAGINATION` would be unreachable. That is intentional at the surface level —
   a paginated table overrides `strategy_order` to start at `PAGINATION` (one
   dispatch), and a pure wheel surface overrides `max_attempts` upward for long
   runs. The default `(max_attempts=3, full order)` combination is therefore a
   conservative cap, not a statement that every surface reaches pagination. Any
   instrumented test must pick a policy whose `max_attempts >= len(strategy_order)`
   when it wants the full chain exercised.

   **Invariants (testable):**
   - The loop performs at most `policy.max_attempts` strategy dispatches; a test
     injects a verify that always returns `False` and asserts the dispatch spy
     was called `<= max_attempts` times and the returned
     `ScrollResult(dispatched=True, moved=False, reason=NO_SCROLL_EFFECT)`.
   - No strategy is dispatched twice in one invocation when it does not verify —
     assert the ordered dispatch log contains each `Strategy` at most once.
   - Exhaustion with at least one armed dispatch yields `NO_SCROLL_EFFECT`, never
     `NOT_DISPATCHED`; exhaustion with zero armed dispatches yields
     `NOT_DISPATCHED`, never `NO_SCROLL_EFFECT` (the `Reason` is derived from
     `dispatched_any`, matching item 1's `__post_init__` contract).
   - `policy.strategy_order[:policy.max_attempts]` binding is per-surface: a
     paginated table policy (`max_attempts=1`, order starting at `PAGINATION`)
     issues exactly one dispatch; a virtual-list policy may allow more.
   - `reason` is always a `Reason` member (never a bare string), consistent with
     item 1's invariant that callers match on `Reason.NO_SCROLL_EFFECT`.

   **Open questions for the implementer:**
   - Does `max_attempts` cap total dispatches, or a *per-strategy* repetition
     budget (a long virtual-list wheel run is one `WHEEL` strategy but may need
     several wheel steps)? The proposal above counts one attempt per strategy
     dispatch. Note that multi-clicks are already expressible within a *single*
     A029 dispatch — `target/automation/base.py`'s `scroll(clicks: int, ...)`
     takes a signed click count — so a second field (e.g.
     `wheel_steps_per_attempt`) may be redundant: prefer encoding larger wheel
     moves in one `A029` `clicks` value and keep `max_attempts` as a per-strategy
     dispatch cap. Resolve before PR2.
   - Should `PAGINATION` sit last in `strategy_order` unconditionally, or should
     the order be decided by the `PageDetector`-driven state machine (item 4 /
     PR3)? Items 4 and PR3 assume detection runs first; keep the default order
     above as the agent-side conservative default and let `ScrollPolicy` reorder
     per surface.
   - `ScrollResult(dispatched=True, moved=True, reason=...)` must carry which
     strategy actually moved (marker left as `...` above) — either a distinct
     `Reason` member per strategy or a separate `strategy_used` field; decide so
     the outcome/log can attribute the successful path.

3. **Structured-content handling is generic, not hard-coded, and named for the
   general case.** Do not bind the classifier to one page name or one control
   (`nextPageButton`, `pagedTable.tableView`, `操作日志`). `TableNavigator` is
   too narrow a name — pagination is one instance of structured content that
   also includes infinite scroll, tree lazy-load, and load-more buttons. Prefer
   `ContentNavigator` (or `StructuredScroller`); a table is just one strategy
   under it. Keep `ContentNavigator` thin by splitting it into three
   collaborating roles rather than one growing class, with `ContentNavigator`
   as the coordinating facade that owns the Detector -> Controller -> Verifier
   sequence:

   ```python
   class PageStructure(Enum):
       PAGINATED      # numbered page controls / next+prev buttons
       INFINITE       # virtualized or append-on-scroll list
       TREE_LAZY      # expand-on-demand tree, sibling rows hidden until expanded
       LOAD_MORE      # explicit "show more / load more" button at list end
       FLAT           # no structured paging detected (plain scrollable region)

   # Bridge to item 2's Strategy enum (consumed by ScrollPolicy.strategy_order).
   # Runs once after classification and feeds the item-4 state machine.
   def structure_to_strategy(s: PageStructure) -> Strategy: ...
       # PAGINATED -> Strategy.PAGINATION
       # INFINITE   -> Strategy.WHEEL          (repeated wheel = scroll-append)
       # TREE_LAZY  -> Strategy.FOCUS_THEN_SCROLL
       # LOAD_MORE  -> Strategy.PAGINATION     (single click, one dispatch)
       # FLAT       -> Strategy.WHEEL / Strategy.SCROLLBAR_DRAG (per-surface)

   class DetectedControl:
       target: Target
       enabled: bool                        # per-control, NOT collapsed

   class DetectionResult:
       structure: PageStructure
       next_controls: tuple[DetectedControl, ...]  # of 0..n next/load-more
       prev_controls: tuple[DetectedControl, ...]
       confidence: float                    # 0.0..1.0; also breaks PAGINATED vs LOAD_MORE ties

   class PageDetector:                     # pure: reads, writes nothing
       def detect(self, snapshot) -> DetectionResult: ...
       # has_pagination / has_next_page / is_infinite_scroll are thin
       # helpers over one `detect()` pass, NOT separate heuristics.

   class PageController:                   # pure: plans, delegates dispatch
       def next(self, result: DetectionResult) -> list[ActionReceipt]: ...
       def advance(self, result: DetectionResult) -> list[ActionReceipt]: ...
       def reset_to_first(self, result: DetectionResult) -> list[ActionReceipt]: ...
       # returns the ordered primitive receipts (A028 / A029 / semantic click
       # gui.click A020) to perform the step, targeting the single *enabled*
       # owner control; never calls the resolver itself. An empty list means
       # "nothing to dispatch" (NOT_DISPATCHED, see item 1).

   class PageChange(Enum):                 # tri-state, NOT bool (see open questions)
       CHANGED      # content / visible window genuinely moved
       UNCHANGED    # snapshot stable, no move
       UNCERTAIN    # could not decide (e.g. virtualized row reuse); caller keeps
                    # stepping for INFINITE, but must not report success on it

   class PageVerifier:                     # pure: reads observer, writes nothing
       def page_changed(self, before, after, threshold) -> PageChange: ...
       # delegates to the Observer diff engine (item 6): compares
       # ObservationSnapshot.tree_digest (content-addressed sha256 over target
       # fingerprints, target/observations/models.py) plus the OCR/AX text
       # slice, gated by threshold so transient re-render churn is not a move.
       # Returns UNCERTAIN when a virtualized list recycles rows in place
       # (tree_digest equal) yet the window may have shifted.

   class ContentNavigator:                 # facade: wires the three roles + observer
       def __init__(self, detector, controller, verifier, observer): ...
       def step(self, direction: Literal["next","prev","first"],
                snapshot, policy: ScrollPolicy) -> ScrollResult: ...
       # ONE bounded iteration: detect -> map structure to Strategy ->
       # controller plan -> dispatch receipt(s) -> verifier. This is the loop
       # body PR2's scroll_with_policy drives over strategy_order; scroll_with_policy
       # delegates the per-attempt dispatch+verify to step.
   ```

   A strategy must be chosen by structure ("does a next-page control exist and
   is it enabled?", "is the list virtualized?"), not by a hard-coded automation
   id. `nextPageButton` remains only as one HiSec instance of a detected
   pagination control. The choice is a decision on `DetectionResult.structure`
   + `confidence` (only enabled owner controls count), mapped through
   `structure_to_strategy` into the item-2 `Strategy` set and resolved by the
   item-4 state machine — not by string-matching a control name. Detector ->
   Controller -> Verifier is a clean pipeline and keeps each role small.

   **Mapping to current code (ground truth):** the primitives come from the
   catalog `target/action_catalog/actions_v1.py` — wheel is `pointer.scroll`
   (**A029**, `scroll(clicks: int, x: Optional[int]=None, y: Optional[int]=None)`
   at `target/automation/base.py`), and a load-more / next-button click is the
   semantic `gui.click` **A020** action already in the catalog;
   `pointer.drag` (**A028**) covers the scrollbar-drag case for a `FLAT`
   region. `PageController` emits these as `ActionReceipt`s (from
   `target/action_dispatcher/dispatch.py`) and never moves a pointer itself.
   The Verifier's content-change signal already exists as
   `ObservationSnapshot.tree_digest` / `Target.fingerprint` in
   `target/observations/models.py` — the Observer (item 6) compares snapshots
   before/after a `PageController.step`. **There is no `PageDetector`,
   `PageController`, `PageVerifier`, or `ContentNavigator` code in the tree
   today** — PR3 is the proposal, not existing behavior mapped from
   `target/automation/base.py`.

   **Invariants (testable):**
   - Pure roles are referentially transparent: `detect(snapshot)` with the same
     snapshot returns equal `DetectionResult`; `PageController`/`PageVerifier`
     hold no pointer/UI state and are unit-testable with a stubbed observer and
     a captured snapshot fixture.
   - `PageController.next()` returns at most one `ActionReceipt` per detected
     next-control, and `[]` when the chosen *owner* `DetectedControl.enabled`
     is `False` (the controller targets the single enabled owner; a test with
     a disabled next-button asserts `[]` → `NOT_DISPATCHED`).
   - `ContentNavigator.step` composes exactly Detector → Controller → Verifier
     once per call: a spy asserts the call order and that the receipt armed in
     Controller actually reached the dispatcher before Verifier ran.
   - Generic fixtures: one `PAGINATED` table, one `INFINITE` virtual list, one
     `LOAD_MORE` list, and one `FLAT` region must each classify the same way on
     user/alert/asset/operation screens (no classifier keyed to a table id).
   - A scrollbar-drag handle and a `FLAT` region must classify as `FLAT` (→
     `WHEEL`/`SCROLLBAR_DRAG`), NOT as `PAGINATED`/`TREE_LAZY` (guards the
     item-5 regression invariant against slider/handle misclassification).

   **Open questions / risks for the implementer:**
   - **Virtualized-list UNCERTAIN handling:** `PageVerifier.page_changed`
     returns `UNCERTAIN` when a virtualized `INFINITE` list recycles rows in
     place (`tree_digest` and OCR text unchanged) yet the window may have
     shifted. Resolve before PR3 which observable the verifier uses to disambiguate
     (scroll-position / first-visible-row signal), and how `ContentNavigator.step`
     + PR2's `scroll_with_policy` treat `UNCERTAIN`: keep stepping for an
     `INFINITE` surface but never report `moved=True` on a bare `UNCERTAIN`
     (a no-op must not count as success).
   - **Control resolution cost:** `detect()` resolves controls through the same
     resolver the dispatcher uses; running it per `step()` on a large table is
     O(visible targets) and may be slow. Decision: cache `DetectionResult` for
     the lifetime of a `ContentNavigator` session (invalidated only by a
     verified page change), or re-detect each step. If cached, a tree that lazy
     loads a sibling mid-session must invalidate the cache.
   - **`LOAD_MORE` vs `PAGINATED` overlap:** a surface with both a next-button
     and a trailing load-more button must pick one owner in `DetectionResult`
     (highest `confidence`) — define the tie-break (PAGINATED wins) before the
     state machine consumes both control groups.
   - **Verifier threshold semantics:** `threshold` is per-strategy (OCR vs AX
     vs digest); a single scalar is insufficient when AX reports a rename on a
     row the digest says is stable. The Observer diff engine (item 6) must
     expose per-strategy verdicts so `page_changed` can combine them without
     duplicating strategy logic in `PageVerifier`.

4. **Decision order runs as a state machine.** Model the whole composite
   lifecycle `DISCOVER -> CLASSIFY -> OWNERSHIP -> EXECUTE -> VERIFY ->`
   (`FALLBACK` loops back to `EXECUTE`) with three terminal outcomes
   (`SUCCESS | NO_SCROLL_EFFECT | NOT_DISPATCHED`) as a single small
   deterministic state machine, not a free-form paragraph, so coverage of every
   fallback path is testable by transition, not by eyeballing control flow.
   Prefer an explicit transition loop over a chain of `if/elif`:

   ```python
   class Step(Enum):
       DISCOVER   # resolve the target region / active window (per surface)
       CLASSIFY   # PageDetector.detect(snapshot) -> DetectionResult (item 3 / PR3)
       OWNERSHIP  # pick the single *enabled* owner control; map structure -> Strategy
       EXECUTE    # one bounded strategy attempt (item 2's scroll_with_policy body)
       VERIFY     # PageVerifier.page_changed -> PageChange (items 3/6)
       FALLBACK   # non-terminal: strategy did not move; advance strategy_order, -> EXECUTE
       SUCCESS    # terminal: moved=True
       NO_SCROLL_EFFECT  # terminal: >=1 strategy dispatched, none moved (exhausted order)
       NOT_DISPATCHED    # terminal: nothing ever armed (no eligible control/strategy)

   def transition(s: Step, ctx: NavigateContext) -> Step:
       # ctx carries the current DetectionResult, remaining strategy_order,
       # ScrollPolicy, snapshot pair, and the partial ScrollResult accumulator.
       # Pure with respect to dispatch: it only DECIDES; side-effecting
       # dispatch/verify happen in the EXECUTE/VERIFY handlers, not in
       # transition(). Returns the next Step; injecting a stub ctx of any step
       # yields a deterministic, assertable transition table.
       ...

   state = Step.DISCOVER
   while state not in {Step.SUCCESS, Step.NO_SCROLL_EFFECT, Step.NOT_DISPATCHED}:
       state = transition(state, ctx)
   ```

   **How the machine composes the other items (so it does not duplicate them):**
   - `DISCOVER -> CLASSIFY` resolves the region once; re-entering `DISCOVER` is
     only legal after a verified page change (cache-invalidation hook, item 3).
   - `CLASSIFY -> OWNERSHIP` runs exactly one `PageDetector.detect(snapshot)`
     pass; `OWNERSHIP` maps `DetectionResult.structure` through
     `structure_to_strategy(s)` into the item-2 `Strategy` set and selects the
     enabled owner control (item-3 tie-break: highest `confidence`, `PAGINATED`
     wins over `LOAD_MORE`).
   - `EXECUTE` is **not** a new loop — it is the bounded body of item 2's
     `scroll_with_policy(policy, dispatch, verify)`: one dispatch of the current
     `Strategy` in `strategy_order`, capped at `ScrollPolicy.max_attempts`.
     `EXECUTE` never iterates strategies itself; advancing to the next strategy
     is the job of the single `FALLBACK -> EXECUTE` edge below, so the strategy
     iteration lives in exactly one place (no second loop in `transition`).
   - `VERIFY` is **mandatory, never optional** (item 1): every `EXECUTE` that
     armed a receipt must be followed by a `VERIFY`. The machine consumes
     `PageVerifier.page_changed(before, after, threshold) -> PageChange`
     (item 3); `PageVerifier` itself delegates the raw snapshot comparisons to
     the Observer diff engine — `Observer.content_changed(...) -> bool` and
     `Observer.diff(...)` (item 6) — rather than reimplementing the strategies.
     Map `PageChange.CHANGED -> moved=True`; `UNCHANGED`/`UNCERTAIN -> moved=False`.
     The machine only transitions to `SUCCESS` when
     `ScrollResult(dispatched=True, moved=True, reason=<strategy>)`. A
     `PageChange.UNCERTAIN` must not set `moved=True` (a no-op never counts as
     success) — it keeps stepping only for an `INFINITE` surface.
   - `FALLBACK` is **not** terminal: it advances to the next `Strategy` in
     `strategy_order` and returns to `EXECUTE`. When the order is exhausted it
     resolves to exactly one of the two distinct terminal classes from item 2:
     `NO_SCROLL_EFFECT` (at least one strategy dispatched, none moved) or
     `NOT_DISPATCHED` (nothing ever armed — no eligible control/strategy).

   **Invariants (testable):**
   - Determinism: `transition(s, ctx)` with equal `(s, ctx)` always returns the
     same next `Step` — a table-driven test enumerates every
     `(state, ctx-class)` edge and asserts the expected successor, giving full
     statement/transition coverage of the fallback paths without a real UI.
   - Single owner: at most one control is dispatched per `EXECUTE`; the machine
     never dispatches both a next-button and a load-more in one step.
   - Termination: only `SUCCESS`/`NO_SCROLL_EFFECT`/`NOT_DISPATCHED` are
     terminal; `FALLBACK` is an intermediate edge back to `EXECUTE`. The loop is
     provably bounded by `strategy_order` length * `max_attempts` — no unbounded
     re-entry once `FALLBACK` exhausts the order.
   - Single advanced strategy per `FALLBACK`: exactly one `Strategy` position
     advances between consecutive `EXECUTE`s; the transition table has exactly
     one STRATEGY-ITERATION edge, never two.
   - No UI state in `transition`: all side effects are confined to the
     EXECUTE/VERIFY handlers so the machine itself is unit-testable with a
     stubbed observer and captured snapshot fixtures (mirrors the pure-role rule
     of item 3).

   **Mapping to current code (ground truth):** the EXECUTE handler is the only
   place that touches the catalog `target/action_catalog/actions_v1.py`
   primitives — `pointer.scroll` (**A029**) and `pointer.drag` (**A028**) — and
   the semantic `gui.click` (**A020**) for a next/load-more control, each
   returning an `ActionReceipt` via `target/action_dispatcher/dispatch.py` whose
   `ok` signals *dispatch only, never movement* (item 1). **There is no
   `Step` machine, `transition`, or `NavigateContext` code in the tree today** —
   this block is the PR2 proposal, not a mapping of existing behavior. PR2 lands
   the machine with the `WHEEL` strategy only; `CLASSIFY`/`OWNERSHIP` are stubbed
   to `FLAT -> WHEEL` until PR3 adds the `PageDetector`.

   **Open questions / risks for the implementer:**
   - **EXECUTE = `scroll_with_policy` (recommended, but confirm signature).** Per
     the `EXECUTE` bullet above, item 2's `scroll_with_policy` is the handler and
     it owns `max_attempts`/`NO_SCROLL_EFFECT` — the machine does not re-bind the
     loop. Residual risk: avoid creating a second `EXECUTE` entry point that
     re-implements the bound; if the machine needs per-strategy verify gating,
     add it to `scroll_with_policy` rather than around it, so the transition
     table keeps exactly one STRATEGY-ITERATION edge (item-5 regression hazard).
   - **FALLBACK ordering by structure:** whether `strategy_order` is the static
     item-2 default or reordered by `DetectionResult.structure` (open in item 2)
     must be resolved before PR3 so the `OWNERSHIP`-phase map into
     `strategy_order` is not re-decided in `transition`.
   - **Caching vs. re-`DISCOVER`:** re-entering `DISCOVER` per step is
     O(visible targets); if `DetectionResult` is cached for the session the
     cache must be invalidated on a verified page change — tie this to the item-3
     control-resolution cost decision rather than deciding twice.

5. **Regression invariants.** The composite layer is a superset of today's
  primitives, never a behavior change. Verify the following against the real
  current code (`target/action_catalog/actions_v1.py` **A028** `pointer.drag`,
  **A029** `pointer.scroll`; `target/automation/base.py` `scroll(clicks,
  x=None, y=None)`, `drag(x1, y1, x2, y2, duration=0.25)`, and the
  `lock_window` / `verify_window_lock` family) on every PR:

  - **R0 — No behavior change without pagination.** For a `FLAT`/plain
    scrollable region (no structural pagination controls), a composite
    `scroll_region` must dispatch the *identical* primitive(s) and honor the
    *same* gating as the raw `pointer.scroll` (A029) path today: same
    `clicks` sign/order, same `x`/`y` routing, and the same `window_lock`
    precondition (both A028 and A029 `requires=("window_lock",)`). The only
    addition is the Observer's verification *after* dispatch — dispatching is
    unchanged and the returned `ScrollResult.moved` can never cause extra
    input. A golden test runs the pre-existing `scroll` recipe (lines above)
    and asserts byte-for-byte the same set of dispatched `ActionReceipt`s.

  - **R1 — No misclassification of draggable controls.** A slider, seek bar,
    resize handle, or scrollbar thumb must resolve to `drag_slider_or_thumb`
    (→ `pointer.drag`, A028), **never** to `page_table` / pagination. A
    `DetectionResult` whose `structure` is `PAGINATED` must be produced only
    when structural pagination controls actually exist — a draggable handle
    must never emit a `PAGINATED` classification. A `FLAT` region exposing a
    scrollbar thumb stays `FLAT` and, per the item-4 `CLASSIFY` contract
    (`FLAT → WHEEL/SCROLLBAR_DRAG`, per-surface), maps to `WHEEL` first, or to
    `SCROLLBAR_DRAG` when wheel provably fails to move — never to a next-page
    `EXECUTE`. The two paths are disambiguated by the fixture set: slider
    control, resize handle, scrollbar thumb, and a genuinely paginated table.
    Assert each yields its expected `Strategy`/action code and that none of
    the draggable ones ever reaches a next-page `EXECUTE`.

  - **R2 — RDP/window-lock fallback unchanged.** When `verify_window_lock`
    fails (RDP active-window detection unreliable), the composite must use the
    *same* safe fallback the raw path uses today: `unlock_window` → focus
    click at an in-rect point with `expected_process_name` set → bounded
    wheel scroll (the documented "Focus Then Scroll" recipe, which is
    scroll-only; a scrollbar-drag follow-up, if any, is an additional later
    dispatch, not part of "the same fallback"), and must re-observe
    `list_windows`/screenshot if frontmost state is in doubt. The composite
    must not silently drop the lock check, add an extra lock/unlock cycle, or
    re-order the fallback; a test asserts the fallback dispatch sequence is
    identical to the current non-composite recipe.

  - **R3 — `window_lock` precondition never weakened.** The composite must
    require the same lock state A028/A029 gate on today (`requires=
    "window_lock"`). It may call `lock_window(strict=...)` itself, but it must
    not dispatch a pointer primitive while unlocked, and must not introduce a
    path that bypasses the lock (e.g. by falling back to a raw drag with
    `window_lock` absent). Assert: every pointer dispatch in every strategy is
    covered by an acquired/verified lock.

  **Test plan (mirrors the Test Plan section):** the regression suite is a
  differential test — run the current recipe and the composite on the same
  fixture and diff the `ActionReceipt` stream and lock state transitions. Add
  table-driven cases for R1 across the draggable-control fixture set.

  **Open risk for the implementer:** R1 is the sharpest edge — a drag handle
  is visually near a scrollbar thumb, and `PageDetector`'s structural
  heuristics must be proven to stop at `FLAT` (item 3's contract) before any
  real table can be reached. If a control is *both* a handle and part of a
  resizable table chrome, define the precedence explicitly (recommend: if a
  draggable handle is detected, `drag_slider_or_thumb` wins over pagination)
  or the classifier becomes a source of silent mis-navigation.

6. **Verify ownership: an observer / diff-engine layer, not a method inside the
   composite action.** Which component implements `verify()` is the open design
   point. Preferred: a dedicated `Observer` that can run multiple diff strategies —
   accessibility-tree diff (snapshot `tree_digest`), fingerprint diff (`Target.fingerprint`),
   and later a screenshot digest diff — and returns a verdict independent of the action
   that dispatched the input. The composite action composes `dispatch` + `observer.diff()`,
   so verification logic is reusable and not duplicated per action.

   **The `Observer` builds on the existing observation machinery; it is NOT a new
   snapshot format.** Today `target/observations/models.py` already defines
   `ObservationSnapshot` and `Target` (with `tree_digest` per snapshot and per-target
   `fingerprint` / `fingerprint_fields`), and `target/observations/fingerprint.py`
   computes the identity-only `fingerprint` (`sha256:<hex>` over the *identity*
   fields, excluding `rect`/position/ownership). The `Observer.snapshot()` must
   therefore return/reuse `ObservationSnapshot` (or a light wrapper) rather than
   invent a parallel schema — diff strategy is selected by *how* two snapshots are
   compared, not by a new snapshot type. `screenshot_evidence_id` currently defaults
   to `None` (screenshots land in P1.4), so a screenshot-digest diff strategy is a
   *proposed* extension of the same `Observer`, not present today.

   ```python
   # target/observations — proposed; no Observer code exists in the tree yet.
   from target.observations.models import ObservationSnapshot, Target

   class DiffStrategy(Enum):           # enum over How we compare, not a new schema
       TREE_DIGEST    = "tree_digest"   # ObservationSnapshot.tree_digest equality
       FINGERPRINT    = "fingerprint"   # per-target Target.fingerprint set
       SCREENSHOT     = "screenshot"    # proposed (needs P1.4 screenshot evidence)

   @dataclass(frozen=True)
   class Diff:                         # observer output; comparison inputs are strategy-dependent
       changed: bool                   # a content change was detected (moved signal)
       strategy: DiffStrategy
       before: DigestRef               # TREE_DIGEST/SCREENSHOT: scalar digest; FINGERPRINT: set
       after: DigestRef                #   of per-target fingerprints (see FINGERPRINT caveat below)
       churn: bool = False             # True ⇒ change attributed to transients, not content

   DigestRef = str | frozenset[str]    # single digest, or a set of per-target fingerprints

   class Observer:                     # observer / diff-engine layer
       def snapshot(self) -> ObservationSnapshot: ...
       def diff(self, before, after, *, strategy=DiffStrategy.TREE_DIGEST) -> Diff: ...
       def content_changed(self, before, after,
                           *, strategy=DiffStrategy.TREE_DIGEST,
                           tolerance: float | None = None) -> bool: ...
                           # tolerance: None ⇒ "any change counts" (item 1 default);
                           # a positive epsilon ⇒ only changes beyond it count.

   # composite action
   before = observer.snapshot()
   dispatched = dispatch(primitive_or_control)   # -> ActionReceipt (dispatch.py)
   after = observer.snapshot()
   moved = observer.content_changed(before, after)
   return ScrollResult(dispatched, moved, reason)
   ```

   **Verify is not optional here.** `moved` in every `ScrollResult` is produced only
   through this `Observer`; no composite tool computes `moved` from a screen
   coordinate delta or a raw action `ok`. A scroll primitive (`pointer.scroll` A029 /
   `pointer.drag` A028) never yields `moved` — a receipt's `ok` means *dispatched*,
   not *moved* (see item 1's `dispatched` vs `moved` separation).

   **Invariants (testable):**
   - `Observer.snapshot()` returns an `ObservationSnapshot`-compatible object whose
     `tree_digest` is stable across two snapshots taken with no UI change (byte-stable,
     same as `fingerprint.py` determinism).
   - **Per the selected `strategy`** (never auto-detected): for `TREE_DIGEST`, two equal
     `tree_digest`s ⇒ `changed=False`; two different digests ⇒ `changed=True`. The diff
     never "implicitly" picks a strategy — the caller passes `strategy` explicitly and
     it must be valid for the chosen strategy's inputs (`diff` must raise on a `before`
     for a strategy it cannot compare).
   - `content_changed` returns `True` iff the chosen diff *strategy* reports a change
     and the change is not attributed to churn.
   - `Diff.churn=True` never propagates to `moved=True`: a spinner / transient
     animation that changes a sub-region but not stable content is filtered. **Valid
     for a strategy with locality** (`FINGERPRINT` set or `SCREENSHOT` sub-region);
     for `TREE_DIGEST` (a single scalar) churn cannot be distinguished from content
     and must default to `churn=False` until the open churn decision (below) lands.

   **Open risks for the implementer:**
   - **Churn vs. content is strategy-dependent and not yet settled.** `tree_digest`
     changes iff any target's identity fields change — an animation that *reflows* a
     control's `title`/`text` would be a false `moved`. Decide whether to (a) compare
     only a filtered identity subset, (b) add the `tolerance` parameter from item 1
     (default "any change"), or (c) require the `PageVerifier` (PR3) to disambiguate.
     Do not leave `content_changed`'s false-positive behavior undefined — it is the
     single most correctness-critical knob in the whole design.
   - **`Target.fingerprint` is cheap but `tree_digest` is one scalar.** A `tree_digest`
     collision-free `changed` signal works, but when only *one* row's fingerprint
     shifts you cannot tell *where*. If differencing needs locality (e.g. PR3's
     verifier), `Observer.diff` must surface per-target `Target.fingerprint` changes,
     not just the aggregate digest.
   - **Where the `Observer` lives.** It must be importable by the composite tool layer
     and the test suite without pulling in an MCP transport (same constraint as
     `ScrollResult` in item 1 — prefer `target/observations/`, co-located with the
     snapshot/fingerprint it compares).


### Implementation Sequencing (three PRs, do not merge into one)

Keep the action dispatcher from becoming a monolith — land in this order, each
with its own tests and independently reviewable:

1. **PR1 – Result contract + composite action**: `ScrollResult`, `Observer`
   baseline (snapshot/diff/content_changed), and `scroll_and_verify` + tests.
   Establishes the "dispatch vs. moved" contract and verify ownership.

   **Scope (explicitly NOT in PR1):** no `ScrollPolicy`, no strategy chain /
   state machine, no `PageDetector`/`PageController`/`PageVerifier`, no
   pagination logic. `scroll_and_verify` in PR1 is a *single-strategy* composite
   (resolve one region → dispatch one primitive → verify) that returns the
   contract for the loop that PR2 layers on top. Land the contract and its
   tests *first* so PR2/PR3 build on stable semantics.

   **Deliverables (all under `target/`, with unit tests in the same PR):**
   - `ScrollResult` dataclass (`dispatched: bool`, `moved: bool`,
     `reason: Reason`) with the `__post_init__` invariants from enforceability
     item 1: `NO_SCROLL_EFFECT ⇒ moved is False`; `moved ⇒ dispatched`;
     `NOT_DISPATCHED ⇒ dispatched is False`; the
     `success == dispatched and moved` predicate; `Reason` typed enum with **no**
     `SUCCESS` member (derived, never stored). Reuse `dataclass(frozen=True)`
     style already used by `ActionReceipt` (`target/action_dispatcher/receipts.py`)
     and `AssignmentResult` (`target/observations/assignment.py`).
   - `Observer` baseline — a thin adapter over the *existing* observation
     machinery, **not** new snapshot/fingerprint code: `snapshot()` wraps
     `build_snapshot()` (`target/observations/snapshot.py`) and returns the
     existing `ObservationSnapshot` (`tree_digest`, `targets`); `content_changed`
     keeps item 6's exact signature `content_changed(before, after, *,
     strategy=DiffStrategy.TREE_DIGEST, tolerance: float | None = None) -> bool`
     (pin it in PR1 so PR3's churn/tolerance work does not churn this API) and is
     `before.tree_digest != after.tree_digest` for the `TREE_DIGEST` strategy
     (the only strategy required for PR1; `FINGERPRINT`/`SCREENSHOT`/`Diff`/
     `DigestRef`/`churn` from enforceability item 6 are PR1-optional and may be
     stubbed or deferred — do not ship dead branches). The `tree_digest` is
     already a `sha256:<hex>` over every target's fingerprint
     (`target/observations/models.py`), so equality is byte-stable.
   - `scroll_and_verify(...)` composite: snapshot `before`, dispatch exactly one
     primitive, snapshot `after`, compute `moved`, return
     `ScrollResult(dispatched, moved, reason)`. `dispatched` is derived from the
     `ActionReceipt.ok` returned by `dispatch.py`; **PR1 must NOT invent a
     multi-primitive fallback** (that is PR2's strategy chain). `reason` comes
     from the item-1 mapping: `receipt.ok is False ⇒ dispatched=False,
     reason=NOT_DISPATCHED`; `ok is True` and `moved is False ⇒
     reason=NO_SCROLL_EFFECT`; `moved is True ⇒ reason` is the marker for the
     primitive actually used (see the PR2 open question on `strategy_used` /
     per-strategy `Reason` members — PR1 keeps it to whatever marker it ships,
     and the decision must not break PR2's attribution).

   **Grounding in current code (verified):** the primitives it dispatches are
   the catalog specs `pointer.drag` (**A028**) / `pointer.scroll` (**A029**) in
   `target/action_catalog/actions_v1.py`; both are declared `side_effect=
   "gui_mutation"` and return an `ActionReceipt` whose `ok`/`code`/`action_id`/
   `action_code` signal *dispatch only*. `scroll`'s implementation
   (`target/automation/base.py`) takes a signed `clicks: int`, so a multi-step
   wheel move stays a single A029 dispatch — do not loop in PR1. Neither
   primitive nor `receipts.py` carries any movement/verification notion.

   **Acceptance (testable):**
   - White-box `ScrollResult` constructor tests for each item-1 invariant
     (successful moved, dispatched-only `NO_SCROLL_EFFECT`,
     invalid `(moved=True, dispatched=False)` raises, invalid
     `(moved=True, reason=NO_SCROLL_EFFECT)` raises, invalid
     `(dispatched=True, reason=NOT_DISPATCHED)` raises).
   - `Observer` determinism: two `snapshot()` calls with no UI change yield equal
     `tree_digest`; `content_changed` is `False` for equal digests and `True` for
     two hand-built snapshots with differing digests.
   - `scroll_and_verify` against a mock dispatcher: a `receipt.ok=False` path
     returns `dispatched=False, reason=NOT_DISPATCHED`; an `ok=True` path where
     the observer reports no change returns `dispatched=True, moved=False,
     reason=NO_SCROLL_EFFECT`, and `success is False`; an `ok=True` path where
     the observer reports a change returns `moved=True, success is True` with
     `reason` set to the marker chosen for the primitive used (value TBD per the
     attribution open question below).
   - Fallback test-reactant: a real `pointer.scroll` A029 on a live window with
     `clicks>0` must not be required to move content for the test to pass — the
     contract holds regardless of UI state.

   **Open questions / risks to resolve before/within PR1:**
   - **`Reason` attribution on success (blocks PR2).** PR2 needs to know which
     strategy moved. Decide in PR1 whether to reuse the *existing* item-1 `Reason`
     members that already name the mechanism (`WHEEL_MOVED`, `SCROLLBAR_DRAGGED`,
     … — do not invent new parallel names like `SCROLLED`/`DRAGGED`) or to add a
     separate `strategy_used` field on `ScrollResult`. Prefer the field: it avoids
     overloading `reason`'s outcome axis and keeps the item-1 enum stable.
     Whatever is chosen must be final by PR2 (item 4's state machine matches on it).
   - **Observer locality / churn is deferred.** PR1 ships only `TREE_DIGEST`
     (no `churn` filtering, no per-target locality). Any test/user path that
     needs "spinner didn't count as movement" must wait for item 6 — document
     this limitation in `scroll_and_verify`'s docstring so callers don't assume
     churn-resistance in PR1.
   - **`scroll_and_verify` signature not yet fixed.** Whether it takes a
     pre-resolved region or resolves internally, and how `verify: bool =
     True` interacts with the `Observer` (item: `verify=False` skips the after
     snapshot and forces `moved=False`?) must be pinned here so PR2's loop and
     the `T1..T4` tool wrappers share one call shape.
2. **PR2 – State machine + bounded strategy**: `ScrollPolicy` + the
   `DISCOVER -> CLASSIFY -> OWNERSHIP -> EXECUTE -> VERIFY` (`FALLBACK` ->
   `EXECUTE`) transition loop, the `NO_SCROLL_EFFECT` / `NOT_DISPATCHED`
   termination, and tests. Builds on PR1's `ScrollResult`/`Observer`
   contract; **no pagination logic yet**.

   **Scope (explicitly NOT in PR2):** no `PageDetector`,
   `PageController`, `PageVerifier` (`ContentNavigator`), no `DetectionResult`
   / structural detection beyond the hard-coded `FLAT` stub (those land in
   PR3). PR2 ships the loop shell and the bounded strategy executor over PR1's
   single-strategy `scroll_and_verify`; it does **not** re-invent verification
   or the result contract. `scroll_with_policy`'s `verify` callback stays
   owned by the PR1 `Observer` (`content_changed`, item 6) — PR2 adds no new
   snapshot/diff code.

   **Deliverables (all under `target/`, with unit tests in the same PR):**
   - `ScrollPolicy` frozen dataclass from enforceability item 2: `max_attempts:
     int = 3` and `strategy_order: tuple[Strategy, ...]` with the four-member
     `Strategy` enum (`WHEEL`, `SCROLLBAR_DRAG`, `FOCUS_THEN_SCROLL`,
     `PAGINATION`). Reuse `dataclass(frozen=True)` style (as PR1 does via
     `ActionReceipt`/`AssignmentResult`). Default `max_attempts=3` must mirror
     `MAX_SCROLL_ATTEMPTS` (pinned to `3` by this doc's acceptance criteria);
     the default is *shorter* than the 4-member order by design (a paginated
     surface overrides `strategy_order` to start at `PAGINATION`; a long wheel
     surface raises `max_attempts`). No module-level integer lives in the loop —
     the bound always travels with the caller.
   - `scroll_with_policy(policy: ScrollPolicy, verify, dispatch) ->
     ScrollResult` — the **single-attempt** strategy executor, not a loop: one
     `EXECUTE` = **one strategy dispatch + one `verify()`** for the current
     position in `strategy_order`. (Signature order pinned here as
     `(policy, verify, dispatch)` to canonicalize the conflict between item 2's
     `(policy, verify, dispatch)` and item 4's `(policy, dispatch, verify)`;
     use keyword args at every call site so order cannot silently drift.)
     It returns: `moved=True` only when a receipt armed **and** `verify()` is
     `True`; `dispatched=True, moved=False, reason=NO_SCROLL_EFFECT` when the
     current attempt armed but did not move (drives `FALLBACK`,
     **it is not a terminal return from `scroll_with_policy` — termination is
     owned by the machine** when the order is exhausted); `dispatched=False,
     moved=False, reason=NOT_DISPATCHED` when the current strategy could not be
     armed. Success `reason` must set the PR1-pinned attribution marker (the
     chosen `strategy_used` field or the mechanism-named `Reason` member —
     whatever PR1 finalized) so item 4's state machine can match on it.
   - The item-4 `Step` enum + pure `transition(s: Step, ctx:
     NavigateContext) -> Step` state machine is **the only loop** in PR2: it
     drives strategy iteration on the single `FALLBACK -> EXECUTE` edge —
     `EXECUTE` calls `scroll_with_policy` once for the current position, and a
     `NO_SCROLL_EFFECT`/empty result on that position advances to the next via
     `FALLBACK`; `EXECUTE` never loops internally a second time. This resolves
     the item-2-vs-item-4 ambiguity (item 2's free-standing `scroll_with_policy`
     loop sketch is superseded by the machine's single-attempt contract; do not
     ship both). `VERIFY` is mandatory (never skipped) for any `EXECUTE` that
     armed a receipt; `PageChange.UNCERTAIN` never sets `moved=True`. Terminal
     states are exactly `SUCCESS | NO_SCROLL_EFFECT | NOT_DISPATCHED`; total
     dispatches are provably bounded by `len(strategy_order) * max_attempts`
     (each of the ≤ `len(strategy_order)` positions re-entered at most
     `max_attempts` times — this is item 4's formula and it is the correct
     bound under the single-attempt model).
   - **Stub boundaries for PR2:** `CLASSIFY`/`OWNERSHIP` run the hard-coded
     `FLAT -> WHEEL` mapping (no `PageDetector` yet); `PAGINATION` and
     `FOCUS_THEN_SCROLL` strategies may be stubbed to "resolve to nothing" so
     the machine's fallback/termination paths are still exercised by tests
     without PR3's detection. Do not ship dead strategy branches beyond these
     stubs.

   **Grounding in current code (verified):** `dispatch` in
   `scroll_with_policy` routes through `target/action_dispatcher/dispatch.py`,
   hitting the catalog specs `pointer.drag` (**A028**) / `pointer.scroll`
   (**A029**) in `target/action_catalog/actions_v1.py`, both `side_effect=
   "gui_mutation"`, returning an `ActionReceipt` whose `ok` signals *dispatch
   only, never movement*; each `verify()` call wraps the PR1 `Observer`. The
   `FLAT -> WHEEL` stub dispatches `pointer.scroll` A029 with a signed
   `clicks` (single dispatch — no per-scroll looping, per `target/automation/
   base.py`). **There is no `ScrollPolicy`, `Step`, `transition`, or
   `NavigateContext` code in the tree today** — this is the PR2 proposal, not a
   mapping of existing behavior (mirrors the item-2 statement).

   **Acceptance (testable):**
   - `ScrollPolicy` construction: default `max_attempts == 3`; a policy with
     `max_attempts == 0` yields the `NOT_DISPATCHED` path without dispatching;
     an instrumented policy with `max_attempts >= len(strategy_order)` exercises
     the full chain (per item 2's note that the default cap hides `PAGINATION`).
   - `scroll_with_policy` against a mock dispatcher/observer: arming + change →
     `moved=True`; arming + no change → `NO_SCROLL_EFFECT`; never-armed →
     `NOT_DISPATCHED`; a strategy that returned `ok=False` never contributes to
     `moved=True`.
   - Transition-table test enumerating every `(state, ctx-class)` edge and
     asserting the expected successor (determinism + full fallback-path
     coverage with a stubbed observer and captured snapshot fixtures; no UI).
   - Single-owner invariant: at most one control/strategy dispatched per
     `EXECUTE`; exactly one strategy position advances per `FALLBACK`.
   - Termination: every run finishes in `SUCCESS | NO_SCROLL_EFFECT |
     NOT_DISPATCHED` within `len(strategy_order) * max_attempts` dispatches.

   **Open questions / risks to resolve before/within PR2:**
   - **`scroll_with_policy` signature (blocks PR2).** Item 4 pins `EXECUTE`
     to call the executor, so the call shape must be settled here: the `verify`
     callback signature, how a per-iteration `max_attempts` interacts with a
     per-surface `strategy_order` override, and where the success `reason`
     attribution marker comes from (the PR1 `strategy_used` decision). Avoid a
     second `EXECUTE` entry point that re-binds the bound — item 5 treats that
     as a regression hazard.
   - **`strategy_order` reordering by structure.** Whether the order stays the
     static item-2 default or is reordered by `DetectionResult.structure` is
     open in item 2; PR2 should land the static default and defer reordering to
     PR3's `OWNERSHIP` mapping so `transition` does not re-decide it.
   - **`NavigateContext` shape.** Must carry the running `ScrollResult`
     accumulator, the remaining `strategy_order`, the `ScrollPolicy`, and the
     snapshot pair; pin its fields here and explicitly reserve `DetectionResult`
     as the planned fifth field so PR3's reordered `strategy_order`/
     `DetectionResult` slots in without a signature churn (per item 4, whose
     `ctx` already includes the current `DetectionResult`).
   - **`NOT_DISPATCHED` vs. `NO_SCROLL_EFFECT` discipline.** Guard against
     the loop reporting `NO_SCROLL_EFFECT` when nothing was ever armed — the
     two termination classes must stay distinct (item 2).
3. **PR3 – Structured-content abstraction**: `PageDetector`/`PageController`/
   `PageVerifier` (`ContentNavigator`), generic pagination detection across
   user/alert/asset/operation lists + tests. Builds on PR1+PR2.

   **Scope (explicitly NOT in PR3):** no new primitives (reuses the shipped
   A020/A028/A029 catalog and PR1's `Observer`), no strategy chain / state
   machine rewrites (PR2's `Step`/`transition` and `strategy_order` are
   untouched), no new snapshot/fingerprint code. PR3 lands the three pure roles
   from enforceability item 3 and wires `ContentNavigator` as the facade PR2's
   `scroll_with_policy` was stubbed around, plus swaps PR2's hard-coded
   `FLAT -> WHEEL` `CLASSIFY`/`OWNERSHIP` mapping for the real
   `structure_to_strategy` decision.

   **Deliverables (all under `target/`, with unit tests in the same PR):**
   - `PageDetector` — pure, exactly one real entry point `detect(snapshot) ->
     DetectionResult`; `has_pagination`/`has_next_page`/`is_infinite_scroll`
     are thin helpers over that single pass, NOT separate heuristics (per item
     3). Chooses `PageStructure` by structure (a next-page control exists and
     is enabled, `nextPageButton` is only one HiSec instance of a detected
     control), never by hard-coded automation id. Resolution goes through the
     same resolver the dispatcher uses; detection result carries
     `next_controls: tuple[DetectedControl,...]`, `prev_controls`,
     `structure`, and `confidence` (breaks `PAGINATED` vs `LOAD_MORE` ties).
   - `PageController` — pure planner: `next/advance/reset_to_first(result)
     -> list[ActionReceipt]` targeting the single *enabled* owner control,
     emitting the ordered receipts (`pointer.scroll` A029 wheel step, `pointer.drag`
     A028 scrollbar drag, or semantic `gui.click` A020 next/load-more click);
     `[]` means "nothing to dispatch" → `NOT_DISPATCHED` (item 1). Never calls
     the resolver itself.
   - `PageVerifier` — pure, delegates to PR1's `Observer`: `page_changed
     (before, after, threshold) -> PageChange` (CHANGED/UNCHANGED/UNCERTAIN
     tri-state, item 3). Uses item-6's `content_changed(..., strategy=...,
     tolerance=...)` (the multi-strategy signature; PR1 ships only the
     `TREE_DIGEST` default baseline) and adds nothing beyond the Observer's
     contract; `UNCERTAIN` when a virtualized list recycles rows in place.
   - `ContentNavigator.step(direction, snapshot, policy) -> ScrollResult` —
     the single per-attempt cycle that PR2's machine drives: it reads the
     already-detected, cached `DetectionResult` from `NavigateContext`
     (CLASSIFY/OWNERSHIP run before `step`, in the machine — `step` does NOT
     re-run `PageDetector.detect`; that would violate the machine's one-detect-
     per-cycle contract) and composes Controller plan → dispatch receipt(s) →
     Verifier. Honors item-1's `Verify is not optional` (any receipt that
     armed must be verified) and item-2's `ScrollPolicy.max_attempts` bound is
     applied by the *caller* (PR2's machine), not re-implemented inside `step`.
     `direction` maps to the Controller method: `"next"` → `next`, `"prev"` →
     `advance`, `"first"` → `reset_to_first`.

   **Grounding in current code (verified):** the emitted primitives are the
   existing catalog specs `pointer.scroll` (**A029**), `pointer.drag`
   (**A028**), and `gui.click` (**A020**) in
   `target/action_catalog/actions_v1.py`, all `side_effect="gui_mutation"`,
   returned as `ActionReceipt`s from `target/action_dispatcher/dispatch.py`
   (receipt `ok`/`code`/`action_id` signal dispatch only, never movement).
   `pointer.scroll`'s implementation (`target/automation/base.py`) takes a
   signed `clicks: int`, so a multi-step wheel move stays a single A029
   dispatch — `PageController` must not re-loop it. The verifier's content
   signal already exists as `ObservationSnapshot.tree_digest` /
   `Target.fingerprint` (`target/observations/models.py`); PR3 compares
   snapshots via PR1's `Observer` and needs **no** new fingerprint code. The
   `FLAT -> WHEEL` hard-coded `CLASSIFY`/`OWNERSHIP` stub from PR2 is replaced
   here with the real `structure_to_strategy` mapping. **There is no
   `PageDetector`, `PageController`, `PageVerifier`, or `ContentNavigator` code
   in the tree today** — PR3 is the proposal (mirrors the item-3 statement).

   **Acceptance (testable):**
   - Referential transparency: `detect(same_snapshot)` returns equal
     `DetectionResult`; `PageController`/`PageVerifier` hold no pointer/UI
     state and unit-test with a stubbed observer + captured snapshot fixture
     (no live UI).
   - `PageController.next()` on a `PAGINATED` fixture returns exactly one
     receipt and `[]` (→ `NOT_DISPATCHED`) when the owner `DetectedControl`
     is `enabled=False`; a `LOAD_MORE` fixture returns a `gui.click` A020
     receipt, an `INFINITE` fixture a wheel `pointer.scroll` A029, a `FLAT`
     region a `WHEEL`/`SCROLLBAR_DRAG` mapping — never `PAGINATED`/`TREE_LAZY`
     (guards the item-5 slider/handle misclassification regression).
   - Generic fixtures: one `PAGINATED` table, one `INFINITE` virtual list, one
     `LOAD_MORE` list, and one `FLAT` region classify identically on
     user/alert/asset/operation screens (no classifier keyed to a table id).
   - `ContentNavigator.step` composes Controller → Verifier exactly once per
     call and does NOT re-run `PageDetector.detect` (it reads the cached
     `DetectionResult`): a spy asserts the controller receipt reached the
     dispatcher before the verifier ran, that an armed receipt is always
     verified (`Verify is not optional`), and that `detect` was invoked only by
     the machine's CLASSIFY step, never from inside `step`.
   - Direction mapping: `step("next")` calls `next`, `step("prev")` calls
     `advance`, `step("first")` calls `reset_to_first`, each asserted by a spy
     against a `PAGINATED` fixture (covers the prev/first paths that `next()`
     alone does not).
   - A stubbed `Observer` returning no change ⇒ `step` returns
     `moved=False`; a change ⇒ `moved=True`; a `PageChange.UNCERTAIN` result
     never sets `moved=True` (item 1 / item 6).

   **Open questions / risks to resolve before/within PR3:**
   - **Virtualized-list UNCERTAIN disambiguation.** `PageVerifier` returns
     `UNCERTAIN` when an `INFINITE` list recycles rows in place (equal
     `tree_digest` + OCR text) yet the window may have shifted. Decide which
     observable (scroll-position / first-visible-row signal) the verifier uses
     to disambiguate, and confirm PR2's machine keeps stepping on `UNCERTAIN`
     for `INFINITE` while never reporting `moved=True` on a bare `UNCERTAIN`.
   - **`strategy_order` reordering hook.** PR3 must surface its
     `DetectionResult.structure` to the item-4 `OWNERSHIP` mapping without
     re-deciding order inside `transition`. `NavigateContext` already reserves
     `DetectionResult` as its fifth field (PR2) — this PR3 keys into that slot;
     do not add a parallel field.
   - **Control resolution cost.** `detect()` resolves controls through the
     dispatcher's resolver; per-`step()` on a large table is O(visible
     targets). Decide cache-vs-redetect: if `DetectionResult` is cached for a
     `ContentNavigator` session, a lazy-loaded sibling mid-session must
     invalidate the cache (item 3 lists both options — pick one and test it).
   - **`LOAD_MORE` vs `PAGINATED` overlap.** A surface with both a next-button
     and a load-more affordance can satisfy both structures; `confidence` is the
     tie-breaker. Pin the precedence rule here so the `OWNERSHIP` mapping
     (PR2) and `page_table`/`scroll_region` classifiers upstream agree.

### `scroll_region`

**Purpose.** The generic composite that scrolls *any* region — paged or
plain — by picking the richest strategy the surface supports, dispatching
it, and *verifying* movement. It is the low-risk entry point that keeps
today's `pointer.scroll` (A029) behavior byte-identical for `FLAT` regions
(R0 invariant, enforceability #5) while delegating to `ContentNavigator`
(PageDetector/PageController/PageVerifier) only when structured paging is
detected.

```python
scroll_region(
    title_re: str | None = None,      # region window/control, else current lock
    process_name: str | None = None,  # ownership precondition (A028/A029 require it)
    target_ref: dict | None = None,   # explicit region target (overrides title_re)
    direction: Literal["next", "prev", "first"] = "next",  # horizontal handled per-strategy
    amount: str = "page",             # wheel step unit: "page" | "line" (Strategy.WHEEL only)
    strategy: Strategy | None = None, # constrain the chosen Strategy; None = auto-detect
    verify: bool = True,              # False forces moved=False (see PR1 note); never fabricates
    max_attempts: int | None = None,  # overrides ScrollPolicy.max_attempts (default 3)
) -> ScrollResult
```

Behavior:

1. **Resolve & own.** Region = `target_ref`, else the window matched by
   `title_re`, else the current `window_lock`. `scroll_region` must be
   gate-identical to A028/A029: both `requires=("window_lock",)` and the
   dispatcher's ownership gate key on `expected_process_name` (`process_name`),
   so this composite never dispatches without a confirmed lock. Use
   `verify_window_lock` (from `target/automation/base.py`) before any
   primitive dispatch.

2. **Classify.** Run `PageDetector.detect(snapshot)`; map `PageStructure` →
   `Strategy` via `structure_to_strategy` (see enforceability #3):
   `PAGINATED`/`LOAD_MORE` → `PAGINATION`, `INFINITE` → `WHEEL`,
   `TREE_LAZY` → `FOCUS_THEN_SCROLL`, `FLAT` → `WHEEL`/`SCROLLBAR_DRAG`
   per-surface. `scroll_region` is a thin binding of its args onto one
   `ContentNavigator.step(direction, snapshot, policy)` call — it does **not**
   re-implement the loop. The `max_attempts`-bounded drive loop lives in PR2's
   `scroll_with_policy` / state machine (enforceability #4); `step` performs
   exactly ONE detect → plan → dispatch → verify iteration.

3. **Dispatch.** `PageController.*` emits at most one `ActionReceipt` whose
   `ActionSpec` is one of the primitives below — the controller never moves a
   pointer itself (PR3 mapping to `target/action_dispatcher/dispatch.py`):
   - `Strategy.WHEEL` → `pointer.scroll` (**A029**)
     (`automation.scroll(clicks:int, x=None, y=None)`); `clicks` is `±1` per
     intent `amount` step, sign from `direction` (`next`=down, `prev`=up).
   - `Strategy.SCROLLBAR_DRAG` (payload `FLAT` with a scrollbar) →
     `pointer.drag` (**A028**)
     (`automation.drag(x1,y1,x2,y2,duration=0.25)`) from a safe handle point.
   - `Strategy.PAGINATION` → a semantic button click (existing `gui.click`
     **A020**), never the wheel, when a next/prev or load-more control exists
     (detected structurally per the Test Plan classifier rule, not by
     hard-coded automation id). `direction` maps to `next`/`prev`; `first`
     maps to `PageController.reset_to_first`. For a horizontal request the
     tool must first confirm the chosen strategy can express it (see open
     questions) — it is not silently dropped.

   Dispatched primitives keep TODAY's exact routing (R0). Verification is
   applied only after the dispatch completes; a `ScrollResult` can never cause
   extra input.

4. **Verify.** `PageVerifier.page_changed(before, after, threshold)` compares
   before/after observations via the Observer/diff-engine (enforceability #6),
   plus `ContentNavigator`'s mandatory `VERIFY` step (never skipped for an
   armed `EXECUTE`). Outcomes:
   - success → `ScrollResult(dispatched=True, moved=True, reason=...)`. The
     cycle's attribution marker is **not settled here** — item 2 / PR1 leave
     open whether success `reason` is a mechanism-named `Reason` member, a
     distinct member per strategy, or a separate `strategy_used` field; that
     choice must be final by PR2 and `scroll_region` must use whatever the
     contract settles instead of inventing `reason=<Strategy>`.
   - attempt cap with a dispatched receipt but no verified move →
     `ScrollResult(dispatched=True, moved=False, reason=Reason.NO_SCROLL_EFFECT)`.
   - nothing armed (`PageController` returns `[]`, e.g. disabled owner) →
     `ScrollResult(dispatched=False, moved=False, reason=Reason.NOT_DISPATCHED)`
     — do **not** fall through to `NO_SCROLL_EFFECT`, which by
     `ScrollResult.__post_init__` requires `dispatched=True`.

   `verify=False` deviates from the mandatory-verify stance only as the PR1
   caller-level affordance: it skips the after-snapshot and **forces**
   `moved=False` (it never fabricates movement, P6). Because item 1 makes
   `moved` verification-only, `verify=False` is effectively opt-out of a
   meaningful result — keep it for API symmetry with PR1's `scroll_and_verify`,
   but a caller relying on `moved=True` must leave it `True`.

**Open questions / risks for this item:**

- **`strategy` constraint vs auto-detect conflict.** When the caller forces a
  `Strategy` that contradicts what `PageDetector` finds, which wins? Recommend:
  an explicit `strategy` is a *constraint*, not an override — the tool still
  verifies with `PageVerifier`, and a mismatch (e.g. forcing `WHEEL` on a
  `PAGINATED` surface) returns a distinct failure rather than silently
  dispatching the wrong primitive. This likely needs a **new `Reason` member**
  (e.g. `COMPOSITION_MISMATCH`) — note that this is a *proposed PR1 addition*
  gated on its own test (the item-1 enum is open to new members), not an
  existing member.
- **Horizontal `direction` is not representable in the core contract.**
  `ContentNavigator.step` only accepts `Literal["next","prev","first"]` and
  `PageController` has no left/right mapping (PR3). If `scroll_region` must
  support horizontal wheel, define how it maps onto `Strategy.WHEEL` at this
  layer without leaking into the step contract; otherwise reject horizontal
  with `NO_SCROLL_EFFECT`/`NOT_DISPATCHED` rather than guessing.
- **Scrollbar-drag coordinate sourcing.** `detect_scroll_region` / drag
  handle extraction is platform-specific (UIA on Windows, AX on macOS) and is
  the least mature strategy. If no reliable handle coordinates are available,
  `scroll_region` must fall through to wheel, not error — document that
  fallback ordering in `strategy_order`. (Name this helper explicitly as a
  `PageDetector` capability rather than a parallel "detect" concept.)
- **`amount` → `clicks` calibration.** "page"/"line" sizing differs per
  surface; the composite must not hard-code a global page-step, or R0's
  byte-identical claim breaks for `FLAT` regions. Keep the `clicks` mapping
  per-platform and cap it inside `max_attempts`.

### `page_table`

**Purpose.** The pagination-specific composite: *turn the page of a table by
clipping a semantic next/prev/load-more control*, then prove the page (visible
row range / `tree_digest`) actually changed. Unlike `scroll_region` — which
picks any strategy and defaults to the wheel — `page_table` is **bounded to
`Strategy.PAGINATION`**: it never emits `pointer.scroll` (A029) or
`pointer.drag` (A028), so a misclassified region cannot get raw wheel input.
It is a thin binding over the same `ContentNavigator`
(PageDetector/PageController/PageVerifier) facade (item 3 / PR3) and must stay
gate-identical to the semantic click primitive `gui.click` (**A020**).

```python
page_table(
    table_ref: dict | None = None,     # explicit region/table target; else current lock
    process_name: str | None = None,   # ownership precondition (A020 requires window_lock)
    direction: Literal["next", "prev", "first"] = "next",
    verify: bool = True,               # False forces moved=False (see PR1 note); never fabricates
    max_attempts: int | None = None,   # overrides ScrollPolicy.max_attempts (default 3);
                                       # forwarded into the single step/policy call below
) -> ScrollResult
```

Behavior:

1. **Resolve & own.** Region = `table_ref`, else the current `window_lock`.
   `gui.click` (A020) `requires=("connected_window", "window_lock",
   "target_ref")` (catalog `target/action_catalog/actions_v1.py`), so
   `page_table` must confirm ownership with `verify_window_lock`
   (`target/automation/base.py`) and resolve the target before any dispatch —
   a composite pagination click is never sent unlocked.

2. **Classify & gate — one detect, one decision.** Run
   `PageDetector.detect(snapshot)` **once**. The resulting `DetectionResult`
   feeds both the page-gate below and `ContentNavigator.step` — `page_table`
   passes its already-computed `DetectionResult` into the single step call
   (see step 3), so the invariant of item 4 ("exactly one
   `PageDetector.detect(snapshot)` pass") holds and `step` must **not**
   re-detect. `step` is one detect -> plan -> dispatch -> verify iteration and
   `page_table` does not re-implement its loop (the `max_attempts`-bounded
   drive loop stays in PR2's `scroll_with_policy`, which consumes `page_table`'s
   `max_attempts` override by folding it into the `ScrollPolicy` it drives).
   **Page-gate:** if `DetectionResult.structure` is not `PAGINATED` or
   `LOAD_MORE` (i.e. `FLAT` / `INFINITE` / `TREE_LAZY`), do not fall through to
   the wheel — return
   `ScrollResult(dispatched=False, moved=False, reason=Reason.NOT_DISPATCHED)`.
   `INFINITE` / `FLAT` / `TREE_LAZY` surfaces are `scroll_region`'s job; routing
   them here would violate the classifier contract (Test Plan:
   `scroll_region`/`page_table` classifier agrees with the PR3 detectors).

3. **Dispatch — semantic click only.** `PageController.next` /
   `PageController.advance` / `PageController.reset_to_first` return the
   ordered receipts, which for the paginated case resolve to a **single
   semantic `gui.click` (A020)** on the single *enabled* next/prev/load-more
   control (detected structurally via `DetectionResult.next_controls` /
   `prev_controls`, never by hard-coded automation id — `nextPageButton` is
   only one HiSec instance). `direction` maps: `next` -> `PageController.next()`
   (targets the single enabled control in `DetectionResult.next_controls`),
   `prev` -> `PageController.advance()` on the single enabled control in
   `DetectionResult.prev_controls`, `first` -> `reset_to_first()`. (The exact
   `PageController` verb for the backward direction — `advance()` vs a dedicated
   `previous()` — is a PR3 naming decision to pin down; `page_table` only
   requires that the backward path target the *enabled* `prev_controls` owner the
   same way `next()` targets the `next_controls` owner.)
   `LOAD_MORE` (structure -> `Strategy.PAGINATION`, item 3) is **one** click /
   one dispatch, not a loop. `first` on a table with no predecessor emits
   `[]` -> `ScrollResult(dispatched=False, moved=False,
   reason=Reason.NOT_DISPATCHED)`. A disabled next-button (last page) also
   yields `[]` -> `NOT_DISPATCHED`; that is the caller's signal to stop.

4. **Verify.** `ContentNavigator.step` runs the mandatory `VERIFY` step (never
   skipped for an armed `EXECUTE`); `PageVerifier.page_changed(before, after,
   threshold)` compares the before/after `ObservationSnapshot`s via the
   Observer/diff-engine (item 6), where `before`/`after` carry `tree_digest`
   (content-addressed `sha256:<hex>` over `Target.fingerprint`s,
   `target/observations/models.py`) plus the OCR/AX text slice. Outcomes:
   - next-click armed + `PageChange.CHANGED` -> `ScrollResult(dispatched=True,
     moved=True, reason=...)` — the success `reason` marker follows the same
     unresolved attribution as item 2/PR1 (per-strategy `Reason` member vs a
     `strategy_used` field); `page_table` uses whatever the settled contract is,
     it does not invent `reason=<Strategy>`.
   - click armed but `PageChange.UNCHANGED` (or the page literally did not move)
     -> `ScrollResult(dispatched=True, moved=False,
     reason=Reason.NO_SCROLL_EFFECT)`.
   - `PageChange.UNCERTAIN` (virtualized row reuse, `tree_digest` equal yet the
     window may have shifted) must **not** be reported as `moved=True`; follow
     item 3's rule — keep stepping only with caller consent, never count it as
     success.

   `verify=False` deviates from the mandatory-verify stance only as the PR1
   caller-level affordance: it skips the after-snapshot and **forces**
   `moved=False` (it never fabricates movement, P6). A caller relying on
   `moved=True` must leave it `True`.

**Open questions / risks for this item:**

- **"All records / drain the whole table" is a caller loop, not one call.**
  `page_table(next)` returns one `ScrollResult` for one page turn. To collect
  every page the caller must loop over `ScrollResult.success`, re-running
  `page_table` (bounded overall by `max_attempts` per call), and stop on
  `NOT_DISPATCHED` (last page). Do not make a single `page_table` call loop
  internally past one dispatch, or it duplicates PR2's `scroll_with_policy`.
- **Page-gate failure mode must be distinct and testable.** Deciding what
  `page_table` does on a `FLAT` / `INFINITE` region is a real behavioral
  contract: recommend fail-fast `NOT_DISPATCHED` (above) so a misclassification
  never silently scrolls. Add a test that a `FLAT` fixture returns
  `dispatched=False` and confirms no `pointer.scroll`/`pointer.drag` was
  dispatched (regression guard: Test Plan "slider/handle -> drag, never table
  pagination").
- **`reason` attribution for a `LOAD_MORE` vs a numbered `PAGINATED` advance.**
  Both map to `Strategy.PAGINATION` but are semantically different page turns.
  If `moved=True` reason must disambiguate them, that needs either two `Reason`
  members or the `strategy_used` field — decide under item 2 / PR1 before PR3.
- **Verify churn on paged tables.** A page turn repaints rows in place; the
  `tree_digest` may change for reasons unrelated to the intended page control
  (async load, spinner). Reuse the item-6 threshold / churn-attribution so a
  transient re-render is never counted as `moved` (Test Plan: false-positive
  assert).

### `scroll_until_visible`

A **goal-driven loop**: keep advancing the view until a target control/text is
observed, or the bounded budget is exhausted. Unlike `scroll_region` (one
bounded strategy pass) and `page_table` (one page turn), this tool owns an
iteration loop across multiple steps. It must compose the existing PR1/PR2
primitives rather than re-implement scrolling, and it must *never* report
success on a target it did not actually observe.

```python
def scroll_until_visible(
    target_text_re: str,
    region_ref: dict | None = None,
    direction: str = "down",          # "down" | "up" — argv maps to clicks sign
    max_steps: int = 8,               # bounded loop budget (>= 1)
    verify: bool = True,              # PR1 caller-level affordance, see below
) -> ScrollResult:                    # NOT a bare bool — see item 1
```

Contract details, review-ready:

- **Iteration = one `scroll_region`-style bounded step + one mandatory target
  probe.** Each step runs the single bounded strategy pass from PR2
  (`scroll_with_policy` with the surface policy: `pointer.scroll` **A029** for
  `WHEEL`, `pointer.drag` **A028** for `SCROLLBAR_DRAG`, or the semantic
  `PAGINATION` control when the `PageDetector` classifies the region as
  paginated — item 3/PR3). After each armed dispatch the step re-snapshots and
  searches for `target_text_re` on the `Target` identity/`text` fields
  (canonical job: locate `Target` whose `text`/`title`/`automation_id` matches,
  in `ObservationSnapshot.targets`; see `target/observations/models.py`).
- **Loop budget semantics.** At most `max_steps` probe iterations. A step that
  dispatches nothing (`NOT_DISPATCHED`) is *not* a retry — it terminates the
  loop immediately with `moved=False` because no mechanism can advance the
  view. A step that dispatches but verifies no change (`NO_SCROLL_EFFECT`) also
  terminates immediately: per item 2 a strategy is never re-run when it failed
  to move, so continuing would be guaranteed futile. Only a step that
  **moved** (Observer-verified `moved=True`) and still did not surface the
  target counts against the remaining budget.
- **Termination outcomes** (all map onto the `ScrollResult` contract of item 1
  — this tool returns a `ScrollResult`, never a bare bool):
  - **target found** → `ScrollResult(dispatched=True, moved=True, reason=...)`.
    `moved` here means the final step moved the view *and* the probe matched;
    the `reason` attribution follows the same unresolved per-strategy
    `Reason`-member-vs-`strategy_used` contract as items 2/PR1/`page_table` —
    this tool does not invent a new `Reason` member.
  - **budget exhausted after ≥1 moved-but-unmatched step** →
    `dispatched=True, moved=False, reason=Reason.NO_SCROLL_EFFECT` (the caller
    sees "scrolled, still no target", not a fabricated `not_found_after_scroll`
    string — `not_found_after_scroll` is rendered from this `ScrollResult`, not
    stored as a distinct outcome).
  - **first step could not dispatch, or target already visible at start** →
    `dispatched=False, moved=False, reason=Reason.NOT_DISPATCHED`. The tool
    probes **before** the first dispatch; if `target_text_re` is already
    present it performs zero dispatches and returns `NOT_DISPATCHED` with
    `moved=False` (lying "moved" for an already-visible target would violate
    the item-1 invariant `moved requires dispatched`). Whether the caller needs
    a *distinct* signal to distinguish "already visible" from "nothing to
    dispatch" is an open enum question — resolved under item 1 (see Open
    questions below).
- **`direction` mapping.** `"down"` → negative A029 `clicks` and, for a
  paginated region, `PageController.next`; `"up"` → positive `clicks` and
  `PageController.advance`/`prev`. Note the domain shift: this tool's
  `direction` uses the string pair `"down"|"up"`, whereas `ContentNavigator.step`
  / `PageController` use `Literal["next","prev","first"]` — the tool must
  **translate** `"down"→"next"` and `"up"→"prev"` before calling `step`; it must
  not pass `"down"` straight into the `Literal` API. The click-sign convention
  must match `target/automation/base.py`'s `scroll(clicks: int, ...)` semantics
  (down = negative) — a wrong sign is a functional bug. Add two unit tests: one
  tying `direction="down"` to `clicks < 0`, one asserting the
  `"down"→"next"` / `"up"→"prev"` translation passes the correct verb to `step`.
- **`verify=False`** keeps the PR1 caller-level affordance but is **decoupled
  from the loop's internal continuation check**: the Observer always decides
  whether a step actually moved and whether the loop keeps iterating (budget
  bullet above). `verify=False` only blanks the **returned** `ScrollResult.moved`
  (sets it `False` on termination) so that a caller which skipped verification
  cannot claim verified movement (P6) — it *never* fabricates movement.
  Concretely, under `verify=True` the returned final result carries
  `moved=True` only when the final step both moved and matched; under
  `verify=False`, even a successful multi-step find returns `moved=False`
  (with `dispatched=True`) so the caller knows movement was not confirmed. A
  caller that needs `moved=True` must leave `verify=True`. The **presence
  probe for `target_text_re` is never disabled** — it is the whole point of the
  tool; `verify` only controls whether the *returned* `moved` reflects the
  Observer, not the search for the target nor the loop's internal progress
  check.
- **Probe on tree text, not screenshot digest alone.** The target predicate is
  a text/identity match against `ObservationSnapshot.targets` (the canonical
  `tree_digest` / `Target.fingerprint` machinery in
  `target/observations/models.py` / `fingerprint.py`). The screenshot digest
  (`content_changed`) is used for the movement check, but the target-find is
  the text/identity match — the two must not be conflated (an OCR digest could
  flip for a transient spinner while the target text never appears; see
  `page_table` item's verify-churn note).

**Open questions / risks for this item:**

- **Present-at-start needs a distinct signal?** The termination bullet above
  already fixes the contract (`NOT_DISPATCHED`, `moved=False`, zero dispatches).
  The remaining decision — whether the caller needs a *distinct* `Reason` to
  tell "already visible" from "nothing dispatchable" — is owned by item 1 and
  must be resolved **before PR1 locks the `Reason` enum** (a new member such as
  `TARGET_VISIBLE` is acceptable only if gated by a test that returns
  `dispatched=False, moved=False, reason=TARGET_VISIBLE`; otherwise callers
  treat `NOT_DISPATCHED` as "target present *or* nothing to do" and probe first
  themselves). This is a hard prerequisite for PR1, not a deferred nicety:
  `scroll_until_visible`'s caller-facing contract changes depending on the
  choice, so the enum must not ship before it is settled.
- **Region-scope of the probe.** `region_ref` scopes both *where* the loop
  scrolls and *where* the target is searched. Without top-level match
  anchoring, `target_text_re` could match a row on a stale page; with it, a
  target that genuinely lives outside `region_ref` (a footer) would never be
  found. Recommend: probe restricted to `region_ref` when given, else the
  active window; flag if a cross-region target is ever required.
- **Budget interplay with `ScrollPolicy`.** `max_steps` (loop iterations) and
  `ScrollPolicy.max_attempts` (strategies per step) are **two nested bounds**,
  not one. `max_steps=8` × one-strategy-per-successful-step must not be read as
  8 failed dead-end dispatches — a `NO_SCROLL_EFFECT` or `NOT_DISPATCHED` step
  terminates immediately (see above). Confirm the product contract: is
  `max_steps` a wall of *moved* steps only, or also counted when a step is
  dispatched-but-unmoved? The spec above terminates on unmoved steps, which
  makes `max_steps` a true **upper bound** on how far one can page (in moved
  steps), never a lower bound.
- **Drag-into-view is an open alternative.** `scroll_until_visible` currently
  drives wheel/pagination. A target below the fold of a drag-scrolled virtual
  list may need `drag_target`-style nudges; decide whether this tool composes
  `SCROLLBAR_DRAG` (it does, via the strategy policy) or whether it stays
  wheel/pagination-only.

Test plan for this item (fold into the section's Test Plan rows):

- A fixture with the target on page N: assert `scroll_until_visible` returns
  `moved=True` and emits exactly N armed step dispatches (probe-before-dispatch
  means no premature stop).
- A fixture where the target never appears: assert termination after at most
  `max_steps` moved steps with `moved=False, reason=NO_SCROLL_EFFECT` and no
  strategy dispatched twice.
- `direction` sign unit test (`"down"` → `clicks < 0`).
- `verify=False` never yields `moved=True` and still performs the target probe;
  a multi-step find under `verify=False` still iterates to the target but
  returns `moved=False` (assert the loop performed >1 step and the final
  `ScrollResult.dispatched=True, moved=False`).
- Present-at-start fixture returns `NOT_DISPATCHED` and performs zero dispatches.

### `drag_target`

**Purpose.** The handle-based composite for controls that are *dragged*, not
clicked or wheel-scrolled: sliders, list-rows on reorder, splitter handles,
resize grips, and virtual-list nudges. It is the only proposed tool that
dispatches `pointer.drag` (**A028**) directly, and it must stay **gate-identical
to A028's ownership precondition** (`requires=("window_lock",)`,
`side_effect="gui_mutation"`, `risk="high"`, `default_screenshot="after"`,
`transition_policy="possible"` — catalog `target/action_catalog/actions_v1.py`).
Unlike `page_table` / `scroll_until_visible`, which route through the
`ContentNavigator` (`PageDetector`/`PageController`/`PageVerifier`) facade, this
tool is a **thin, un-strategized binding over the raw `drag` primitive**; it must
*not* silently fall back to `pointer.scroll` (A029) or a semantic click when a
drag yields no effect — `drag_target` is opt-in for exactly the controls A028
was designed for (sliders, list reordering, selection rectangles, drag-and-drop).

```python
drag_target(
    target_ref: dict,          # the draggable control (Thumb/Rail/Handle/Row)
    dx: int | None = None,     # delta in px from the grab point to release point
    dy: int | None = None,     # at least one of dx/dy is required
    x2: int | None = None,     # absolute release point (mutually exclusive w/ dx/dy)
    y2: int | None = None,
    grab: Literal["center", "handle"] = "handle",  # see risk on grab point below
    verify: bool = True,       # False forces moved=False (P6: never fabricate)
    expected_process_name: str | None = None,  # see open risk: current drag() has no such param
) -> ScrollResult
```

Behavior:

1. **Resolve & own.** Locate `target_ref` through the same resolver that feeds
   `TaskResult`/`ObservationSnapshot` (`target/observations/resolver.py`); the
   control's rect gives the grab point. Before any dispatch, confirm ownership
   with `verify_window_lock(activate=True)` (`automation/base.py`) — A028
   `requires=("window_lock",)`, so an unlocked `drag_target` must fail closed
   with `ScrollResult(dispatched=False, moved=False, reason=Reason.NOT_DISPATCHED)`
   and never race a real pointer grab onto an unresolved/lost lock.

2. **Compute endpoints.** If `dx`/`dy` given, release point =
   `(grab_x + dx, grab_y + dy)`; if `x2`/`y2` given, use those directly. Exactly
   one of the delta pair or the absolute pair must be supplied (validate at the
   signature boundary: `(dx or dy)` XOR `(x2 or y2)`, else `NOT_DISPATCHED` with
   an argument error). `grab="handle"` snaps the grab point to the resolved
   handle/thumb sub-rect when the control exposes one (via
   `DetectionResult`-style structural discovery, never a hard-coded automation
   id); `grab="center"` uses the control center (safe-center path).

3. **Dispatch — one `pointer.drag` (A028), never a loop.** Call
   `drag(x1, y1, x2, y2, duration=0.25)` once (signature per
   `automation/base.py`). `drag_target` performs **one armed dispatch per call**;
   there is no internal retry loop (a caller that needs repeated nudges loops
   over this tool itself, each call bounded by the same one-dispatch rule). The
   `expected_process_name` is *intended* to be threaded to the backend `drag`
   for a post-drag process-mismatch check — but note the current
   `AutomationBackend.drag` primitive has **no** `expected_process_name`
   parameter (only the click-family methods do), so forwarding it requires an
   explicit backend-signature extension (see Open questions); until then the
   ownership check is carried by the `verify_window_lock` step alone.

4. **Verify.** Take an `ObservationSnapshot` before the drag and after, and diff
   via the Observer/diff-engine (item 6). Because A028 is
   `side_effect="gui_mutation"` with `transition_policy="possible"`, verify must
   decide between:
   - `drag` **dispatched** and the diff shows the control moved / state changed
     (e.g. slider value, row order, `tree_digest` changed) →
     `ScrollResult(dispatched=True, moved=True, reason=...)` — the `reason`
     attribution follows the same unresolved PR1 contract as the other tools
     (per-strategy `Reason` member vs `strategy_used`); this tool does not
     invent `reason=<Strategy>`.
   - `drag` dispatched but no verifiable change →
     `ScrollResult(dispatched=True, moved=False, reason=Reason.NO_SCROLL_EFFECT)`.
   - `PageChange.UNCERTAIN` (transient animation churn, same `tree_digest` yet
     the handle may have shifted) must **not** be reported `moved=True` — reuse
     the item-6 threshold / churn-attribution so a repaint is never counted as a
     drag success.
   - `verify=False` (PR1 caller-level affordance) skips the after-snapshot and
     **forces** `moved=False`; it never fabricates movement (P6). A caller
     relying on `moved=True` must leave `verify=True`. **`reason` under
     `verify=False`:** with `dispatched=True, moved=False`, item-1's invariant
     ("`ok` true and `moved` false ⇒ `reason=NO_SCROLL_EFFECT`") pins the reason
     to `NO_SCROLL_EFFECT` — but that mislabels a **skipped** verification as a
     *verified* no-effect. The implementation should either accept
     `NO_SCROLL_EFFECT` deliberately with the ambiguity documented, or add a
     distinct `Reason.UNVERIFIED` and reconcile item 1's invariant table (see
     Open questions).

**Open questions / risks for this item:**

- **Grab-point correctness is the hard part.** A028 is `risk="high"` and
  `gui_mutation`; grabbing a slider by center when it has a small thumb, or
  grabbing the wrong handle in a splitter, produces a *successful* drag that
  moves the wrong thing — a `moved=True` false positive the diff-engine may not
  catch if the whole control moved. Define the `grab` policy (center vs
  thumb/handle) per control type and add a fixture that a `Thumb` control
  resolves its grab point to the thumb rect, not the rail center.
- **Dragging is directional and absolute; `drag_target` is not a coarse
  strategy.** It deliberately does **not** compose `SCROLLBAR_DRAG` (unlike the
  open question at `scroll_until_visible`), because a scrollbar thumb drag and a
  content drag have different end-condition semantics. Decide explicitly whether
  `drag_target` should ever be offered as a sub-strategy of `scroll_with_policy`
  (PR2), or whether it stays caller-only for precise controls. Recommendation:
  caller-only for v1; fold scrollbar dragging into the `SCROLLBAR_DRAG` strategy
  separately, keyed by `DetectionResult` structure, not by `drag_target`.
- **`drag` result alone is not proof of movement.** Parity with the acceptance
  rule "the agent does not treat `scroll.ok=true` as success without an
  observation change": `drag` returning `ok=true` must not be treated as
  `moved=True` without the diff. Add a test asserting a dry-run/no-op drag
  (backends must keep dry-run default for real pointer actions unless
  `EDR_WD_ALLOW_REAL_CLICKS=1`) returns `ok=true` yet `drag_target` reports
  `dispatched=False` or `moved=False` appropriately.
- **`expected_process_name` is not in the current `drag()` signature.** The
  existing `AutomationBackend.drag(x1, y1, x2, y2, duration=0.25)` primitive
  (`automation/base.py`) has no `expected_process_name` parameter, unlike the
  click-family methods. Decide whether to *extend* the backend `drag` signature
  (a real, separately-reviewed code change) or to drop the parameter from
  `drag_target` and rely solely on `verify_window_lock` for process ownership.
  Until that decision, the parameter is declared-but-pending and must not be
  assumed to exist in the primitive.
- **`verify=False` reason ambiguity (item-1 invariant collision).** As detailed
  in step 4, when `verify=False` the doc's own invariant forces
  `reason=NO_SCROLL_EFFECT`, which conflates *unverified* with *no-scroll-effect*.
  Resolve before PR1: either accept `NO_SCROLL_EFFECT` with the caveat
  documented, or add `Reason.UNVERIFIED` and update item-1's invariant table so
  an unverified drag-distinct case is testable.
- **Endpoint validation gap.** An out-of-bounds `x2`/`y2` (off the client
  rect) is a recoverable caller error, not a retry; decide whether the tool
  clamps to the client rect or returns `NOT_DISPATCHED` with an argument error.
  Recommend the latter so an off-screen drop never fires silently.

## Backend Requirements

### Windows

- Keep `scroll` and `drag` primitive compatibility.
- Add optional verification helpers that can extract table/list visible rows
  from UIA where available.
- Treat RDP active-window detection failures as recoverable:
  - try `lock_window` first.
  - if lock verification fails due active-window API limitations, use
    focus-then-scroll only when process/window/point are all known.
- Prefer semantic pagination button clicks over wheel scrolling when
  `nextPageButton` or equivalent controls exist.

### macOS

- Keep dry-run default for real pointer actions unless
  `EDR_WD_ALLOW_REAL_CLICKS=1`.
- Use AX scroll actions when available before falling back to `pyautogui`.
- Prefer AX-discovered scroll areas and table row changes as verification.
- Preserve the same action catalog IDs as Windows.

## Test Plan

Unit/contract tests:

- action catalog contains `pointer.drag` and `pointer.scroll`.
- `scroll_region`/`page_table` classifier chooses pagination when a next-page
  control exists (detected structurally, not by hard-coded automation id).
- `focus_then_scroll` requires `expected_process_name`.
- `scroll` result alone is not accepted as proof of UI movement.
- RDP lock failure maps to a bounded fallback, not unlimited raw scroll.
- Composite actions return `ScrollResult` with `dispatched`/`moved`/`reason`.
- `ScrollPolicy.max_attempts` cap is respected: when no strategy verifies a
  change, the loop returns `moved=False, reason="no_scroll_effect"` and does not
  re-run a failed strategy; different surfaces can override `max_attempts`.
- `PageDetector` / `PageController` / `PageVerifier` return correct results for
  a generic table (user/alert/asset/operation list fixture).
- Observer `content_changed` handles a **verify false positive**: a transient
  animation that changes the screenshot digest (or spinner) but not the actual
  content must NOT be counted as `moved`. Assert `diff(before, after)` is
  attributed to churn, not to a content change.
- Regression: a slider/handle control is classified as drag, never as table
  pagination; a plain scrollable region (no pagination controls) keeps today's
  scroll behavior.

Target-level smoke tests:

- `scroll(clicks=-1, x, y)` returns `ok=true` in dry-run or real mode.
- `drag(...)` returns `ok=true` in dry-run or real mode.
- `status.action_space.drag` and `status.action_space.scroll` are true.

Live E2E tests:

- Windows HiSec `日志中心 -> 操作日志`:
  - detect table and pagination.
  - try one bounded wheel scroll.
  - verify first visible row changes or declare no wheel effect.
  - click next page.
  - verify page/first row changed.
- macOS generic scroll area:
  - focus region.
  - bounded scroll.
  - verify visible content or screenshot digest changed.

## Acceptance Criteria

- The agent does not treat `scroll.ok=true` as user-visible success without an
  observation change.
- Composite scroll actions expose `dispatched`/`moved`/`reason` so callers
  cannot conflate dispatch with movement.
- Paginated tables use pagination controls for "more records" intents,
  detected structurally across user/alert/asset/operation lists — not
  hard-coded to one page name or automation id.
- Raw wheel scrolling is bounded and followed by verification.
- Approach paths are capped by a per-surface `ScrollPolicy`
  (agent-side default `MAX_SCROLL_ATTEMPTS = 3`); exhaustion returns
  `no_scroll_effect` and never loops infinitely.
- RDP/window-lock failures have a safe, documented fallback that is unchanged
  for non-paginated regions.
- Drag/scroll remain backward compatible MCP tools.
- Future planner guidance exposes both primitive and semantic scroll actions,
  preferring semantic/paginated actions when available.

## Open Questions

- Should `scroll_region` be a new MCP tool, an agent-side composite action, or
  both?
- Should `page_table` be HiSec-specific first or generic across backends?
- Which visible-row extraction should be authoritative when UIA table text is
  sparse: UIA tree, screenshot OCR, or screenshot digest?
- Should `scroll` itself optionally perform verification, or should verification
  live only in composite actions?
