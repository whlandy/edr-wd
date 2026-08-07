"""
PR1 acceptance — Observer determinism + content_changed
(docs/todo/scroll-and-paged-table-actions.md, enforceability item 6).
"""

from __future__ import annotations

import copy

import pytest


from observations.models import ObservationSnapshot
from observations.snapshot import build_snapshot
from scroll.observer import DiffStrategy, Observer


def _snap(targets, backend="x", host="h"):
    return build_snapshot(targets=targets, backend=backend, host=host,
                          captured_at="2026-08-01T00:00:00Z")


_BASE = [
    {"process_name": "X", "pid": 1, "native_window_id": "w",
     "title": "Main", "kind": "window"},
    {"process_name": "X", "pid": 1, "native_window_id": "w",
     "title": "OK", "kind": "control", "control_type": "Button",
     "text": "value"},
]


# ---------------------------------------------------------------------------
# snapshot() determinism
# ---------------------------------------------------------------------------


def test_snapshot_deterministic_same_targets_fn():
    calls = {"n": 0}

    def targets_fn():
        calls["n"] += 1
        return list(_BASE)

    obs = Observer(targets_fn=targets_fn, backend="x", host="h")
    s1 = obs.snapshot()
    s2 = obs.snapshot()
    assert isinstance(s1, ObservationSnapshot)
    assert s1.tree_digest == s2.tree_digest
    assert calls["n"] == 2


def test_snapshot_requires_targets_fn():
    obs = Observer()
    with pytest.raises(ValueError):
        obs.snapshot()


def test_snapshot_requires_nonempty_targets():
    obs = Observer(targets_fn=lambda: [], backend="x", host="h")
    with pytest.raises(ValueError):
        obs.snapshot()


# ---------------------------------------------------------------------------
# content_changed — TREE_DIGEST
# ---------------------------------------------------------------------------


def test_content_changed_false_for_equal_digests():
    obs = Observer()
    a = _snap(list(_BASE))
    b = _snap(list(reversed(_BASE)))  # same set, reordered => equal digest
    assert obs.content_changed(a, b) is False


def test_content_changed_true_for_different_digests():
    obs = Observer()
    a = _snap(list(_BASE))
    mod = copy.deepcopy(_BASE)
    mod[1]["title"] = "OKAY"
    b = _snap(mod)
    assert obs.content_changed(a, b) is True


def test_content_changed_explicit_strategy():
    obs = Observer()
    a = _snap(list(_BASE))
    mod = copy.deepcopy(_BASE)
    mod[1]["text"] = "changed"
    b = _snap(mod)
    assert obs.content_changed(a, b, strategy=DiffStrategy.TREE_DIGEST) is True


def test_diff_returns_tree_digest_scalar():
    obs = Observer()
    a = _snap(list(_BASE))
    b = _snap(list(_BASE))
    d = obs.diff(a, b, strategy=DiffStrategy.TREE_DIGEST)
    assert d.changed is False
    assert d.strategy is DiffStrategy.TREE_DIGEST
    assert d.before == a.tree_digest and d.after == b.tree_digest


# ---------------------------------------------------------------------------
# FINGERPRINT strategy (implemented in PR1 for locality)
# ---------------------------------------------------------------------------


def test_fingerprint_strategy_changed():
    obs = Observer()
    a = _snap(list(_BASE))
    mod = copy.deepcopy(_BASE)
    mod[1]["automation_id"] = "btn-new"
    b = _snap(mod)
    d = obs.diff(a, b, strategy=DiffStrategy.FINGERPRINT)
    assert d.changed is True
    assert isinstance(d.before, frozenset)


def test_fingerprint_strategy_unmoved_for_rect_only_change():
    # rect is NOT an identity field, so a pure layout change is not "moved"
    # under the fingerprint strategy.
    obs = Observer()
    a = _snap(list(_BASE))
    mod = copy.deepcopy(_BASE)
    mod[1]["rect"] = (1, 2, 3, 4)
    b = _snap(mod)
    assert obs.content_changed(a, b, strategy=DiffStrategy.FINGERPRINT) is False


# ---------------------------------------------------------------------------
# SCREENSHOT strategy is proposed-only
# ---------------------------------------------------------------------------


def test_screenshot_strategy_not_implemented():
    obs = Observer()
    a = _snap(list(_BASE))
    b = _snap(list(_BASE))
    with pytest.raises(NotImplementedError):
        obs.diff(a, b, strategy=DiffStrategy.SCREENSHOT)


# ---------------------------------------------------------------------------
# Type/validation
# ---------------------------------------------------------------------------


def test_diff_rejects_non_snapshot_inputs():
    obs = Observer()
    with pytest.raises(TypeError):
        obs.diff("not-a-snapshot", _snap(list(_BASE)))
