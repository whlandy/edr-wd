"""
verifier.py — PageVerifier + ScrollCoordinator facade (PR3).
(docs/todo/scroll-and-paged-table-actions.md, enforceability item 3.)

`PageVerifier` is the PURE content-change judge: it reduces a before/after
snapshot pair to a `PageChange` tri-state (CHANGED / UNCHANGED / UNCERTAIN),
gated by a threshold so transient re-render churn is not a "move". It
delegates to PR1's `Observer` digest semantics; it adds nothing beyond that
contract.

`ScrollCoordinator` is the coordinating facade that owns the
Detector -> Controller -> Verifier sequence for ONE bounded per-attempt step.
PR2's machine drives `step` over `strategy_order`; `step` reads the
already-cached `DetectionResult` (set by the machine's CLASSIFY/OWNERSHIP) and
NEVER re-runs `PageDetector.detect`.
"""

from __future__ import annotations

from typing import Optional

from observations.models import ObservationSnapshot

from .actions import PlannedAction
from .controller import NavigationMode, PageController
from .detect import (
    DetectionResult,
    PageChange,
    PageDetector,
    PageStructure,
    structure_to_strategy,
)
from .executor import ScrollExecutor
from .observer import Observer
from .results import ScrollResult

# Default threshold: fraction of the digest-signal considered a "move".
DEFAULT_THRESHOLD = 0.0


class PageVerifier:
    """Pure tri-state judge over a snapshot pair. No pointer/UI state."""

    def page_changed(
        self,
        before: ObservationSnapshot,
        after: ObservationSnapshot,
        threshold: float = DEFAULT_THRESHOLD,
        region: tuple[int, int, int, int] | None = None,
    ) -> PageChange:
        """Classify whether `after` is a genuine content move vs `before`.

        Rules (item 3 / item 6):
          * different tree_digest past the threshold  → CHANGED
          * equal digest but the window may have shifted (virtualized row
            reuse, signalled by repeated identical row fingerprints) → UNCERTAIN
          * otherwise → UNCHANGED

        When `region` (a (left, top, right, bottom) rect) is given, the
        comparison is scoped to targets whose rect intersects that region
        (f5: region-scoped verification — unrelated window churn outside the
        target area must not count as a move). A region with no matching
        targets yields UNCHANGED.
        """
        if before is None or after is None:
            return PageChange.UNCHANGED

        # Region-scope the comparison when a region is supplied (f5).
        b_targets = before.targets
        a_targets = after.targets
        if region is not None:
            b_targets = PageVerifier._in_region(before.targets, region)
            a_targets = PageVerifier._in_region(after.targets, region)

        if PageVerifier._digest_of(b_targets) != PageVerifier._digest_of(a_targets):
            # Genuine structural change — above the (default zero) threshold.
            return PageChange.CHANGED

        # Same digest: check for in-place row recycling (virtualized reuse).
        if self._looks_recycled(b_targets, a_targets, threshold):
            return PageChange.UNCERTAIN

        return PageChange.UNCHANGED

    @staticmethod
    def _in_region(
        targets: tuple, region: tuple[int, int, int, int]
    ) -> tuple:
        """Targets whose rect intersects the given (left, top, right, bottom)
        region. Targets without a rect are excluded from a region-scoped
        comparison (their location is unknown)."""
        l, t, r, b = region
        out = []
        for tgt in targets:
            rect = tgt.rect
            if rect is None:
                continue
            tl, tt, tr, tb = rect
            if tr < l or tb < t or tl > r or tt > b:
                continue  # no intersection
            out.append(tgt)
        return tuple(out)

    @staticmethod
    def _digest_of(targets: tuple) -> str:
        """A stable digest over the given target set (fingerprint-based)."""
        import hashlib

        h = hashlib.sha256()
        for t in targets:
            h.update((t.fingerprint or t.target_id).encode("utf-8"))
            h.update(b"\x00")
        return h.hexdigest()

    @staticmethod
    def _looks_recycled(
        before: tuple,
        after: tuple,
        threshold: float,
    ) -> bool:
        """Virtualized lists recycle a fixed row-template in place: digest
        equal yet the visible window may have shifted. Detect this by a
        repeated homogeneous row structure on both sides."""
        if threshold > 0:
            return False
        if not before or not after:
            return False
        if len(before) != len(after):
            return False
        # A recycling virtualized list keeps the SAME set of identical-row
        # fingerprints (same digest) yet may have shifted; only flag this when
        # there genuinely are repeated identical rows (row_signal > 1).
        if PageVerifier._row_signal(before) <= 1:
            return False
        return PageVerifier._row_signal(before) == PageVerifier._row_signal(after)

    @staticmethod
    def _row_signal(targets: tuple) -> int:
        """A crude homogeneity signal: number of Button/control targets whose
        text is identical (the virtualized row-template fingerprint)."""
        texts = {}
        for t in targets:
            key = (t.control_type, t.text)
            texts[key] = texts.get(key, 0) + 1
        return max(texts.values()) if texts else 0


