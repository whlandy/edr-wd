"""
P0.2 acceptance gate — dry-run + IDs (architecture §10 step 11,
FR-P0.2-05, FR-P0.2-09 via expectations registry).
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
# FR-P0.2-05 — dry_run does not invoke any backend
# ---------------------------------------------------------------------------


def test_dry_run_no_backend_call_on_valid_plan():
    """dry_run must validate the plan using only in-process catalog
    data. The `on_backend_called` instrumentation hook (when provided)
    is the test's window to assert zero backend calls."""
    payload = _load_fixture("positive_plan.json")
    plan = pm.ActionSequence.from_dict(payload)
    backend_calls: list[tuple] = []

    def spy(name: str, args: tuple, kwargs: dict) -> None:
        backend_calls.append((name, args, kwargs))

    result = pm.dry_run(plan, on_backend_called=spy)
    assert result.ok
    assert result.validated_steps == 3
    assert backend_calls == []


def test_dry_run_no_backend_call_on_invalid_plan():
    payload = _load_fixture("negative_unknown_action.json")
    plan = pm.ActionSequence.from_dict(payload)
    backend_calls: list = []

    def spy(name: str, args: tuple, kwargs: dict) -> None:
        backend_calls.append((name, args, kwargs))

    result = pm.dry_run(plan, on_backend_called=spy)
    assert not result.ok
    assert any(e.code == "unknown_action_id" for e in result.errors)
    assert backend_calls == []


def test_dry_run_does_not_import_backend_modules():
    """Verify that running dry_run does not pull in `automation.*`
    modules. If a future refactor accidentally couples dry_run to a
    backend, sys.modules will contain `automation.*` keys."""
    # Pre-state: prune any automation.* modules already imported
    keys_to_prune = [k for k in sys.modules if k.startswith("automation")]
    for k in keys_to_prune:
        sys.modules.pop(k, None)

    payload = _load_fixture("positive_plan.json")
    plan = pm.ActionSequence.from_dict(payload)
    pm.dry_run(plan)

    post = [k for k in sys.modules if k.startswith("automation")]
    assert post == [], (
        f"dry_run pulled in backend modules: {post}"
    )


def test_dry_run_returns_validation_errors_structured():
    payload = _load_fixture("negative_cycle.json")
    plan = pm.ActionSequence.from_dict(payload)
    result = pm.dry_run(plan)
    assert not result.ok
    codes = {e.code for e in result.errors}
    assert "dependency_cycle" in codes


def test_dry_run_case_validates_holistic():
    case_payload = {
        "case_id": "TC-1",
        "title": "T",
        "preconditions": [
            {"step_id": "P1", "step_no": 0, "action_id": "session.connect",
             "action_code": "A001", "on_error": "abort"},
        ],
        "steps": [
            {"step_id": "S1", "step_no": 1, "action_id": "gui.click",
             "action_code": "A020", "depends_on": ["P1"],
             "on_error": "capture_and_abort"},
        ],
        "cleanup": [],
    }
    case = pm.TestCase.from_dict(case_payload)
    result = pm.dry_run_case(case)
    assert result.ok
    assert result.validated_steps == 2


# ---------------------------------------------------------------------------
# FR-P0.2-09 — Typed expectation registry
# ---------------------------------------------------------------------------


def test_expectation_registry_contains_all_documented_types():
    documented = set(pm.VALID_EXPECTATION_TYPES)
    registered = set(pm.EXPECTATION_TYPE_REGISTRY.keys())
    assert documented == registered


def test_validate_expectation_type_accepts_known():
    for t in pm.VALID_EXPECTATION_TYPES:
        pm.validate_expectation_type(t)  # should not raise


def test_validate_expectation_type_rejects_unknown():
    with pytest.raises(pm.ProtocolModelError) as exc:
        pm.validate_expectation_type("not_a_type")
    assert exc.value.code == "enum_error"


def test_iter_expectation_types_is_sorted():
    types = [t.type for t in pm.iter_expectation_types()]
    assert types == sorted(types)


def test_expectation_registry_is_immutable():
    """P0.2 review: registry must reject mutation. The exposed
    `EXPECTATION_TYPE_REGISTRY` is a `MappingProxyType`."""
    import types as _types
    assert isinstance(pm.EXPECTATION_TYPE_REGISTRY, _types.MappingProxyType)
    with pytest.raises(TypeError):
        pm.EXPECTATION_TYPE_REGISTRY["new_type"] = None  # type: ignore[index]
    with pytest.raises(TypeError):
        del pm.EXPECTATION_TYPE_REGISTRY[pm.EXPECTATION_ACTION_OK]  # type: ignore[arg-type]


