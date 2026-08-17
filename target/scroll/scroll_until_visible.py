"""
scroll_until_visible.py — target-driven bounded scroll (T3).
(docs/todo/scroll-and-paged-table-actions.md, "T3: scroll_until_visible".)

`scroll_until_visible` is the target-driven sibling of `page_table` (T2) and
`scroll_region` (PR3). Where those perform *one* bounded step and hand the
result back, this composite keeps advancing in ONE scrolling direction until
a text-identified control (``target_text_re``) actually appears in the
observation, under a hard ``max_steps`` bound.

Design decisions (each pinned by a unit test):

  * **Probe before the first dispatch.** One snapshot + one regex probe before
    any input is sent. If the target is already visible the action performs
    ZERO dispatches and returns ``Reason.TARGET_VISIBLE`` (``dispatched=False``,
    ``moved=False``). Nothing is ever wheeled/clicked "just to be sure".

  * **Stop conditions.** The loop stops immediately on ``NOT_DISPATCHED``
    (empty plan / failed dispatch) and — when ``verify=True`` — on
    ``NO_SCROLL_EFFECT`` (a step that did not move content). It never keeps
    scrolling a dead surface.

  * **Step accounting.** Only *moved-but-unmatched* steps count against
    ``max_steps``. A step that moved content but did not surface the target is
    counted and the loop continues. Under ``verify=False`` every scrolling step
    counts (movement is not verified, so no-effect cannot be detected).

  * **Bounded exhaustion.** When ``max_steps`` moves never surface the target,
    the loop terminates honestly with ``Reason.TARGET_NOT_FOUND`` — never an
    infinite spin, never a fabricated success.

  * **Direction mapping.** ``down`` -> ``MOVE_NEXT`` -> negative wheel clicks /
    semantic next (``PageController._plan_next``, ``DEFAULT_WHEEL_CLICKS = -3``).
    ``up``   -> ``MOVE_PREV`` -> positive wheel clicks / semantic prev
    (``PageController._plan_prev``). This reuses the existing controller, so
    direction correctly drives both the wheel sign and the pagination
    next/prev owner.

  * **verify=False never reports moved=True.** Without movement verification
    the composite cannot substantiate ``moved``, so the found result is reported
    with ``moved=False`` (``dispatched=True``) — the honest "scrolled, target
    observed, movement unverified" signal flags ``TARGET_VISIBLE``.

  * **Never report success unless the target was actually observed.** The
    target probe is the ONLY gate on a success outcome. ``success`` is still
    ``dispatched and moved`` (a goal-reached outcome after a verified scroll),
    and it is only ever returned from the branch where the probe matched.

Pure module — no MCP transport. It composes the injected ``detector`` /
``controller`` / ``executor`` / ``observer`` / ``verifier`` (as do the sibling
`backend.py` facades), so it is unit-testable without a live backend.
"""

from __future__ import annotations

import re
from typing import List, Optional

try:
    from ..observations.models import ObservationSnapshot, Target
except ImportError:
    from observations.models import ObservationSnapshot, Target

from .actions import PlannedAction
from .controller import NavigationMode, PageController
from .detect import DetectionResult, PageDetector, structure_to_strategy
from .executor import ScrollExecutor
from .observer import Observer
from .results import Reason, ScrollResult
from .verifier import PageChange, PageVerifier

# Default hard bound: how many moved-but-unmatched scrolling steps to allow
# before giving up. `0` is forbidden (we want an explicit caller bound).
DEFAULT_MAX_STEPS = 8

# The reason terminals this composite adds on top of the PR1 set.
_TARGET_VISIBLE = Reason.TARGET_VISIBLE
_TARGET_NOT_FOUND = Reason.TARGET_NOT_FOUND


def _compile(regex: str) -> "re.Pattern[str]":
    """Compile the caller-supplied target-text regex (case-insensitive)."""
    try:
        return re.compile(regex, re.IGNORECASE)
    except re.error as exc:  # pragma: no cover - defensive; validated by caller
        raise ValueError(f"invalid target_text_re {regex!r}: {exc}") from exc


def find_target(snapshot: ObservationSnapshot, target_text_re: str) -> Optional[Target]:
    """Pure probe: first *control* whose ``text`` matches ``target_text_re``.

    Only controls (``kind == \"control\"``) are considered — a window or
    dialog title is not a scrollable list item. Returns the first match in
    tree order, or ``None`` when nothing matches.
    """
    matcher = _compile(target_text_re)
    for tgt in snapshot.targets:
        if tgt.kind != "control":
            continue
        if tgt.text and matcher.search(tgt.text):
            return tgt
    return None