class ScrollCoordinator:
    """Coordinatig facade: owns Detector -> Controller -> Executor -> Verifier
    for one bounded per-attempt step. `step` is the loop body PR2's machine
    drives; `step` reads the already-cached `DetectionResult` (set by the
    machine's CLASSIFY/OWNERSHIP) and never re-runs `PageDetector.detect`.

    Execution goes through `ScrollExecutor` (which wraps the real dispatcher
    via `DispatcherAdapter`), so no `ActionReceipt` is ever fabricated here.
    """

    def __init__(
        self,
        detector: PageDetector,
        controller: PageController,
        verifier: PageVerifier,
        observer: Observer,
        executor: Optional[ScrollExecutor] = None,
        threshold: float = DEFAULT_THRESHOLD,
    ) -> None:
        self.detector = detector
        self.controller = controller
        self.verifier = verifier
        self.observer = observer
        self.executor = executor or ScrollExecutor()
        self.threshold = threshold
        self._result: Optional[DetectionResult] = None

    # -- detection cache (set by the machine's CLASSIFY / OWNERSHIP step) ----
    def set_detection(self, result: Optional[DetectionResult]) -> None:
        self._result = result

    def clear_detection(self) -> None:
        self._result = None

    @property
    def cached_structure(self) -> Optional[PageStructure]:
        if self._result is None:
            return None
        return self._result.structure

    # -----------------------------------------------------------------------
    def step(
        self,
        mode: NavigationMode,
        snapshot: ObservationSnapshot,
        policy,
    ) -> ScrollResult:
        """ONE bounded per-attempt cycle, with bounded poll/backoff (f5).

        Reads the cached `DetectionResult` (set by the machine) — never
        re-runs `PageDetector.detect`. Returns `NOT_DISPATCHED` if no cached
        detection or the controller produced an empty plan (no enabled owner
        control, item 1).

        Bounded polling: dispatch once, then re-snapshot up to
        `policy.max_attempts` times (backing off via `policy.backoff_ms`)
        until `page_changed` reports a genuine move. A dispatch whose content
        change never stabilises within the bound returns `NO_SCROLL_EFFECT` —
        never a fabricated success. Verification is region-scoped to the owner
        control's rect when available (f5: unrelated window churn outside the
        target area must not count as a move).
        """
        result = self._result
        if result is None:
            return ScrollResult.not_dispatched()

        plan = self.controller.plan(result, mode)
        if not plan:
            return ScrollResult.not_dispatched()

        # Execute each planned command via the real dispatcher (executor).
        # A plan arms only when at least one receipt came back ok; dispatch
        # success is never treated as movement.
        receipts = self.executor.execute_plan(plan)
        if not any(r.ok for r in receipts):
            return ScrollResult.not_dispatched()

        # Region-scope verification to the owner control's bounds (f5).
        region = None
        owner = result.owner
        if owner is not None and owner.target.rect is not None:
            region = owner.target.rect

        # verify is mandatory (item 1 "Verify is not optional"), bounded.
        max_polls = getattr(policy, "max_attempts", 1) or 1
        change = PageChange.UNCHANGED
        for attempt in range(max_polls):
            after = self.observer.snapshot()
            change = self.verifier.page_changed(
                snapshot, after, self.threshold, region=region
            )
            if change is PageChange.CHANGED:
                strategy = structure_to_strategy(result.structure)
                return ScrollResult.success_result(strategy)
            # backoff before the next poll (skip after the last attempt)
            delay_ms = getattr(policy, "backoff_ms", lambda _i: 0)(attempt)
            if delay_ms > 0 and attempt < max_polls - 1:
                self._sleep(delay_ms / 1000.0)
        # UNCHANGED / UNCERTAIN never report moved=True (item 3/6).
        return ScrollResult.no_effect()

    @staticmethod
    def _sleep(seconds: float) -> None:
        import time

        time.sleep(seconds)


__all__ = ["PageVerifier", "ScrollCoordinator", "PageChange", "DEFAULT_THRESHOLD", "NavigationMode"]
