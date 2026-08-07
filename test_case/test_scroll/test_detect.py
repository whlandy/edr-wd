"""
PR3 acceptance — PageDetector generic structural classification
(docs/todo/scroll-and-paged-table-actions.md, enforceability item 3).
"""

from __future__ import annotations

import pytest


from observations.snapshot import build_snapshot
from scroll.detect import (
    PageDetector,
    PageStructure,
    structure_to_strategy,
)
from scroll.results import Strategy


def _snap(targets):
    return build_snapshot(targets=targets, backend="x", host="h",
                          captured_at="2026-08-01T00:00:00Z")


def _win(title="Main"):
    return {"process_name": "X", "pid": 1, "native_window_id": "w",
            "title": title, "kind": "window"}


def _btn(title, automation_id=None, text=None):
    d = {"process_name": "X", "pid": 1, "native_window_id": "w",
         "title": title, "kind": "control", "control_type": "Button"}
    if automation_id is not None:
        d["automation_id"] = automation_id
    if text is not None:
        d["text"] = text
    return d


def _paged_fixture():
    return [_win(), _btn("next", "nextPageButton", "下一页"),
            _btn("prev", "prevPageButton", "上一页")]


def _load_more_fixture():
    return [_win(), _btn("more", "loadMoreButton", "加载更多")]


def _infinite_fixture():
    return [_win(),
            {"process_name": "X", "pid": 1, "native_window_id": "w",
             "title": "virtual", "kind": "control",
             "control_type": "List", "automation_id": "virtualList"}]


def _flat_fixture():
    return [_win(),
            {"process_name": "X", "pid": 1, "native_window_id": "w",
             "title": "table", "kind": "control",
             "control_type": "Table", "automation_id": "plainTable"}]


# ---------------------------------------------------------------------------
# single-pass detection
# ---------------------------------------------------------------------------


def test_detect_paginated():
    d = PageDetector().detect(_snap(_paged_fixture()))
    assert d.structure is PageStructure.PAGINATED
    assert d.has_pagination
    assert d.can_advance
    assert d.owner is not None and d.owner.enabled


def test_detect_load_more():
    d = PageDetector().detect(_snap(_load_more_fixture()))
    assert d.structure is PageStructure.LOAD_MORE
    assert not d.has_pagination
    assert d.owner is not None


def test_detect_infinite():
    d = PageDetector().detect(_snap(_infinite_fixture()))
    assert d.structure is PageStructure.INFINITE
    assert d.is_infinite_scroll
    assert not d.can_advance


def test_detect_flat():
    d = PageDetector().detect(_snap(_flat_fixture()))
    assert d.structure is PageStructure.FLAT
    assert not d.has_pagination and not d.can_advance


def test_referential_transparency_same_snapshot():
    snap = _snap(_paged_fixture())
    a = PageDetector().detect(snap)
    b = PageDetector().detect(snap)
    assert a.structure is b.structure
    assert a.confidence == b.confidence
    assert [c.target.target_id for c in a.next_controls] == [
        c.target.target_id for c in b.next_controls
    ]


def test_generic_across_screens():
    """The classifier is not keyed to a control id — any button whose text/
    automation_id is a next affordance classifies as PAGINATED regardless of
    the concrete id (user/alert/asset/operation screens)."""
    for aid in ("nextPageButton", "btn-next", "goNext", None):
        targets = [_win(), _btn("next", aid, "下一页")]
        d = PageDetector().detect(_snap(targets))
        assert d.structure is PageStructure.PAGINATED, aid


# ---------------------------------------------------------------------------
# f7: tightened detection rules (no bare `>` / `<` / `more` substrings)
# ---------------------------------------------------------------------------


def test_breadcrumb_with_gt_is_not_next_button():
    """f7: text 'a > b' (breadcrumb) must NOT be classified as a next
    button — the bare '>' is no longer a substring next-marker."""
    targets = [_win(), _btn("crumb", "crumbLabel", "Settings > Detail")]
    d = PageDetector().detect(_snap(targets))
    assert d.structure is PageStructure.FLAT
    assert d.owner is None


def test_chevron_only_text_is_next_button():
    """f7: a button whose text is *entirely* the next chevron IS a next
    affordance (exact/whole-text match, not substring)."""
    for txt in (">", ">>", "»", "→", "❯"):
        targets = [_win(), _btn("next", "nextArrow", txt)]
        d = PageDetector().detect(_snap(targets))
        assert d.structure is PageStructure.PAGINATED, repr(txt)
        assert d.owner is not None and d.owner.enabled


