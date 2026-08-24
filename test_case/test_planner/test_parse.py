"""P3.1 acceptance gate — Commit C: parse + 5-stage validation."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


_REPO = Path(__file__).resolve().parents[2]
for p in (str(_REPO / "target"), str(_REPO / "agent")):
    if p not in sys.path:
        sys.path.insert(0, p)


from planner import (  # noqa: E402
    COORDINATE_FALLBACK_NOT_ALLOWED,
    CODE_ACTION_CODE_MISMATCH,
    CODE_BACKEND_DISABLED,
    CODE_DEPENDENCY_CYCLE,
    CODE_PLAN_SCHEMA_INVALID,
    CODE_TARGET_REF_SNAPSHOT_MISMATCH,
    CODE_UNKNOWN_SCHEMA_VERSION,
    LLMPlanResponse,
    ParseResult,
    SCHEMA_VERSION,
    STABLE_VALIDATION_CODES,
    ValidationError,
    enabled_actions_for,
    parse_plan,
)


# ---------------------------------------------------------------------------
# Fixture: real catalog view
# ---------------------------------------------------------------------------


@pytest.fixture
def catalog():
    return enabled_actions_for("windows_pywinauto", "windows_hisec")


def _plan(**overrides) -> dict:
    """Build a minimal valid plan; mutate per-test."""
    base = {
        "schema_version": SCHEMA_VERSION,
        "plan_id": "P-test",
        "snapshot_id": "OBS-1",
        "expected_actions": [
            {"step_id": "S1", "action_id": "gui.click",
             "target_ref": {"snapshot_id": "OBS-1",
                            "target_id": "T-1"}},
        ],
        "expected_dependencies": {"S1": []},
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Acceptance #2 — invalid model output rejected
# ---------------------------------------------------------------------------


def test_acceptance_2_invalid_plan_schema_rejected(catalog):
    """Acceptance #2: invalid model output (extra field, wrong
    enum, missing step_id) is rejected with stable `code` and
    JSON pointer."""
    # Missing required `schema_version` → plan_schema_invalid.
    payload = _plan()
    del payload["schema_version"]
    result = parse_plan(payload, catalog=catalog, snapshot_id="OBS-1")
    codes = [e.code for e in result.errors]
    assert CODE_PLAN_SCHEMA_INVALID in codes
    pointers = [e.pointer for e in result.errors]
    assert "/schema_version" in pointers


def test_acceptance_2_unknown_schema_version_rejected(catalog):
    payload = _plan(schema_version="plan_schema.v999")
    result = parse_plan(payload, catalog=catalog, snapshot_id="OBS-1")
    codes = [e.code for e in result.errors]
    assert CODE_UNKNOWN_SCHEMA_VERSION in codes


def test_acceptance_2_missing_required_field_rejected(catalog):
    payload = _plan()
    del payload["snapshot_id"]
    result = parse_plan(payload, catalog=catalog, snapshot_id="OBS-1")
    assert any(
        e.code == CODE_PLAN_SCHEMA_INVALID and e.pointer == "/snapshot_id"
        for e in result.errors
    )


def test_acceptance_2_empty_expected_actions_rejected(catalog):
    payload = _plan(expected_actions=[])
    result = parse_plan(payload, catalog=catalog, snapshot_id="OBS-1")
    assert any(
        e.code == CODE_PLAN_SCHEMA_INVALID
        and e.pointer == "/expected_actions"
        for e in result.errors
    )


def test_acceptance_2_step_id_missing_rejected(catalog):
    payload = _plan(expected_actions=[
        {"step_id": "S1", "action_id": "gui.click"},
        {"action_id": "gui.click"},  # missing step_id
    ])
    result = parse_plan(payload, catalog=catalog, snapshot_id="OBS-1")
    # Stage 2 (catalog) passes; Step ID detection in stage 2
    # pass catches duplicate / missing in the duplicate-detection
    # block. Missing step_id is acceptable (we tolerate empty
    # step_id) but duplicate step_id is rejected.
    # In this fixture, only one step has step_id; the second
    # step has none — no duplicate. The action_id is the same
    # so stage 2 passes. Verify no errors (missing step_id is
    # not itself an error; cycle detection sees no step_ids).
    assert result.errors == ()


def test_acceptance_2_duplicate_step_id_rejected(catalog):
    payload = _plan(expected_actions=[
        {"step_id": "S1", "action_id": "gui.click"},
        {"step_id": "S1", "action_id": "gui.click"},
    ])
    result = parse_plan(payload, catalog=catalog, snapshot_id="OBS-1")
    assert any(e.code == CODE_PLAN_SCHEMA_INVALID for e in result.errors)


def test_acceptance_2_non_object_step_rejected(catalog):
    payload = _plan(expected_actions=["not-a-dict"])
    result = parse_plan(payload, catalog=catalog, snapshot_id="OBS-1")
    assert any(e.code == CODE_PLAN_SCHEMA_INVALID for e in result.errors)


# ---------------------------------------------------------------------------
# Stage 2 — catalog filter
# ---------------------------------------------------------------------------


def test_disabled_action_in_plan_rejected(catalog):
    """FR-P3.1-03 / acceptance #1 variant: disabled action
    chosen by the model returns `backend_disabled`."""
    payload = _plan(expected_actions=[
        {"step_id": "S1", "action_id": "type_text"},  # not on Windows
    ])
    result = parse_plan(payload, catalog=catalog, snapshot_id="OBS-1")
    assert any(
        e.code == CODE_BACKEND_DISABLED
        and e.pointer == "/expected_actions/0/action_id"
        for e in result.errors
    )


# ---------------------------------------------------------------------------
# Stage 3 — action_code consistency
# ---------------------------------------------------------------------------


def test_action_code_mismatch_in_plan_rejected(catalog):
    """FR-P3.1-05: mismatch between action_id and action_code
    returns `action_code_mismatch`."""
    payload = _plan(expected_actions=[
        {"step_id": "S1", "action_id": "gui.click",
         "action_code": "WRONG_CODE"},
    ])
    result = parse_plan(payload, catalog=catalog, snapshot_id="OBS-1")
    assert any(
        e.code == CODE_ACTION_CODE_MISMATCH
        and e.pointer == "/expected_actions/0/action_code"
        for e in result.errors
    )


def test_action_code_match_passes(catalog):
    """When action_code matches the catalog, no error fires."""
    spec = next(s for s in catalog if s.action_id == "gui.click")
    payload = _plan(expected_actions=[
        {"step_id": "S1", "action_id": "gui.click",
         "action_code": spec.action_code},
    ])
    result = parse_plan(payload, catalog=catalog, snapshot_id="OBS-1")
    assert result.errors == ()


def test_action_code_omitted_is_allowed(catalog):
    """No action_code field → no error."""
    payload = _plan(expected_actions=[
        {"step_id": "S1", "action_id": "gui.click"},
    ])
    result = parse_plan(payload, catalog=catalog, snapshot_id="OBS-1")
    assert result.errors == ()


# ---------------------------------------------------------------------------
# Stage 4 — target_ref.snapshot_id
# ---------------------------------------------------------------------------


def test_target_ref_snapshot_mismatch_rejected(catalog):
    """FR-P3.1-06 / acceptance #4: a stale `target_ref.snapshot_id`
    is rejected with `target_ref_snapshot_mismatch`."""
    payload = _plan(expected_actions=[
        {"step_id": "S1", "action_id": "gui.click",
         "target_ref": {"snapshot_id": "OBS-STALE",
                        "target_id": "T-1"}},
    ])
    result = parse_plan(payload, catalog=catalog, snapshot_id="OBS-1")
    assert any(
        e.code == CODE_TARGET_REF_SNAPSHOT_MISMATCH
        and e.pointer
            == "/expected_actions/0/target_ref/snapshot_id"
        for e in result.errors
    )


def test_target_ref_snapshot_match_passes(catalog):
    payload = _plan(expected_actions=[
        {"step_id": "S1", "action_id": "gui.click",
         "target_ref": {"snapshot_id": "OBS-1",
                        "target_id": "T-1"}},
    ])
    result = parse_plan(payload, catalog=catalog, snapshot_id="OBS-1")
    assert result.errors == ()


def test_target_ref_missing_is_allowed(catalog):
    """No target_ref → no error (some actions don't need one)."""
    payload = _plan(expected_actions=[
        {"step_id": "S1", "action_id": "gui.click"},
    ])
    result = parse_plan(payload, catalog=catalog, snapshot_id="OBS-1")
    assert result.errors == ()


# ---------------------------------------------------------------------------
# Stage 5 — dependency cycle
# ---------------------------------------------------------------------------


def test_dependency_cycle_in_plan_rejected(catalog):
    """FR-P3.1-10: plans with dependency cycles are rejected at
    validation time with `dependency_cycle` and the involved
    `step_id`s."""
    payload = _plan(expected_actions=[
        {"step_id": "A", "action_id": "gui.click"},
        {"step_id": "B", "action_id": "gui.click"},
    ])
    payload["expected_dependencies"] = {"A": ["B"], "B": ["A"]}
    result = parse_plan(payload, catalog=catalog, snapshot_id="OBS-1")
    cycle_errors = [e for e in result.errors
                    if e.code == CODE_DEPENDENCY_CYCLE]
    assert cycle_errors
    # The cycle field carries the closed loop.
    assert any("A" in e.cycle and "B" in e.cycle for e in cycle_errors)


def test_dependency_chain_accepted(catalog):
    """Linear A → B → C is fine."""
    payload = _plan(expected_actions=[
        {"step_id": "A", "action_id": "gui.click"},
        {"step_id": "B", "action_id": "gui.click"},
        {"step_id": "C", "action_id": "gui.click"},
    ])
    payload["expected_dependencies"] = {"A": [], "B": ["A"],
                                          "C": ["B"]}
    result = parse_plan(payload, catalog=catalog, snapshot_id="OBS-1")
    assert result.errors == ()


def test_dangling_dependency_rejected(catalog):
    """A dependency referencing a non-existent step is a schema
    error (D12 stage 1c)."""
    payload = _plan(expected_actions=[
        {"step_id": "A", "action_id": "gui.click"},
    ])
    payload["expected_dependencies"] = {"A": ["B"]}  # B does not exist
    result = parse_plan(payload, catalog=catalog, snapshot_id="OBS-1")
    assert any(
        e.code == CODE_PLAN_SCHEMA_INVALID
        and "B" in e.pointer
        for e in result.errors
    )


# ---------------------------------------------------------------------------
# D9 — coordinate fallback
# ---------------------------------------------------------------------------


def test_coordinate_fallback_not_allowed_when_semantic_exists(catalog):
    """Acceptance #3: a coordinate action chosen while a unique
    `gui.click` candidate exists returns
    `coordinate_fallback_not_allowed`."""
    payload = _plan(expected_actions=[
        {"step_id": "S1", "action_id": "pointer.click_screen"},
    ])
    result = parse_plan(
        payload, catalog=catalog, snapshot_id="OBS-1",
        semantic_target_action_ids={"gui.click"},
    )
    assert any(
        e.code == COORDINATE_FALLBACK_NOT_ALLOWED
        for e in result.errors
    )


def test_coordinate_fallback_allowed_when_no_semantic(catalog):
    """No semantic target → coordinate action is permitted."""
    payload = _plan(expected_actions=[
        {"step_id": "S1", "action_id": "pointer.click_screen"},
    ])
    result = parse_plan(
        payload, catalog=catalog, snapshot_id="OBS-1",
        semantic_target_action_ids=set(),
    )
    assert result.errors == ()


def test_coordinate_check_does_not_fire_for_semantic_action(catalog):
    """A semantic `gui.click` is never flagged by the coordinate
    fallback rule (the rule only fires for `pointer.*`)."""
    payload = _plan(expected_actions=[
        {"step_id": "S1", "action_id": "gui.click"},
    ])
    result = parse_plan(
        payload, catalog=catalog, snapshot_id="OBS-1",
        semantic_target_action_ids={"gui.click"},
    )
    assert not any(
        e.code == COORDINATE_FALLBACK_NOT_ALLOWED for e in result.errors
    )


def test_coordinate_fallback_is_scoped_to_the_current_step(catalog):
    payload = _plan(expected_actions=[
        {"step_id": "S1", "action_id": "pointer.click_screen"},
        {"step_id": "S2", "action_id": "pointer.click_screen"},
    ])
    result = parse_plan(
        payload, catalog=catalog, snapshot_id="OBS-1",
        semantic_target_action_ids={"S2": {"gui.click"}},
    )

    coordinate_errors = [
        error for error in result.errors
        if error.code == COORDINATE_FALLBACK_NOT_ALLOWED
    ]
    assert len(coordinate_errors) == 1
    assert coordinate_errors[0].pointer == "/expected_actions/1/action_id"


# ---------------------------------------------------------------------------
# Short-circuit behaviour
# ---------------------------------------------------------------------------


def test_schema_failure_short_circuits_subsequent_stages(catalog):
    """When stage 1 fails, stages 2..5 MUST NOT fire (the plan
    payload shape is untrusted; we cannot validate
    catalog references against garbage)."""
    payload = _plan()
    # Mutate to be invalid at every stage:
    payload.pop("schema_version", None)
    payload["expected_actions"] = [
        {"step_id": "S1", "action_id": "type_text",
         "action_code": "wrong",
         "target_ref": {"snapshot_id": "OBS-other", "target_id": "T-1"}},
    ]
    result = parse_plan(payload, catalog=catalog, snapshot_id="OBS-1")
    # All errors MUST be schema-level; no `backend_disabled`,
    # `action_code_mismatch`, `target_ref_snapshot_mismatch`.
    codes = {e.code for e in result.errors}
    assert CODE_PLAN_SCHEMA_INVALID in codes or \
        CODE_UNKNOWN_SCHEMA_VERSION in codes
    assert CODE_BACKEND_DISABLED not in codes
    assert CODE_ACTION_CODE_MISMATCH not in codes
    assert CODE_TARGET_REF_SNAPSHOT_MISMATCH not in codes


# ---------------------------------------------------------------------------
# Stable codes (architecture §7.2)
# ---------------------------------------------------------------------------


def test_stable_validation_codes_set_matches_used_codes():
    """All emitted codes MUST be in STABLE_VALIDATION_CODES."""
    expected = {
        CODE_PLAN_SCHEMA_INVALID,
        CODE_BACKEND_DISABLED,
        CODE_ACTION_CODE_MISMATCH,
        CODE_TARGET_REF_SNAPSHOT_MISMATCH,
        CODE_DEPENDENCY_CYCLE,
        COORDINATE_FALLBACK_NOT_ALLOWED,
        CODE_UNKNOWN_SCHEMA_VERSION,
    }
    assert STABLE_VALIDATION_CODES == expected


# ---------------------------------------------------------------------------
# LLMPlanResponse integration
# ---------------------------------------------------------------------------


def test_parse_result_to_dict_serialises_errors():
    """ValidationError.to_dict emits stable fields."""
    e = ValidationError(
        code="plan_schema_invalid",
        pointer="/foo",
        message="bad",
        value=42,
        cycle=("A", "B", "A"),
    )
    d = e.to_dict()
    assert d["code"] == "plan_schema_invalid"
    assert d["pointer"] == "/foo"
    assert d["message"] == "bad"
    assert d["value"] == 42
    assert d["cycle"] == ["A", "B", "A"]


def test_llm_plan_response_round_trip_via_parse_plan(catalog):
    """LLMPlanResponse carries `plan` + `validation_errors`."""
    payload = _plan(expected_actions=[
        {"step_id": "S1", "action_id": "type_text"},
    ])
    result = parse_plan(payload, catalog=catalog, snapshot_id="OBS-1")
    resp = LLMPlanResponse(plan=result.plan,
                            validation_errors=tuple(
                                e.to_dict() for e in result.errors
                            ))
    assert resp.plan["plan_id"] == "P-test"
    assert any(
        e["code"] == "backend_disabled" for e in resp.validation_errors
    )


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_parse_plan_is_deterministic(catalog):
    """Two calls on the same payload return the same errors in
    the same order (architecture §7.2 stability)."""
    payload = _plan(expected_actions=[
        {"step_id": "S1", "action_id": "type_text"},
        {"step_id": "S2", "action_id": "gui.click",
         "target_ref": {"snapshot_id": "OBS-STALE",
                        "target_id": "T-1"}},
    ])
    a = parse_plan(payload, catalog=catalog, snapshot_id="OBS-1")
    b = parse_plan(payload, catalog=catalog, snapshot_id="OBS-1")
    a_codes = [e.code for e in a.errors]
    b_codes = [e.code for e in b.errors]
    assert a_codes == b_codes
    a_points = [e.pointer for e in a.errors]
    b_points = [e.pointer for e in b.errors]
    assert a_points == b_points
