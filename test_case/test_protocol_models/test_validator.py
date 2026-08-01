"""
P0.2 acceptance gate — validator + dependency DAG (architecture §10
steps 4-11, FR-P0.2-03, -04, -06, -07, -09).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


_REPO = Path(__file__).resolve().parents[2]
_TARGET = _REPO / "target"
_FIXTURES = _REPO / "test_case" / "fixtures" / "protocol"

if str(_TARGET) not in sys.path:
    sys.path.insert(0, str(_TARGET))


from action_catalog import catalog_digest  # noqa: E402
import protocol_models as pm  # noqa: E402


V1_DIGEST = catalog_digest()


def _load_fixture(name: str) -> dict:
    return json.loads((_FIXTURES / name).read_text())


# ---------------------------------------------------------------------------
# FR-P0.2-03 / §10 — Stable codes + JSON paths
# ---------------------------------------------------------------------------


def test_positive_plan_validates_clean():
    payload = _load_fixture("positive_plan.json")
    plan = pm.ActionSequence.from_dict(payload)
    errors = pm.validate_plan(plan)
    assert errors == [], f"unexpected errors: {errors}"


def test_unknown_action_id_returns_stable_code_and_path():
    payload = _load_fixture("negative_unknown_action.json")
    plan = pm.ActionSequence.from_dict(payload)
    errors = pm.validate_plan(plan)
    assert len(errors) >= 1
    bad = [e for e in errors if e.code == "unknown_action_id"]
    assert len(bad) == 1
    assert bad[0].path == "steps[1].action_id"
    assert bad[0].value == "gui.press"


def test_unknown_field_from_model_translates_to_unknown_field_code():
    """Strict-mode rejection at the model layer is mapped through
    `re_raise_protocol_model_error` so callers see the stable
    `unknown_field` code at the validator surface."""
    payload = {
        "plan_id": "PLAN-X",
        "catalog_version": "1.0.1",
        "catalog_digest": V1_DIGEST,
        "target": "t",
        "steps": [],
        "secret_extra": "boom",
    }
    try:
        pm.ActionSequence.from_dict(payload)
    except pm.ProtocolModelError as exc:
        ve = pm.re_raise_protocol_model_error(exc)
        assert ve.code == pm.CODE_UNKNOWN_FIELD
        assert ve.path == "ActionSequence"


def test_action_code_mismatch_returns_stable_code():
    payload = _load_fixture("positive_plan.json")
    payload = json.loads(json.dumps(payload))
    payload["steps"][1]["action_code"] = "A030"  # wrong for window_lock.set (A002)
    plan = pm.ActionSequence.from_dict(payload)
    errors = pm.validate_plan(plan)
    bad = [e for e in errors if e.code == "action_code_mismatch"]
    assert len(bad) == 1
    assert bad[0].path == "steps[1].action_code"
    assert bad[0].value == "A030"


def test_unknown_action_code_returns_stable_code():
    """When the action_id is known but the action_code is not in the
    catalog's code map, the validator reports `unknown_action_code`."""
    payload = _load_fixture("positive_plan.json")
    payload = json.loads(json.dumps(payload))
    payload["steps"][1]["action_code"] = "A999"  # not registered anywhere
    plan = pm.ActionSequence.from_dict(payload)
    errors = pm.validate_plan(plan)
    bad = [e for e in errors if e.code == "unknown_action_code"]
    assert len(bad) == 1
    assert bad[0].path == "steps[1].action_code"
    assert bad[0].value == "A999"


def test_action_code_mismatch_with_known_action_id():
    """When action_id is in the catalog but the supplied action_code
    does not match the catalog's record for that id, the validator
    reports `action_code_mismatch`."""
    payload = _load_fixture("positive_plan.json")
    payload = json.loads(json.dumps(payload))
    payload["steps"][1]["action_code"] = "A030"  # valid code, but for gui.type_text
    plan = pm.ActionSequence.from_dict(payload)
    errors = pm.validate_plan(plan)
    bad = [e for e in errors if e.code == "action_code_mismatch"]
    assert len(bad) == 1
    assert bad[0].path == "steps[1].action_code"


def test_unknown_expectation_type_returns_stable_code():
    payload = _load_fixture("positive_plan.json")
    payload = json.loads(json.dumps(payload))
    payload["steps"][2]["expectations"][0]["type"] = "no_such_expectation"
    # Construction itself rejects (Expectation.__post_init__):
    with pytest.raises(pm.ProtocolModelError) as exc:
        pm.ActionSequence.from_dict(payload)
    assert exc.value.code == "enum_error"


# ---------------------------------------------------------------------------
# FR-P0.2-06 — schema_version_mismatch
# ---------------------------------------------------------------------------


def test_old_schema_version_rejected():
    payload = _load_fixture("positive_plan.json")
    payload = json.loads(json.dumps(payload))
    payload["catalog_version"] = "0.9.0"
    plan = pm.ActionSequence.from_dict(payload)
    errors = pm.validate_plan(plan)
    bad = [e for e in errors if e.code == "schema_version_mismatch"]
    assert len(bad) == 1
    assert bad[0].path == "schema_version"
    assert bad[0].value == "0.9.0"


def test_newer_schema_version_accepted():
    payload = _load_fixture("positive_plan.json")
    payload = json.loads(json.dumps(payload))
    payload["catalog_version"] = "2.0.0"
    plan = pm.ActionSequence.from_dict(payload)
    errors = pm.validate_plan(plan)
    schema_errs = [e for e in errors if e.code == "schema_version_mismatch"]
    assert schema_errs == []