def test_prev_chevron_only_text_is_prev_button():
    """f7: a prev-only chevron is detected as a prev control (in prev_controls)
    but does not make the surface PAGINATED — there is no next control to
    advance to, so has_next_page stays False."""
    for txt in ("<", "<<", "«", "←", "❮"):
        targets = [_win(), _btn("prev", "prevArrow", txt)]
        d = PageDetector().detect(_snap(targets))
        assert d.prev_controls, repr(txt)
        assert d.prev_controls[0].target.text == txt
        assert d.can_advance is False


def test_more_substring_word_is_not_load_more():
    """f7: 'moreinfo' / 'moresettings' (bare 'more' as a substring) must
    NOT be classified as load-more."""
    for txt in ("moreinfo", "moresettings", "MoreOptions"):
        targets = [_win(), _btn("x", "btn", txt)]
        d = PageDetector().detect(_snap(targets))
        assert d.structure is PageStructure.FLAT, txt
        assert d.owner is None


def test_standalone_more_word_is_load_more():
    """f7: a standalone 'more' word (whole token) still classifies as
    load-more."""
    for txt in ("more", "More", "load more", "show more"):
        targets = [_win(), _btn("more", "loadMoreBtn", txt)]
        d = PageDetector().detect(_snap(targets))
        assert d.structure is PageStructure.LOAD_MORE, txt


def test_filter_with_gt_text_not_next():
    """f7: a filter-style control carrying a '>' (e.g. 'Sort > Asc') must not
    be treated as a paging next button."""
    targets = [_win(), _btn("sort", "sortBtn", "Sort > Asc")]
    d = PageDetector().detect(_snap(targets))
    assert d.structure is PageStructure.FLAT



# ---------------------------------------------------------------------------
# disabled owner
# ---------------------------------------------------------------------------


def test_disabled_next_button_is_not_owner():
    targets = [_win(), _btn("next", "nextPageButton", "下一页disabled"),
               _btn("prev", "prevPageButton", "上一页")]
    d = PageDetector().detect(_snap(targets))
    # Structure is still PAGINATED; the disabled next control is excluded.
    assert d.structure is PageStructure.PAGINATED
    assert d.owner is None
    # f4: a disabled next control means the surface is paginated in structure
    # but NOT currently advancable — can_advance must be False.
    assert d.can_advance is False
    assert d.has_pagination is True


# ---------------------------------------------------------------------------
# PAGINATED vs LOAD_MORE tie-break
# ---------------------------------------------------------------------------


def test_paginated_wins_tie_over_load_more():
    targets = [_win(), _btn("next", "nextPageButton", "下一页"),
               _btn("more", "loadMoreButton", "加载更多")]
    d = PageDetector().detect(_snap(targets))
    assert d.structure is PageStructure.PAGINATED


# ---------------------------------------------------------------------------
# thin helpers call detect once
# ---------------------------------------------------------------------------


def test_helpers_are_projections_of_single_detect():
    detector = PageDetector()
    snap_p = _snap(_paged_fixture())
    # Each helper calls detect internally; verify behaviour matches detect().
    assert detector.has_pagination(snap_p) is True
    assert detector.can_advance(snap_p) is True
    assert detector.is_infinite_scroll(snap_p) is False

    snap_i = _snap(_infinite_fixture())
    assert detector.is_infinite_scroll(snap_i) is True
    assert detector.has_pagination(snap_i) is False


def test_has_next_page_is_deprecated_alias_of_can_advance():
    """The legacy `has_next_page` name still works (backward-compat) but emits
    DeprecationWarning and equals `can_advance` — so callers can migrate and
    the old name can eventually be removed."""
    detector = PageDetector()
    snap_p = _snap(_paged_fixture())
    with pytest.warns(DeprecationWarning, match=r"use \.can_advance"):
        r = detector.has_next_page(snap_p)
    assert r is detector.can_advance(snap_p) is True

    d = PageDetector().detect(snap_p)
    with pytest.warns(DeprecationWarning, match=r"use \.can_advance"):
        legacy = d.has_next_page
    assert legacy == d.can_advance


# ---------------------------------------------------------------------------
# structure -> strategy bridge
# ---------------------------------------------------------------------------


def test_structure_to_strategy_mapping():
    assert structure_to_strategy(PageStructure.PAGINATED) is Strategy.PAGINATION
    assert structure_to_strategy(PageStructure.INFINITE) is Strategy.WHEEL
    assert structure_to_strategy(PageStructure.TREE_LAZY) is Strategy.FOCUS_THEN_SCROLL
    assert structure_to_strategy(PageStructure.LOAD_MORE) is Strategy.PAGINATION
    assert structure_to_strategy(PageStructure.FLAT) is Strategy.WHEEL


def test_detect_requires_controls():
    # No controls => FLAT, never crash.
    d = PageDetector().detect(_snap([_win()]))
    assert d.structure is PageStructure.FLAT
