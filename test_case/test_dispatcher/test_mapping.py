"""
P1.1 acceptance gate — ActionDispatchMap + catalog coverage.

FR-P1.1-01 / FR-P1.1-10: every catalog entry must resolve to a
backend method or surface dispatch_target_missing cleanly.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


_REPO = Path(__file__).resolve().parents[2]
_TARGET = _REPO / "target"

if str(_TARGET) not in sys.path:
    sys.path.insert(0, str(_TARGET))


import action_dispatcher as ad          # noqa: E402
from action_catalog import ACTIONS_V1    # noqa: E402


class _NullBackend:
    """Backend with no methods. Use to assert
    `resolve_backend_method` returns None for unimplemented tools."""


def test_dispatch_map_has_unique_action_ids_and_tool_names():
    dmap = ad.ActionDispatchMap.from_catalog()
    assert len(dmap.by_action_id) == 27
    assert len(dmap.by_tool_name) == 27
    assert set(dmap.by_action_id.keys()) == {s.action_id for s in ACTIONS_V1}
    assert set(dmap.by_tool_name.keys()) == {s.tool_name for s in ACTIONS_V1}


def test_resolve_backend_method_returns_callable_when_present():
    class B:
        def click(self, **kw):
            return {"ok": True}

    dmap = ad.ActionDispatchMap.from_catalog()
    method = dmap.resolve_backend_method(B(), "gui.click")
    assert callable(method)


def test_resolve_backend_method_returns_none_when_method_missing():
    dmap = ad.ActionDispatchMap.from_catalog()
    assert dmap.resolve_backend_method(_NullBackend(), "gui.click") is None


def test_resolve_backend_method_returns_none_for_unknown_action_id():
    dmap = ad.ActionDispatchMap.from_catalog()
    assert dmap.resolve_backend_method(_NullBackend(), "foo.bar") is None


def test_resolve_backend_method_returns_none_for_server_inline_action():
    """server-inline actions (execution_provider != "backend") are
    NOT routed through the dispatcher; resolve_backend_method
    returns None even when a backend attribute happens to share the
    tool_name."""
    class B:
        def restore_edr(self):
            return {"ok": True}

    dmap = ad.ActionDispatchMap.from_catalog()
    assert dmap.resolve_backend_method(B(), "hisec.restore_edr") is None


def test_check_backend_coverage_covers_all_27():
    cov = ad.check_backend_coverage("unknown_backend")
    assert len(cov) == 27
    # Coverage reflects BACKEND_NOT_IMPLEMENTED; an unknown backend
    # is not in the table, so every entry shows False.
    assert all(v is False for v in cov.values())


def test_is_mutating_classification():
    from action_catalog import ACTIONS_V1
    dmap = ad.ActionDispatchMap.from_catalog()
    for spec in ACTIONS_V1:
        expected = spec.side_effect in ad.MUTATING_SIDE_EFFECTS
        actual = ad.is_mutating(spec)
        assert actual is expected, (
            f"{spec.action_id}: side_effect={spec.side_effect!r}; "
            f"expected mutating={expected}"
        )


def test_dispatch_all_actions_resolve():
    """For a backend that implements every backend-routed tool,
    dispatch_map.resolve_backend_method returns a callable for
    every action_id whose execution_provider is 'backend'.

    This is the structural half of FR-P1.1-10."""
    class FullBackend:
        # Provide every tool_name that any V1 action routes to.
        pass

    # Build a backend class with every tool_name as a method.
    tool_names = {s.tool_name for s in ACTIONS_V1
                  if s.execution_provider == "backend"}
    ns = {name: (lambda self, **kw: {"ok": True})
          for name in tool_names}
    FullBackend = type("FullBackend", (), ns)
    backend = FullBackend()

    dmap = ad.ActionDispatchMap.from_catalog()
    for spec in ACTIONS_V1:
        if spec.execution_provider == "backend":
            method = dmap.resolve_backend_method(backend, spec.action_id)
            assert callable(method), (
                f"backend has no method for {spec.action_id!r} "
                f"(tool_name={spec.tool_name!r})"
            )