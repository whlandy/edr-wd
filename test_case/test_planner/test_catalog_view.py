"""P3.1 acceptance gate — Commit A: planner catalog view."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


_REPO = Path(__file__).resolve().parents[2]
for p in (str(_REPO / "target"), str(_REPO / "agent")):
    if p not in sys.path:
        sys.path.insert(0, p)


from action_catalog import (  # noqa: E402
    ACTIONS_V1,
    BACKEND_NOT_IMPLEMENTED,
    VALID_BACKENDS,
)
from planner.catalog_view import (  # noqa: E402
    PlannerToolEntry,
    enabled_actions_for,
    planner_tool_list,
)


# ---------------------------------------------------------------------------
# FR-P3.1-01 / Acceptance #1: enabled actions filter
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("backend,profile", [
    ("macos_accessibility", "macos_hisec"),
    ("macos_accessibility", "macos_generic"),
])
def test_acceptance_1_macos_hisec_excludes_backend_gaps(
    backend: str, profile: str,
):
    """Acceptance #1: catalog view for
    `(macos_accessibility, macos_hisec)` excludes `type_text`,
    `select`, `get_text`."""
    specs = enabled_actions_for(backend, profile)
    names = {s.tool_name for s in specs}
    for excluded in ("type_text", "select", "get_text"):
        assert excluded not in names, (
            f"{excluded!r} MUST be excluded for "
            f"({backend}, {profile}); got {names}"
        )


def test_windows_hisec_has_full_catalog():
    """Windows has no `BACKEND_NOT_IMPLEMENTED` gaps; all 27
    actions are visible."""
    specs = enabled_actions_for("windows_pywinauto", "windows_hisec")
    assert len(specs) == 27
    names = {s.tool_name for s in specs}
    assert names == {s.tool_name for s in ACTIONS_V1}


def test_enabled_count_matches_well_known_table():
    """Spot-check the per-backend/per-profile enabled count."""
    table = {
        ("macos_accessibility", "macos_hisec"): 24,
        ("macos_accessibility", "macos_generic"): 24,
        ("windows_pywinauto", "windows_hisec"): 27,
    }
    for (backend, profile), expected in table.items():
        actual = len(enabled_actions_for(backend, profile))
        assert actual == expected, (
            f"({backend}, {profile}): expected {expected}, got {actual}"
        )


# ---------------------------------------------------------------------------
# Strict-exclude (not just mark) — FR-P3.1-01 "absent, not just marked"
# ---------------------------------------------------------------------------


def test_excluded_actions_absent_not_marked_disabled():
    """The strict-exclude policy MUST NOT return disabled actions
    in the list at all (FR-P3.1-01 "absent, not just marked")."""
    for backend in VALID_BACKENDS:
        gaps = BACKEND_NOT_IMPLEMENTED.get(backend, {})
        for tool_name in gaps:
            specs = enabled_actions_for(backend)
            for spec in specs:
                assert spec.tool_name != tool_name, (
                    f"{tool_name!r} should be absent from "
                    f"{backend} enabled list"
                )


def test_tool_list_excludes_backend_gaps():
    """Same policy at the JSON wire format."""
    tools = planner_tool_list("macos_accessibility", "macos_hisec")
    names = {t["tool_name"] for t in tools}
    for excluded in ("type_text", "select", "get_text"):
        assert excluded not in names


# ---------------------------------------------------------------------------
# PlannerToolEntry shape
# ---------------------------------------------------------------------------


def test_planner_tool_entry_to_dict_has_required_keys():
    e = PlannerToolEntry(
        action_id="gui.click", tool_name="gui.click",
        description="click action",
        input_schema={"type": "object"},
    )
    d = e.to_dict()
    for key in (
        "action_id", "tool_name", "description", "input_schema",
        "requires", "side_effect", "risk", "rollback_class",
        "preferred_over", "enabled", "disabled_reason",
    ):
        assert key in d


def test_planner_tool_list_returns_dicts():
    tools = planner_tool_list("windows_pywinauto", "windows_hisec")
    assert len(tools) == 27
    assert all(isinstance(t, dict) for t in tools)


def test_planner_tool_list_includes_risk_and_side_effect():
    """The planner surface MUST carry risk + side_effect so the
    prompt can render safety guidance (per D14 confirmation
    policy)."""
    tools = planner_tool_list("windows_pywinauto", "windows_hisec")
    for t in tools:
        # `risk` may be the empty string for safe actions;
        # field MUST be present.
        assert "risk" in t
        assert "side_effect" in t


# ---------------------------------------------------------------------------
# Profile argument is forward-compatible
# ---------------------------------------------------------------------------


def test_profile_none_returns_backend_filter_only():
    """Profile-less callers see the same enabled set as the
    default profile (forward-compat with pre-P3.1 callers)."""
    specs_none = enabled_actions_for("macos_accessibility")
    specs_hisec = enabled_actions_for(
        "macos_accessibility", "macos_hisec",
    )
    assert {s.tool_name for s in specs_none} == {
        s.tool_name for s in specs_hisec
    }


@pytest.mark.parametrize("backend", [
    "windows_pywinauto",
    "macos_accessibility",
])
def test_unknown_backend_raises(backend: str):
    """Profile-less unknown backend MUST raise."""
    pass  # placeholder; real cases below


def test_unknown_backend_raises_value_error():
    with pytest.raises(ValueError, match="unknown backend"):
        enabled_actions_for("not_a_backend")


def test_unknown_backend_in_tool_list_raises():
    with pytest.raises(ValueError, match="unknown backend"):
        planner_tool_list("not_a_backend")


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_enabled_actions_for_is_deterministic():
    """Two consecutive calls return the same list in the same
    order (catalog is immutable; deterministic per P0.1 §7.2)."""
    a = enabled_actions_for("windows_pywinauto", "windows_hisec")
    b = enabled_actions_for("windows_pywinauto", "windows_hisec")
    assert [s.tool_name for s in a] == [s.tool_name for s in b]


def test_tool_list_is_deterministic():
    a = planner_tool_list("windows_pywinauto", "windows_hisec")
    b = planner_tool_list("windows_pywinauto", "windows_hisec")
    assert a == b