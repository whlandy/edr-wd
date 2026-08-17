"""
detect.py — generic, non-hard-coded page-structure detection (PR3).
(docs/todo/scroll-and-paged-table-actions.md, enforceability item 3.)

`PageDetector` classifies an `ObservationSnapshot` into a `PageStructure`
in **one** `detect()` pass and returns a `DetectionResult`. The helpers
`has_pagination` / `has_next_page` / `is_infinite_scroll` are thin projections
over that single pass — they are NOT separate heuristics (item 3).

Design rules (item 3 / "a strategy is chosen by structure"):

  * Classification is structural and keyword-based, never keyed to a single
    control name or table id. `nextPageButton` is only one HiSec instance of a
    detected pagination affordance — the detector recognises *kinds* of
    affordance (next / prev / load-more button, virtualized list) from the
    control's `control_type` + text/automation_id.
  * Only *enabled* owner controls count (a control is disabled/collapsed if
    its text/automation_id carries a disabled marker).
  * `confidence` (0..1) breaks the `PAGINATED` vs `LOAD_MORE` tie: a surface
    with both a next-button and a trailing load-more button is `PAGINATED`
    (it wins the tie), returning the highest-confidence owner in
    `next_controls`.

The detector is PURE (reads the snapshot, writes nothing) so
`detect(same_snapshot)` returns an equal `DetectionResult` (referential
transparency, item-3 acceptance).
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Tuple

try:
    from ..observations.models import ObservationSnapshot, Target
except ImportError:
    from observations.models import ObservationSnapshot, Target

if TYPE_CHECKING:
    from .results import Strategy

# PAGINATED / LOAD_MORE affordance keywords (case-insensitive substring).
# NOTE (f7): bare symbols `>`, `<`, `more` are deliberately NOT here — a
# substring `in` match would flag any text containing those characters/words
# ("a > b", "moreinfo", etc.) as an affordance. Chevron-only buttons are
# matched by exact/token text via `_chevron_affordance`; the word "more" must
# appear as a standalone token (`_load_more_token`), never as a bare substring.
_NEXT_MARKERS = (
    "next", "next page", "nextpage", "下一页", "下页", "next>>",
)
_PREV_MARKERS = (
    "prev", "previous", "上一页", "上页", "prevpage",
)
_LOAD_MORE_MARKERS = (
    "load more", "show more", "加载更多", "显示更多", "展开更多",
)
# Explicit chevron/arrow labels for next/prev buttons (exact or all-chevron
# text only — never a substring, so "crumb > sub" is not a next button).
_NEXT_CHEVRONS = (">", ">>", "»", "›", "→", "❯")
_PREV_CHEVRONS = ("<", "<<", "«", "‹", "←", "❮")
_DISABLED_MARKERS = (
    "disabled", "disable", "collapsed", "greyed", "grayed", "不可用",
)
# Structural markers that identify a virtualized / append-on-scroll list.
_VIRTUAL_MARKERS = (
    "virtual", "infinite", "recycle", "virtualized", "windowed",
)


class PageStructure(Enum):
    """Classification of a scrollable surface (item 3)."""

    PAGINATED = "paginated"      # numbered page controls / next+prev buttons
    INFINITE = "infinite"        # virtualized or append-on-scroll list
    TREE_LAZY = "tree_lazy"      # expand-on-demand tree, siblings hidden
    LOAD_MORE = "load_more"      # explicit "show more / load more" at end
    FLAT = "flat"                # no structured paging (plain scroll region)


class PageChange(Enum):
    """Tri-state content-change verdict (item 3 / item 6)."""

    CHANGED = "changed"
    UNCHANGED = "unchanged"
    UNCERTAIN = "uncertain"


@dataclass(frozen=True)
class DetectedControl:
    """A detected next/load-more/prev affordance tied to the resolved target."""

    target: Target
    enabled: bool  # per-control; False = disabled/collapsed


@dataclass(frozen=True)
class DetectionResult:
    """Output of one `detect()` pass (single pass, single result)."""

    structure: PageStructure
    next_controls: Tuple[DetectedControl, ...] = ()
    prev_controls: Tuple[DetectedControl, ...] = ()
    confidence: float = 0.0

    @property
    def owner(self) -> DetectedControl | None:
        """The single *enabled* owner control among next_controls (item 3:
        only enabled owner controls count)."""
        for c in self.next_controls:
            if c.enabled:
                return c
        return None

    @property
    def can_advance(self) -> bool:
        """Whether the surface can CURRENTLY advance — i.e. an enabled owner
        control exists. Distinguishes a paginated *structure* (has_pagination)
        from an actually-advancable page: a disabled next button must NOT
        report can_advance=True."""
        return self.owner is not None

    @property
    def has_next_page(self) -> bool:
        """DEPRECATED: use `can_advance` (same value). The old name was
        ambiguous — it really means "can currently move next", not "the page
        structure is paginated" (see has_pagination). Keep reading for
        backward-compat; slated for removal."""
        warnings.warn(
            "DetectionResult.has_next_page is deprecated; use .can_advance",
            DeprecationWarning,
            stacklevel=2,
        )
        return self.can_advance

    @property
    def has_available_next_page(self) -> bool:
        """Alias for `can_advance` — an enabled next page exists and can be
        advanced to right now."""
        return self.can_advance

    @property
    def has_pagination(self) -> bool:
        return self.structure is PageStructure.PAGINATED

    @property
    def is_infinite_scroll(self) -> bool:
        return self.structure is PageStructure.INFINITE


def _contains(text: str | None, markers: Tuple[str, ...]) -> bool:
    if not text:
        return False
    low = text.lower()
    return any(m in low for m in markers)


def _is_enabled(target: Target) -> bool:
    """Per-control enabledness (NOT collapsed). Derived structurally: a
    control is disabled if its text/automation_id carries a disabled marker."""
    if _contains(target.automation_id, _DISABLED_MARKERS):
        return False
    if _contains(target.text, _DISABLED_MARKERS):
        return False
    return True


def _text_only(candidate: str | None) -> str:
    return (candidate or "").strip()


def _is_all_chevrons(text: str | None, chevrons: Tuple[str, ...]) -> bool:
    """True only when the (trimmed) text is made *entirely* of chevron/arrow
    symbols (e.g. '>' or '>>' or '»'). Guards against breadcrumb text like
    'a > b' or 'Settings > Detail' being read as a next button."""
    if not text:
        return False
    t = _text_only(text)
    if not t:
        return False
    for ch in t:
        if ch not in "".join(chevrons):
            return False
    return True


def _load_more_token(text: str | None) -> bool:
    """True when 'more' appears as a standalone word (not a bare substring,
    so 'moreinfo' / 'moresettings' are NOT 'more' affordances)."""
    if not text:
        return False
    import re

    t = _text_only(text).lower()
    # standalone 'more' as a whole word, possibly with surrounding punctuation
    return bool(re.search(r"(^|[^a-z])more([^a-z]|$)", t))


def _kind_of(target: Target) -> str | None:
    """Which affordance kind a control implements, or None. Bare symbols are
    matched only as exact/whole-text chevrons, never as substrings (f7)."""
    if _contains(target.automation_id, _NEXT_MARKERS) or _contains(
        target.text, _NEXT_MARKERS
    ):
        return "next"
    if _is_all_chevrons(target.text, _NEXT_CHEVRONS):
        return "next"
    if _contains(target.automation_id, _PREV_MARKERS) or _contains(
        target.text, _PREV_MARKERS
    ):
        return "prev"
    if _is_all_chevrons(target.text, _PREV_CHEVRONS):
        return "prev"
    if _contains(target.automation_id, _LOAD_MORE_MARKERS) or _contains(
        target.text, _LOAD_MORE_MARKERS
    ):
        return "load_more"
    if _load_more_token(target.automation_id) or _load_more_token(target.text):
        return "load_more"
    return None


def _confidence(target: Target, kind: str) -> float:
    """Confidence that `target` is really a `kind` affordance. Exact id match
    on a pagination keyword scores higher than a loose text mention."""
    id_ = (target.automation_id or "").lower()
    text = (target.text or "").lower()
    if kind == "next":
        if id_ and _contains(id_, _NEXT_MARKERS):
            return 0.95
        if text and _contains(text, _NEXT_MARKERS):
            return 0.7
    if kind == "load_more":
        if id_ and _contains(id_, _LOAD_MORE_MARKERS):
            return 0.9
        if text and _contains(text, _LOAD_MORE_MARKERS):
            return 0.75
    return 0.5


class PageDetector:
    """Pure classifier over one snapshot. No pointer/UI state."""

    def detect(self, snapshot: ObservationSnapshot) -> DetectionResult:
        controls = [t for t in snapshot.targets if t.kind == "control"]
        if not controls:
            return DetectionResult(
                structure=PageStructure.FLAT, confidence=0.0
            )

        next_controls: list[DetectedControl] = []
        prev_controls: list[DetectedControl] = []
        load_more_controls: list[DetectedControl] = []
        virtualized = False

        for t in controls:
            kind = _kind_of(t)
            dc = DetectedControl(target=t, enabled=_is_enabled(t))
            if kind == "next":
                next_controls.append(dc)
            elif kind == "prev":
                prev_controls.append(dc)
            elif kind == "load_more":
                load_more_controls.append(dc)
            if _contains(t.automation_id, _VIRTUAL_MARKERS) or _contains(
                t.text, _VIRTUAL_MARKERS
            ):
                virtualized = True

        # Ordered by confidence (single-owner tie-break: PAGINATED wins).
        next_controls.sort(key=lambda dc: _confidence(dc.target, "next"),
                           reverse=True)
        load_more_controls.sort(
            key=lambda dc: _confidence(dc.target, "load_more"), reverse=True
        )

        if next_controls:
            # PAGINATED wins the PAGINATED-vs-LOAD_MORE tie (item 3).
            structure = PageStructure.PAGINATED
            owner = next_controls[0]
            confidence = _confidence(owner.target, "next")
            return DetectionResult(
                structure=structure,
                next_controls=tuple(next_controls),
                prev_controls=tuple(prev_controls),
                confidence=confidence,
            )

        if load_more_controls:
            owner = load_more_controls[0]
            return DetectionResult(
                structure=PageStructure.LOAD_MORE,
                next_controls=tuple(load_more_controls),
                prev_controls=tuple(prev_controls),
                confidence=_confidence(owner.target, "load_more"),
            )

        if virtualized:
            return DetectionResult(
                structure=PageStructure.INFINITE,
                prev_controls=tuple(prev_controls),
                confidence=0.8,
            )

        return DetectionResult(
            structure=PageStructure.FLAT,
            next_controls=tuple(next_controls),
            prev_controls=tuple(prev_controls),
            confidence=0.0,
        )

    def can_advance(self, snapshot: ObservationSnapshot) -> bool:
        """Whether an enabled next/load-more owner exists — the surface can be
        advanced right now (see DetectionResult.can_advance)."""
        return self.detect(snapshot).can_advance

    def has_pagination(self, snapshot: ObservationSnapshot) -> bool:
        return self.detect(snapshot).has_pagination

    def has_next_page(self, snapshot: ObservationSnapshot) -> bool:
        """DEPRECATED: use `.can_advance` (same value)."""
        warnings.warn(
            "PageDetector.has_next_page is deprecated; use .can_advance",
            DeprecationWarning,
            stacklevel=2,
        )
        return self.can_advance(snapshot)

    def is_infinite_scroll(self, snapshot: ObservationSnapshot) -> bool:
        return self.detect(snapshot).is_infinite_scroll


def structure_to_strategy(s: PageStructure) -> "Strategy":
    """Bridge to item 2's `Strategy` enum (consumed by strategy_order;
    imported lazily to avoid a circular import at module load)."""
    from .results import Strategy

    return {
        PageStructure.PAGINATED: Strategy.PAGINATION,
        PageStructure.INFINITE: Strategy.WHEEL,
        PageStructure.TREE_LAZY: Strategy.FOCUS_THEN_SCROLL,
        PageStructure.LOAD_MORE: Strategy.PAGINATION,
        PageStructure.FLAT: Strategy.WHEEL,
    }[s]


__all__ = [
    "PageStructure",
    "PageChange",
    "DetectedControl",
    "DetectionResult",
    "PageDetector",
    "structure_to_strategy",
]