# ---------------------------------------------------------------------------
# FR-P0.2-07 — catalog_digest_mismatch
# ---------------------------------------------------------------------------


def test_catalog_digest_mismatch_returns_stable_code():
    payload = _load_fixture("positive_plan.json")
    payload = json.loads(json.dumps(payload))
    payload["catalog_digest"] = "sha256:" + "0" * 64
    plan = pm.ActionSequence.from_dict(payload)
    errors = pm.validate_plan(plan)
    bad = [e for e in errors if e.code == "catalog_digest_mismatch"]
    assert len(bad) == 1
    assert bad[0].path == "catalog_digest"


# ---------------------------------------------------------------------------
# FR-P0.2-04 — Dependency DAG: cycle detection + diamond accepted
# ---------------------------------------------------------------------------


def test_three_node_cycle_detected():
    payload = _load_fixture("negative_cycle.json")
    plan = pm.ActionSequence.from_dict(payload)
    errors = pm.validate_plan(plan)
    cycles = [e for e in errors if e.code == "dependency_cycle"]
    assert len(cycles) >= 1
    # The cycle payload reports the offending node path
    cycle = cycles[0].value
    assert cycle[0] == cycle[-1]  # closed loop


def test_diamond_dependency_accepted():
    payload = _load_fixture("positive_diamond.json")
    plan = pm.ActionSequence.from_dict(payload)
    errors = pm.validate_plan(plan)
    cycles = [e for e in errors if e.code == "dependency_cycle"]
    assert cycles == []
    # And no other structural errors
    structural = [e for e in errors if e.code != "schema_version_mismatch"
                  and e.code != "catalog_digest_mismatch"]
    assert structural == []


def test_four_node_cycle_detected():
    payload = _load_fixture("positive_plan.json")
    payload = json.loads(json.dumps(payload))
    # add a fourth step that closes a 4-cycle
    payload["steps"].append({
        "step_id": "S004",
        "action_id": "session.window_lock.verify",
        "action_code": "A005",
        "depends_on": ["S003"],
        "on_error": "abort",
    })
    # close the cycle: S001 depends on S004
    payload["steps"][0]["depends_on"] = ["S004"]
    plan = pm.ActionSequence.from_dict(payload)
    errors = pm.validate_plan(plan)
    cycles = [e for e in errors if e.code == "dependency_cycle"]
    assert len(cycles) >= 1


def test_duplicate_step_id_rejected():
    payload = _load_fixture("positive_plan.json")
    payload = json.loads(json.dumps(payload))
    payload["steps"][1]["step_id"] = "S001"  # dup of steps[0]
    plan = pm.ActionSequence.from_dict(payload)
    errors = pm.validate_plan(plan)
    bad = [e for e in errors if e.code == "duplicate_step_id"]
    assert len(bad) == 1
    assert bad[0].value == "S001"


def test_missing_dependency_rejected():
    payload = _load_fixture("positive_plan.json")
    payload = json.loads(json.dumps(payload))
    payload["steps"][2]["depends_on"] = ["S999"]
    plan = pm.ActionSequence.from_dict(payload)
    errors = pm.validate_plan(plan)
    bad = [e for e in errors if e.code == "missing_dependency"]
    assert len(bad) == 1
    assert bad[0].value == "S999"


# ---------------------------------------------------------------------------
# FR-P0.2-08 — invalid args surface (when input schema rejects)
# ---------------------------------------------------------------------------


def test_invalid_args_for_dict_typed_field():
    """ActionStep.args must be a Mapping; passing a string fails
    construction with a stable type_error code."""
    with pytest.raises(pm.ProtocolModelError) as exc:
        pm.ActionStep(step_id="S1", action_id="session.connect",
                       args="not a mapping")
    assert exc.value.code == "type_error"


# ---------------------------------------------------------------------------
# Stable-code audit
# ---------------------------------------------------------------------------


def test_all_validation_codes_have_stable_string_identities():
    """Regression guard: the canonical code set is frozen so
    cross-checkpoint consumers can rely on these strings."""
    expected = {
        "unknown_action_id", "unknown_action_code", "action_code_mismatch",
        "schema_version_mismatch", "catalog_digest_mismatch",
        "duplicate_step_id", "missing_dependency", "dependency_cycle",
        "backend_disabled", "invalid_args", "unknown_expectation_type",
        "unknown_field", "invalid_target_ref",
    }
    actual = set(pm.ARCHITECTURE_P0_2_VALIDATION_CODES)
    assert actual == expected


def test_validate_case_step_no_uniqueness():
    """TestCase validator reports duplicate step_no across the case."""
    case_payload = {
        "case_id": "TC-1",
        "title": "T",
        "preconditions": [],
        "steps": [
            {"step_id": "S1", "step_no": 1, "action_id": "session.connect",
             "action_code": "A001", "on_error": "abort"},
            {"step_id": "S2", "step_no": 1, "action_id": "session.window_lock.set",
             "action_code": "A002", "on_error": "abort"},
        ],
        "cleanup": [],
    }
    case = pm.TestCase.from_dict(case_payload)
    errors = pm.validate_case(case)
    bad = [e for e in errors if e.code == "duplicate_step_id"]
    assert len(bad) == 1
    assert bad[0].value == 1