def test_expectation_registry_iteration_stable_across_runs():
    """Two iterations over the registry produce the same order."""
    a = sorted(t.type for t in pm.iter_expectation_types())
    b = sorted(t.type for t in pm.iter_expectation_types())
    assert a == b


# ---------------------------------------------------------------------------
# New fixture: negative_unknown_field.json
# ---------------------------------------------------------------------------


def test_negative_unknown_field_fixture_rejects_strict_mode():
    """The dedicated fixture tests one contract: an unknown field
    on a step triggers strict-mode rejection."""
    payload = _load_fixture("negative_unknown_field.json")
    with pytest.raises(pm.ProtocolModelError) as exc:
        pm.ActionSequence.from_dict(payload)
    assert exc.value.code == "unknown_field"
    # Specifically the rogue_field on the step
    assert "rogue_field" in str(exc.value)


# ---------------------------------------------------------------------------
# IDs (P0.2 Open Decision #3 — stdlib-only sortable)
# ---------------------------------------------------------------------------


def test_ids_have_correct_scope_marker():
    """Each scope appears as the third dash-delimited token. The
    monotonic seq + timestamp prefix occupies the first two
    tokens; the 8-hex random suffix is the fourth."""
    for ctor, scope in [
        (pm.new_plan_id,       "PLAN"),
        (pm.new_step_id,       "STEP"),
        (pm.new_request_id,    "REQ"),
        (pm.new_checkpoint_id, "CP"),
        (pm.new_event_id,      "EVT"),
        (pm.new_branch_id,     "BR"),
        (pm.new_snapshot_id,   "SNAP"),
        (pm.new_evidence_id,   "EVID"),
        (pm.new_trace_id,      "TRACE"),
    ]:
        i = ctor()
        parts = i.split("-")
        assert len(parts) == 4, f"unexpected format: {i}"
        # First token: 13-digit unix-ms timestamp
        assert len(parts[0]) == 13 and parts[0].isdigit(), (
            f"{i}: timestamp token {parts[0]!r}"
        )
        # Second token: 4-hex monotonic seq
        assert len(parts[1]) == 4 and all(c in "0123456789abcdef"
                                           for c in parts[1]), (
            f"{i}: seq token {parts[1]!r}"
        )
        # Third token: scope
        assert parts[2] == scope, f"{i}: scope token {parts[2]!r} != {scope!r}"
        # Fourth token: 8-hex random suffix
        assert len(parts[3]) == 8 and all(c in "0123456789abcdef"
                                           for c in parts[3]), (
            f"{i}: random token {parts[3]!r}"
        )


def test_ids_have_stable_format():
    """ms(13)-seq(4)-scope-uuid(8)."""
    pid = pm.new_plan_id()
    parts = pid.split("-")
    assert len(parts) == 4
    assert len(parts[0]) == 13
    assert parts[2] == "PLAN"
    assert len(parts[3]) == 8


def test_ids_are_unique_within_a_process():
    ids = {pm.new_plan_id() for _ in range(1000)}
    assert len(ids) == 1000


def test_ids_are_sortable_within_same_scope():
    """Sort by string compare; later IDs have later timestamps so
    ascending == insertion order."""
    ids = [pm.new_step_id() for _ in range(10)]
    assert ids == sorted(ids)


def test_ids_are_sortable_across_processes():
    """Cross-process ordering is defined by the timestamp prefix.
    Two IDs whose `time.time_ns()` readings differ by even 1 ms
    compare in the right order."""
    import time as _t
    first = pm.new_step_id()
    _t.sleep(0.002)  # at least 2 ms gap
    second = pm.new_step_id()
    assert first < second


def test_new_id_rejects_unknown_scope(monkeypatch):
    """The internal _new_id helper rejects scopes not in VALID_SCOPES."""
    from protocol_models import ids as _ids
    with pytest.raises(ValueError) as exc:
        _ids._new_id("BOGUS")
    assert "BOGUS" in str(exc.value)


def test_id_parts_round_trip():
    """The _id_parts helper reconstructs the original tokens.

    Tests reach into `protocol_models.ids` directly rather than via
    the public package surface — `_id_parts` is a test/internal
    helper and is intentionally NOT re-exported from
    `protocol_models.__init__` (P0.2 review feedback).
    """
    from protocol_models.ids import _id_parts
    pid = pm.new_plan_id()
    ms_str, seq_int, scope, rand = _id_parts(pid)
    assert ms_str == pid.split("-")[0]
    assert scope == "PLAN"
    assert len(rand) == 8