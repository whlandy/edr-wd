"""P3.1 acceptance gate — Commit B: schema + LLMPlanRequest/Response."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


_REPO = Path(__file__).resolve().parents[2]
for p in (str(_REPO / "target"), str(_REPO / "agent")):
    if p not in sys.path:
        sys.path.insert(0, p)


from planner.schema import (  # noqa: E402
    LLMPlanRequest,
    LLMPlanResponse,
    PLAN_JSON_SCHEMA,
    SCHEMA_VERSION,
    export_schema,
    known_versions,
)


# ---------------------------------------------------------------------------
# FR-P3.1-02 — strict JSON Schema
# ---------------------------------------------------------------------------


def test_schema_version_field_present_and_locked():
    """D11: schema_version is required + const for V1."""
    props = PLAN_JSON_SCHEMA["properties"]
    assert "schema_version" in props
    assert props["schema_version"]["type"] == "string"
    assert props["schema_version"]["const"] == "plan_schema.v1"
    assert "schema_version" in PLAN_JSON_SCHEMA["required"]


def test_schema_top_level_additional_properties_false():
    assert PLAN_JSON_SCHEMA["additionalProperties"] is False


def test_schema_nested_objects_all_have_additional_properties_false():
    """Every nested $defs object MUST also lock
    `additionalProperties: false` (FR-P3.1-02)."""
    for def_name, def_schema in PLAN_JSON_SCHEMA["$defs"].items():
        assert def_schema.get("additionalProperties") is False, (
            f"{def_name} lacks additionalProperties: false"
        )


def test_schema_required_top_level():
    expected = {"schema_version", "plan_id", "snapshot_id",
                "expected_actions"}
    assert set(PLAN_JSON_SCHEMA["required"]) == expected


def test_schema_action_step_requires_step_id_and_action_id():
    step_schema = PLAN_JSON_SCHEMA["$defs"]["ActionStep"]
    assert set(step_schema["required"]) == {"step_id", "action_id"}
    assert step_schema["properties"]["step_id"]["minLength"] == 1


def test_schema_target_ref_additional_properties_false():
    target = PLAN_JSON_SCHEMA["$defs"]["TargetRef"]
    assert target["additionalProperties"] is False


def test_schema_transition_enum_matches_p2_1():
    expected = {
        "none", "control_state_change", "page_navigation",
        "modal_open", "modal_close", "window_open", "window_close",
        "window_owner_change", "application_restart",
        "unknown_material_change",
    }
    assert set(PLAN_JSON_SCHEMA["$defs"]["Transition"]
               ["properties"]["expected"]["enum"]) == expected


def test_schema_expectation_enum_matches_p1_4():
    expected = {
        "action_ok", "window_open", "window_closed",
        "active_window_owner", "control_exists", "control_absent",
        "control_text_equals", "control_text_contains",
        "control_value_equals", "control_checked_equals",
        "control_enabled_equals",
        "window_text_contains", "control_text_contains_time",
        "window_text_contains_time",
        "visual_evidence_captured",
    }
    assert set(PLAN_JSON_SCHEMA["$defs"]["Expectation"]
               ["properties"]["type"]["enum"]) == expected


# ---------------------------------------------------------------------------
# D11 schema_version migration contract
# ---------------------------------------------------------------------------


def test_known_versions_includes_current():
    assert SCHEMA_VERSION in known_versions()


def test_known_versions_frozen():
    """The set of known versions is a frozenset (immutable)."""
    assert isinstance(known_versions(), frozenset)


def test_schema_version_const_is_a_string():
    """Future consumers can use this to validate a payload
    before parsing."""
    assert isinstance(SCHEMA_VERSION, str)
    assert "." in SCHEMA_VERSION  # "plan_schema.v1"


# ---------------------------------------------------------------------------
# LLMPlanRequest shape
# ---------------------------------------------------------------------------


def test_llm_plan_request_required_fields():
    req = LLMPlanRequest(
        snapshot_id="OBS-1", target="EDRClient",
        profile="windows_hisec",
        allowed_actions=("gui.click",),
    )
    assert req.snapshot_id == "OBS-1"
    assert req.target == "EDRClient"
    assert req.profile == "windows_hisec"
    assert req.allowed_actions == ("gui.click",)


def test_llm_plan_request_defaults():
    req = LLMPlanRequest(
        snapshot_id="OBS", target="x", profile="x",
        allowed_actions=(),
    )
    # Default schema_ref points at the on-disk export.
    assert req.schema_ref == "test_case/schema/plan.schema.json"
    assert req.hints == {}


def test_llm_plan_request_immutable():
    req = LLMPlanRequest(
        snapshot_id="OBS", target="x", profile="x",
        allowed_actions=("gui.click",),
    )
    with pytest.raises(Exception):  # FrozenInstanceError or AttributeError
        req.snapshot_id = "OBS-other"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# LLMPlanResponse shape
# ---------------------------------------------------------------------------


def test_llm_plan_response_defaults():
    resp = LLMPlanResponse(plan={"plan_id": "P-1"})
    assert resp.plan == {"plan_id": "P-1"}
    assert resp.validation_errors == ()


def test_llm_plan_response_with_errors():
    resp = LLMPlanResponse(
        plan={"plan_id": "P-2"},
        validation_errors=(
            {"code": "plan_schema_invalid", "pointer": "/foo"},
        ),
    )
    assert len(resp.validation_errors) == 1
    assert resp.validation_errors[0]["code"] == "plan_schema_invalid"


# ---------------------------------------------------------------------------
# Export to test_case/schema/plan.schema.json
# ---------------------------------------------------------------------------


def test_export_writes_file_under_test_case_schema(tmp_path: Path):
    """Calling export() MUST write a JSON file under
    `test_case/schema/plan.schema.json`."""
    # tmp_path is unused — we only check that the on-disk
    # location exists after export.
    target = export_schema()
    assert target.exists()
    # Round-trip: parse the file as JSON, compare.
    on_disk = json.loads(target.read_text(encoding="utf-8"))
    assert on_disk == PLAN_JSON_SCHEMA


def test_exported_schema_is_valid_json():
    target = export_schema()
    text = target.read_text(encoding="utf-8")
    # Must parse.
    json.loads(text)
    # Must be pretty-printed (indent=2).
    assert "\n  " in text


def test_exported_schema_has_required_top_level_keys():
    target = export_schema()
    on_disk = json.loads(target.read_text(encoding="utf-8"))
    assert on_disk["type"] == "object"
    assert on_disk["additionalProperties"] is False
    assert "schema_version" in on_disk["required"]


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_export_is_deterministic():
    """Two consecutive exports byte-match (sort_keys=True)."""
    a = export_schema().read_text(encoding="utf-8")
    b = export_schema().read_text(encoding="utf-8")
    assert a == b


# ---------------------------------------------------------------------------
# Schema is a valid JSON Schema draft 2020-12 surface (best-effort)
# ---------------------------------------------------------------------------


def test_schema_meta_keys_present():
    assert "$schema" in PLAN_JSON_SCHEMA
    assert "$id" in PLAN_JSON_SCHEMA
    assert "$defs" in PLAN_JSON_SCHEMA
    assert PLAN_JSON_SCHEMA["$schema"].startswith("https://json-schema.org/draft/")


def test_schema_has_title_and_description():
    assert "title" in PLAN_JSON_SCHEMA
    assert "description" in PLAN_JSON_SCHEMA
