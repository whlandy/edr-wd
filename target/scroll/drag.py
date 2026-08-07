"""
drag.py — `drag_target` composite: one pointer.drag (A028) from a resolved
control to a release point, with window/ownership and movement verification.
(docs/todo/scroll-and-paged-table-actions.md, "T4: drag_target".)

T4 is the drag-specific sibling of `page_table` (T2) and `scroll_until_visible`
(T3). Where those scroll one page / one direction, this composite performs a
single drag for sliders, handles, splitter bars, resize grips, list
reordering, and precise virtual-list nudges.

Design decisions (each pinned by a unit test):

  * **Resolve `target_ref` to a control rectangle.** `resolve_drag_target`
    is the pure resolution gate (mirrors the ownership/selector semantics of
    `observations.resolver.resolve_target`, kept small and back-end-free so it
    unit-tests on any host). It matches the target by `target_id`, then
    `automation_id`, then `text`, then `control_type`, honouring
    `expected_process_name` as an ownership filter. Returns the control Target
    or raises `TargetResolutionError`.

  * **Verify window ownership before dispatch.** Ownership is enforced at
    resolution time (a matching control only counts if its `process_name`
    matches `expected_process_name`); the server/MCP layer additionally runs
    `verify_window_lock` before calling the composite when a lock exists.

  * **Compute exactly one drag from a safe grab point to a release point.**
    The grab point is derived from the resolved control's `rect`:
      - `grab="center"`  → the rect centre (safe default for most controls).
      - `grab="handle"`  → the leading-edge quarter point along the dominant
        drag axis (thumb/handle semantics — used for sliders, splitter bars
        and resize grips where grabbing the rail centre would be wrong).
    The release point is either an absolute `(x2, y2)` or the grab point plus
    a `(dx, dy)` offset — exactly one form must be supplied, and a
    zero-vector drag (no movement) is rejected.

  * **Dispatch only `pointer.drag` (A028).** The controller emits exactly one
    `PlannedAction(action_id="pointer.drag", action_code="A028", ...)`. A
    drag never falls back to a wheel (`pointer.scroll` A029) or a semantic
    click (`gui.click` A020).

  * **Verify actual state/position/content movement via the Observer.** After
    the drag is dispatched, the after-snapshot is diffed against the
    before-snapshot by the composite's verifier. `moved=True` is only ever
    reported when a real observation change was detected.

  * **verify=False never returns moved=True.** Without movement verification
    the composite cannot substantiate `moved`, so the outcome is the honest
    dispatched-but-unverified `NO_SCROLL_EFFECT`.

  * **A dry-run/no-op drag cannot return `moved=True`.** A dispatch that is
    never verified as moving content yields `NO_SCROLL_EFFECT` (never
    success), matching the PR1 "dispatch is not movement" invariant.

Pure module — no MCP transport. It composes the injected `executor` /
`observer` / `verifier` (as do the sibling modules), so it is unit-testable
without a live backend. The `backend.run_drag_target` facade wires the real
chain and the server MCP tool calls it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional, Tuple

from observations.models import ObservationSnapshot, Target

from .actions import PlannedAction
from .executor import ScrollExecutor
from .observer import Observer
from .results import Reason, ScrollResult, Strategy
from .verifier import PageChange, PageVerifier

# pointer.drag (A028) — the ONLY primitive this composite may dispatch.
DRAG_ACTION_ID = "pointer.drag"
DRAG_ACTION_CODE = "A028"

# Default drag duration (seconds) passed to the backend primitive.
DEFAULT_DURATION = 0.25


class DragTargetResolutionError(RuntimeError):
    """Raised when `target_ref` cannot be resolved to a drag-able control.

    Not frozen (a plain exception) so the traceback machinery (pytest
    included) can attach state without hitting dataclass immutability.
    Carries a stable machine-readable `code` alongside the message.
    """

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code
        self.message = message or code


def _norm(s: Optional[str]) -> str:
    return (s or "").strip()


def resolve_drag_target(
    snapshot: ObservationSnapshot,
    target_ref: Mapping[str, object],
    *,
    expected_process_name: Optional[str] = None,
) -> Target:
    """Resolve `target_ref` to a *control* Target in `snapshot` (pure).

    Matching order (first unambiguous hit wins):
      * `target_id` — exact observation-local id (T####).
      * `automation_id` — exact native automation/accessibility id.
      * `text` — exact normalized text.
      * `control_type` — exact role/type.

    `expected_process_name`, when supplied, acts as an ownership gate: only
    controls whose `process_name` equals it (case-insensitive, `.exe`
    tolerant) are candidates — a mismatch raises `DragTargetResolutionError`
    with `code="ownership_mismatch"`. When the selector matches nothing
    (or nothing under the ownership gate) the error carries
    `code="target_not_found"`; when it matches more than one control it
    carries `code="target_ambiguous"`. Window/dialog targets (non-control)
    are never drag sources.
    """
    if not isinstance(target_ref, Mapping) or not target_ref:
        raise DragTargetResolutionError(
            code="invalid_target_ref", message="target_ref must be a non-empty mapping"
        )

    candidates = [t for t in snapshot.targets if t.kind == "control"]
    if expected_process_name:
        norm = expected_process_name.strip().lower().removesuffix(".exe")
        filtered = [t for t in candidates if (t.process_name or "").strip().lower() == norm]
        if not filtered:
            raise DragTargetResolutionError(
                code="ownership_mismatch",
                message=(
                    f"no control owned by process {expected_process_name!r} in snapshot"
                ),
            )
        candidates = filtered

    # 1) exact target_id
    tid = _norm(str(target_ref.get("target_id") or ""))
    if tid:
        matched = [t for t in candidates if t.target_id == tid]
        _raise_if_ambiguous(matched, "target_id")
        if len(matched) == 1:
            return matched[0]

    # 2) exact automation_id
    aid = _norm(str(target_ref.get("automation_id") or ""))
    if aid:
        matched = [t for t in candidates if _norm(t.automation_id) == aid]
        _raise_if_ambiguous(matched, "automation_id")
        if len(matched) == 1:
            return matched[0]

    # 3) exact text
    text = _norm(str(target_ref.get("text") or ""))
    if text:
        matched = [t for t in candidates if _norm(t.text) == text]
        _raise_if_ambiguous(matched, "text")
        if len(matched) == 1:
            return matched[0]

    # 4) exact control_type
    ctype = _norm(str(target_ref.get("control_type") or ""))
    if ctype:
        matched = [t for t in candidates if _norm(t.control_type).lower() == ctype.lower()]
        _raise_if_ambiguous(matched, "control_type")
        if len(matched) == 1:
            return matched[0]

    extra = " under the ownership gate" if expected_process_name else ""
    raise DragTargetResolutionError(
        code="target_not_found",
        message=f"target_ref did not resolve to a control{extra}",
    )


def _raise_if_ambiguous(matched: list[Target], via: str) -> None:
    if len(matched) > 1:
        raise DragTargetResolutionError(
            code="target_ambiguous",
            message=f"target_ref matched {len(matched)} controls via {via!r}; "
                    "refusing to pick one",
        )


def center(rect: Tuple[int, int, int, int]) -> Tuple[int, int]:
    """Rect centre: ((left+right)//2, (top+bottom)//2)."""
    left, top, right, bottom = rect
    return (left + right) // 2, (top + bottom) // 2


def grab_point(
    rect: Tuple[int, int, int, int],
    grab: str,
    dx: int,
    dy: int,
) -> Tuple[int, int]:
    """Compute the safe grab point from a control rect (pure).

    * ``grab="center"`` -> the rect centre (safe default).
    * ``grab="handle"`` -> the leading-edge quarter point along the dominant
      drag axis, so the grab lands on the thumb/handle (slider, splitter,
      resize grip) rather than the rail centre:
        - dominant horizontal drag (|dx| >= |dy| and dx != 0) → point at
          left + width//4 when dragging right, right - width//4 when dragging
          left, on the vertical centre-line;
        - dominant vertical drag otherwise → point at top + height//4 when
          dragging down, bottom - height//4 when dragging up, on the
          horizontal centre-line;
        - a pure zero-vector drag is rejected by the caller before we get
          here (see `_compute_endpoints`), so a purely axial dominance case
          always holds.

    Raises ``ValueError`` on an unknown ``grab`` mode.
    """
    if grab not in ("center", "handle"):
        raise ValueError(f"unknown grab mode {grab!r}; expected center|handle")
    if grab == "center":
        return center(rect)

    left, top, right, bottom = rect
    width = max(right - left, 1)
    height = max(bottom - top, 1)
    cx, cy = center(rect)

    if abs(dx) >= abs(dy):
        # horizontal drag: grab the leading-edge quarter of the thumb.
        if dx > 0:
            return (left + width // 4, cy)
        if dx < 0:
            return (right - width // 4, cy)
        return (cx, cy)
    # vertical drag.
    if dy > 0:
        return (cx, top + height // 4)
    if dy < 0:
        return (cx, bottom - height // 4)
    return (cx, cy)


def _compute_endpoints(
    *,
    rect: Tuple[int, int, int, int],
    grab: str,
    dx: Optional[int],
    dy: Optional[int],
    x2: Optional[int],
    y2: Optional[int],
    start: Optional[Tuple[int, int]],
) -> Tuple[Tuple[int, int], Tuple[int, int]]:
    """Validate endpoint arguments and compute (start, end) screen points.

    Exactly one endpoint form may be supplied:
      * offset form — ``dx``/``dy`` relative to the grab point; a zero-vector
        ``(0, 0)`` is a no-op drag and is rejected.
      * absolute form — ``x2``/``y2`` (screen coords).

    Supplying both, or neither, or an offset with a zero vector, raises
    ``ValueError`` before anything is dispatched.
    """
    offset = (dx is not None) or (dy is not None)
    absolute = (x2 is not None) or (y2 is not None)
    if offset and absolute:
        raise ValueError(
            "supply either (dx, dy) offset OR absolute (x2, y2), not both"
        )

    start = start if start is not None else grab_point(rect, grab, dx or 0, dy or 0)

    if absolute:
        if x2 is None or y2 is None:
            raise ValueError("absolute endpoint requires both x2 and y2")
        return start, (int(x2), int(y2))

    if not offset:
        raise ValueError("supply either (dx, dy) offset or absolute (x2, y2)")

    if (dx or 0) == 0 and (dy or 0) == 0:
        raise ValueError("a zero-vector (dx=dy=0) drag is a no-op; refused")

    return start, (start[0] + int(dx or 0), start[1] + int(dy or 0))


def plan_drag(
    target: Target,
    *,
    end: Tuple[int, int],
    start: Optional[Tuple[int, int]] = None,
    duration: float = DEFAULT_DURATION,
) -> PlannedAction:
    """Build the single A028 pointer.drag command for the resolved target.

    Pure planner — returns exactly one ``PlannedAction`` that dispatches
    ``pointer.drag`` (A028) with the computed ``start``/``end`` and the
    resolved control's ``target_id`` as the ``target_ref``. It never emits a
    wheel (A029) or a semantic click (A020).
    """
    return PlannedAction(
        action_id=DRAG_ACTION_ID,
        action_code=DRAG_ACTION_CODE,
        args={
            "start": {"x": start[0], "y": start[1]} if start else None,
            "end": {"x": end[0], "y": end[1]},
            "duration": float(duration),
        },
        target_ref={"target_id": target.target_id},
    )


def step_drag(
    *,
    target_ref: Mapping[str, object],
    before: ObservationSnapshot,
    dx: Optional[int] = None,
    dy: Optional[int] = None,
    x2: Optional[int] = None,
    y2: Optional[int] = None,
    grab: str = "handle",
    duration: float = DEFAULT_DURATION,
    verify: bool = True,
    observer: Optional[Observer] = None,
    verifier: Optional[PageVerifier] = None,
    executor: ScrollExecutor,
    threshold: float = 0.0,
    expected_process_name: Optional[str] = None,
) -> ScrollResult:
    """One pointer.drag (A028) from a resolved control to a release point.

    Resolves the drag source from ``target_ref`` against ``before``, verifies
    ownership, computes the grab/release points, dispatches exactly one A028,
    then (when ``verify=True``) verifies real movement via the observer.

    Returns a ``ScrollResult``:
      * ``SCROLLBAR_DRAGGED`` — dispatched and verified-moved.
      * ``NO_SCROLL_EFFECT``  — dispatched but content did not move (or
        ``verify=False``, which can never substantiate ``moved=True``).
      * ``NOT_DISPATCHED``    — resolution failed, plan was empty, or the
        dispatch receipt reported failure (nothing was ever armed).
    """
    try:
        target = resolve_drag_target(
            before, target_ref, expected_process_name=expected_process_name
        )
    except DragTargetResolutionError:
        # nothing resolvable -> nothing was ever armed.
        return ScrollResult.not_dispatched()
    if target.rect is None:
        return ScrollResult.not_dispatched()

    start, end = _compute_endpoints(
        rect=target.rect,
        grab=grab,
        dx=dx,
        dy=dy,
        x2=x2,
        y2=y2,
        start=None,
    )

    plan = [plan_drag(target, start=start, end=end, duration=duration)]
    if not plan:
        return ScrollResult.not_dispatched()

    receipts = executor.execute_plan(plan)
    if not receipts or not any(getattr(r, "ok", False) for r in receipts):
        return ScrollResult.not_dispatched()

    after = before
    if verify:
        if observer is None:
            raise ValueError(
                "drag verify=True requires an observer (wiring bug); "
                "pass observer= or verify=False"
            )
        after = observer.snapshot()
        moved = verifier.page_changed(before, after, threshold) is PageChange.CHANGED
        if not moved:
            return ScrollResult.no_effect()
        return ScrollResult.success_result(Strategy.SCROLLBAR_DRAG)

    return ScrollResult.no_effect()


def run_drag_target(
    source,
    *,
    target_ref: Mapping[str, object],
    dx: Optional[int] = None,
    dy: Optional[int] = None,
    x2: Optional[int] = None,
    y2: Optional[int] = None,
    grab: str = "handle",
    duration: float = DEFAULT_DURATION,
    verify: bool = True,
    threshold: float = 0.0,
    expected_process_name: Optional[str] = None,
    verifier: Optional[PageVerifier] = None,
    executor: Optional[ScrollExecutor] = None,
) -> dict:
    """Production facade for the server ``drag_target`` MCP tool.

    Builds the real chain against a live ``ScrollBackendSource`` and returns a
    ``ScrollResult``-shaped dict, honest on unavailability / resolution
    failure (NOT_DISPATCHED, never a fabricated success).
    """
    from .backend import BackendUnavailableError

    executor = executor or ScrollExecutor()
    verifier = verifier or PageVerifier()
    observer = source.make_observer()

    try:
        before = observer.snapshot()
    except BackendUnavailableError as e:
        return ScrollResult.not_dispatched().to_dict() | {"backend_error": str(e)}
    except ValueError:
        # no controls / no targets → nothing to drag.
        return ScrollResult.not_dispatched().to_dict()

    try:
        sr = step_drag(
            target_ref=target_ref,
            before=before,
            dx=dx,
            dy=dy,
            x2=x2,
            y2=y2,
            grab=grab,
            duration=duration,
            verify=verify,
            observer=observer,
            verifier=verifier,
            executor=executor,
            threshold=threshold,
            expected_process_name=expected_process_name,
        )
        return sr.to_dict()
    except DragTargetResolutionError as e:
        return ScrollResult.not_dispatched().to_dict() | {
            "error_code": e.code,
            "error": e.message,
        }
    except ValueError:
        return ScrollResult.not_dispatched().to_dict()


__all__ = [
    "DRAG_ACTION_ID",
    "DRAG_ACTION_CODE",
    "DEFAULT_DURATION",
    "DragTargetResolutionError",
    "resolve_drag_target",
    "center",
    "grab_point",
    "plan_drag",
    "step_drag",
    "run_drag_target",
]