def scroll_until_visible(
    *,
    target_text_re: str,
    direction: str = "down",
    max_steps: int = DEFAULT_MAX_STEPS,
    verify: bool = True,
    detector,
    controller: Optional[PageController] = None,
    executor: ScrollExecutor,
    observer: Observer,
    verifier: Optional[PageVerifier] = None,
    threshold: float = 0.0,
) -> ScrollResult:
    """Advance one direction until ``target_text_re`` is observed (bounded).

    Args are injected explicitly (no hidden globals) so tests can drive the
    loop with stubs and the `backend.py` facade wires the real chain.

    Returns a ``ScrollResult`` whose outcome is one of:
      * ``TARGET_VISIBLE``  — target observed. ``dispatched/moved`` are False
                              when it was already visible (zero input), or
                              True when a verified scroll surfaced it.
      * ``NOT_DISPATCHED``  — the very first scroll step could not be armed.
      * ``NO_SCROLL_EFFECT``— a verified step moved no content (verify=True).
      * ``TARGET_NOT_FOUND``— ``max_steps`` moved-but-unmatched steps elapsed.
    """
    if direction not in ("down", "up"):
        raise ValueError(f"unknown scroll_until_visible direction {direction!r}; "
                         f"expected down|up")
    if max_steps < 1:
        raise ValueError("max_steps must be >= 1")
    if target_text_re is None or not str(target_text_re).strip():
        raise ValueError("target_text_re is required")

    controller = controller or PageController()
    verifier = verifier or PageVerifier()
    mode = NavigationMode.MOVE_NEXT if direction == "down" else NavigationMode.MOVE_PREV

    # 1) Probe BEFORE the first dispatch — zero input if already visible.
    before = observer.snapshot()
    if find_target(before, target_text_re) is not None:
        return ScrollResult(
            dispatched=False, moved=False, reason=_TARGET_VISIBLE,
        )

    result: DetectionResult = detector.detect(before)

    steps = 0
    while steps < max_steps:
        plan: List[PlannedAction] = controller.plan(result, mode)
        if not plan:
            # Nothing to arm → NOT_DISPATCHED (terminal: stop immediately).
            return ScrollResult.not_dispatched()

        receipts = executor.execute_plan(plan)
        if not receipts or not any(getattr(r, "ok", False) for r in receipts):
            return ScrollResult.not_dispatched()

        after = observer.snapshot()

        moved = False
        if verify:
            moved = verifier.page_changed(before, after, threshold) is PageChange.CHANGED

        # 2) Target observed → goal reached (honest success only if verified
        #    movement; verify=False keeps moved=False).
        if find_target(after, target_text_re) is not None:
            if moved:
                return ScrollResult.success_result(
                    structure_to_strategy(result.structure),
                    reason=_TARGET_VISIBLE,
                )
            return ScrollResult(
                dispatched=True, moved=False, reason=_TARGET_VISIBLE,
            )

        # 3) No target yet.
        if verify and not moved:
            # A step that moved nothing cannot bring the target closer; stop.
            return ScrollResult.no_effect()

        # Moved-but-unmatched step → counted against the bound, keep scanning.
        steps += 1
        before = after

    # Bounded exhaustion — target never observed.
    return ScrollResult(dispatched=True, moved=False, reason=_TARGET_NOT_FOUND)


def run_scroll_until_visible(
    source,
    *,
    target_text_re: str,
    direction: str = "down",
    max_steps: int = DEFAULT_MAX_STEPS,
    verify: bool = True,
    detector: Optional[PageDetector] = None,
    verifier: Optional[PageVerifier] = None,
    executor: Optional[ScrollExecutor] = None,
    controller: Optional[PageController] = None,
    threshold: float = 0.0,
) -> dict:
    """Production facade for the server.py ``scroll_until_visible`` MCP tool.

    Mirrors ``run_scroll_region`` / ``run_page_table``: builds the real chain
    against the live ``ScrollBackendSource`` and returns a ``ScrollResult``-
    shaped dict, honest on unavailability.
    """
    from .backend import BackendUnavailableError

    detector = detector or PageDetector()
    verifier = verifier or PageVerifier()
    executor = executor or ScrollExecutor()
    controller = controller or PageController()
    observer = source.make_observer()

    try:
        sr = scroll_until_visible(
            target_text_re=target_text_re,
            direction=direction,
            max_steps=max_steps,
            verify=verify,
            detector=detector,
            controller=controller,
            executor=executor,
            observer=observer,
            verifier=verifier,
            threshold=threshold,
        )
        return sr.to_dict()
    except BackendUnavailableError as e:
        return ScrollResult.not_dispatched().to_dict() | {"backend_error": str(e)}
    except ValueError:
        # no controls / no targets → nothing to scroll or probe.
        return ScrollResult.not_dispatched().to_dict()


__all__ = [
    "find_target",
    "scroll_until_visible",
    "run_scroll_until_visible",
    "DEFAULT_MAX_STEPS",
]
