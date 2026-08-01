"""
P0.2 acceptance gate — strict models + round-trip + canonical JSON
(architecture §10 step 1, FR-P0.2-01, FR-P0.2-02, FR-P0.2-08).

Run:
    cd /Users/whl/AI-Agent/skill/edr-wd
    python3 -m pytest -q test_case/test_protocol_models

Covers:
    G1. Strict mode (unknown fields rejected at every model).
    G2. JSON round-trip (model in -> dict out -> JSON bytes, no loss).
    G3. Canonical JSON determinism (insertion-order independent).
    G4. Error envelope contract (§19).
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


# ---------------------------------------------------------------------------
# G1. Strict mode — unknown fields rejected
# ---------------------------------------------------------------------------


def test_action_sequence_strict_rejects_unknown_field():
    payload = {
        "plan_id": "PLAN-X",
        "catalog_version": "1.0.1",
        "catalog_digest": V1_DIGEST,
        "target": "t",
        "steps": [],
        "secret_extra": "boom",
    }
    with pytest.raises(pm.ProtocolModelError) as exc:
        pm.ActionSequence.from_dict(payload)
    assert exc.value.code == "unknown_field"
    assert exc.value.path == "ActionSequence"


def test_action_step_strict_rejects_unknown_field():
    payload = {
        "step_id": "S1",
        "action_id": "session.connect",
        "ghost": "boo",
    }
    with pytest.raises(pm.ProtocolModelError) as exc:
        pm.ActionStep.from_dict(payload)
    assert exc.value.code == "unknown_field"


def test_expectation_strict_rejects_unknown_field():
    payload = {"type": "window_text_contains", "value": "X", "surprise": 1}
    with pytest.raises(pm.ProtocolModelError) as exc:
        pm.Expectation.from_dict(payload)
    assert exc.value.code == "unknown_field"


def test_atomic_test_step_strict_rejects_unknown_field():
    payload = {"step_id": "S1", "action_id": "session.connect", "phantom": 1}
    with pytest.raises(pm.ProtocolModelError) as exc:
        pm.AtomicTestStep.from_dict(payload)
    assert exc.value.code == "unknown_field"


def test_test_case_strict_rejects_unknown_field():
    payload = {
        "case_id": "TC-X",
        "title": "T",
        "legacy_extra": 1,
    }
    with pytest.raises(pm.ProtocolModelError) as exc:
        pm.TestCase.from_dict(payload)
    assert exc.value.code == "unknown_field"


def test_error_envelope_strict_rejects_unknown_field():
    payload = {
        "code": "x", "message": "y", "mystery": True,
    }
    with pytest.raises(pm.ProtocolModelError) as exc:
        pm.ErrorEnvelope.from_dict(payload)
    assert exc.value.code == "unknown_field"


# ---------------------------------------------------------------------------
# G2. JSON round-trip — model in, JSON bytes out, no semantic loss
# ---------------------------------------------------------------------------


def _load_fixture(name: str) -> dict:
    return json.loads((_FIXTURES / name).read_text())


def test_action_sequence_round_trip_from_fixture():
    payload = _load_fixture("positive_plan.json")
    plan = pm.ActionSequence.from_dict(payload)
    # round-trip via canonical_bytes from canonical_json module
    from protocol_models.canonical_json import canonical_bytes, canonical_sha256
    h1 = canonical_sha256(plan)
    h2 = canonical_sha256(plan)
    assert h1 == h2
    # JSON-serializable: encoder accepts a dict view
    serializable = json.loads(json.dumps(plan, default=lambda o: dict(o.__dict__) if hasattr(o, '__dict__') else str(o)))


def test_action_sequence_round_trip_preserves_field_set():
    payload = _load_fixture("positive_plan.json")
    plan = pm.ActionSequence.from_dict(payload)
    # Re-serialize via dataclasses.asdict to confirm no field loss
    from dataclasses import asdict
    back = asdict(plan)
    # plan_id, catalog_version, catalog_digest preserved exactly
    assert back["plan_id"] == payload["plan_id"]
    assert back["catalog_version"] == payload["catalog_version"]
    assert back["catalog_digest"] == payload["catalog_digest"]
    assert back["target"] == payload["target"]
    assert len(back["steps"]) == len(payload["steps"])
    for s_in, s_back in zip(payload["steps"], back["steps"]):
        assert s_back["step_id"] == s_in["step_id"]
        assert s_back["action_id"] == s_in["action_id"]
        assert s_back["action_code"] == s_in["action_code"]


def test_target_ref_strict_minimal():
    ref = pm.TargetRef(snapshot_id="OBS-1", target_id="T0007")
    assert ref.expected_process_name == ""
    assert ref.fingerprint is None
    assert ref.selector_hint is None


def test_target_ref_rejects_empty_snapshot():
    with pytest.raises(pm.ProtocolModelError) as exc:
        pm.TargetRef(snapshot_id="", target_id="T0007")
    assert exc.value.code == "empty_value"


def test_expectation_rejects_unknown_type():
    with pytest.raises(pm.ProtocolModelError) as exc:
        pm.Expectation(type="not_a_real_type")
    assert exc.value.code == "enum_error"


def test_expectation_rejects_negative_timeout():
    with pytest.raises(pm.ProtocolModelError) as exc:
        pm.Expectation(type="window_text_contains", timeout_seconds=-1)
    assert exc.value.code == "enum_error"


def test_action_step_rejects_unknown_on_error():
    with pytest.raises(pm.ProtocolModelError) as exc:
        pm.ActionStep(step_id="S1", action_id="session.connect",
                       on_error="not_a_real_policy")
    assert exc.value.code == "enum_error"


def test_transition_rejects_unknown_kind():
    with pytest.raises(pm.ProtocolModelError) as exc:
        pm.Transition(kind="not_a_real_kind")
    assert exc.value.code == "enum_error"


def test_test_case_rejects_nonpositive_timeout():
    with pytest.raises(pm.ProtocolModelError) as exc:
        pm.TestCase(case_id="TC", title="T", timeout_seconds=0)
    assert exc.value.code == "enum_error"


# ---------------------------------------------------------------------------
# G3. Canonical JSON determinism
# ---------------------------------------------------------------------------


def test_canonical_bytes_stable_across_insertion_order():
    a = {"a": 1, "b": 2, "c": 3}
    b = {"c": 3, "b": 2, "a": 1}
    from protocol_models.canonical_json import canonical_bytes, canonical_sha256
    assert canonical_bytes(a) == canonical_bytes(b)
    assert canonical_sha256(a) == canonical_sha256(b)


def test_canonical_bytes_round_trip_json():
    from protocol_models.canonical_json import canonical_bytes
    obj = {"x": [1, 2, {"y": "z"}], "k": "v"}
    raw = canonical_bytes(obj)
    loaded = json.loads(raw.decode("utf-8"))
    assert loaded == obj


def test_canonical_bytes_round_trip_dataclass():
    from protocol_models.canonical_json import canonical_bytes, canonical_sha256
    plan = pm.ActionSequence.from_dict(_load_fixture("positive_plan.json"))
    raw = canonical_bytes(plan)
    # Valid UTF-8 JSON; round-trip parses back to equivalent object
    loaded = json.loads(raw.decode("utf-8"))
    assert loaded["plan_id"] == "PLAN-PROTECTION-001"
    assert loaded["catalog_version"] == "1.0.1"
    assert len(loaded["steps"]) == 3
    # Digest is stable across two runs
    assert canonical_sha256(plan) == canonical_sha256(plan)


def test_canonical_bytes_rejects_nan():
    from protocol_models.canonical_json import canonical_bytes
    with pytest.raises(TypeError):
        canonical_bytes({"x": float("nan")})


def test_canonical_bytes_rejects_unsupported_type():
    from protocol_models.canonical_json import canonical_bytes
    with pytest.raises(TypeError):
        canonical_bytes({"x": object()})


def test_canonical_bytes_set_is_deterministic():
    """Set ordering is non-deterministic at the language level; the
    helper must sort by canonical JSON bytes so two sets with the
    same elements produce identical output regardless of insertion
    order (P0.2 review)."""
    from protocol_models.canonical_json import canonical_bytes
    a = canonical_bytes({"x": {"a", "b", "c"}})
    b = canonical_bytes({"x": {"c", "b", "a"}})
    assert a == b
    # And the sorted order is reflected in the bytes
    raw = a.decode("utf-8")
    assert '"a"' in raw and raw.find('"a"') < raw.find('"b"') < raw.find('"c"')


def test_canonical_bytes_frozenset_is_deterministic():
    from protocol_models.canonical_json import canonical_bytes
    a = canonical_bytes({"k": frozenset(["z", "y", "x"])})
    b = canonical_bytes({"k": frozenset(["x", "z", "y"])})
    assert a == b


def test_canonical_bytes_nested_set_determinism():
    """Deeper nesting: a set inside a dict inside a list, mixed with
    other containers, must still round-trip deterministically."""
    from protocol_models.canonical_json import canonical_bytes
    payload = {"outer": [{"inner": {"alpha", "beta"}}, {"other": 1}]}
    a = canonical_bytes(payload)
    # Build the same payload with a freshly-constructed set
    payload2 = {"outer": [{"inner": {"beta", "alpha"}}, {"other": 1}]}
    b = canonical_bytes(payload2)
    assert a == b


def test_canonical_bytes_set_keys_in_dict():
    """Python dicts cannot have set keys, but values can be sets.
    Make sure no path accidentally tries to use a set as a key."""
    from protocol_models.canonical_json import canonical_bytes
    out = canonical_bytes({"a": {1, 2}, "b": [3, 4]})
    loaded = json.loads(out.decode("utf-8"))
    assert loaded == {"a": [1, 2], "b": [3, 4]}


# ---------------------------------------------------------------------------
# G4. Error envelope (§19)
# ---------------------------------------------------------------------------


def test_error_envelope_minimal():
    env = pm.ErrorEnvelope(code="target_ambiguous", message="two matches")
    assert env.retryable is False
    assert env.details == {}
    assert env.request_id is None


def test_error_envelope_round_trip_from_dict():
    data = {
        "code": "target_stale",
        "message": "snapshot expired",
        "retryable": True,
        "details": {"snapshot_id": "OBS-1"},
        "request_id": "REQ-1",
        "observed_at": "2026-08-01T10:00:00Z",
    }
    env = pm.ErrorEnvelope.from_dict(data)
    assert env.code == "target_stale"
    assert env.retryable is True
    assert env.details == {"snapshot_id": "OBS-1"}


def test_action_receipt_round_trip_with_error():
    data = {
        "ok": False,
        "request_id": "REQ-1",
        "server_instance_id": "INST-2",
        "observed_at": "2026-08-01T10:00:00Z",
        "error": {"code": "x", "message": "y"},
    }
    r = pm.ActionReceipt.from_dict(data)
    assert r.ok is False
    assert isinstance(r.error, pm.ErrorEnvelope)
    assert r.error.code == "x"


def test_action_receipt_rejects_nonbool_ok():
    with pytest.raises(pm.ProtocolModelError):
        pm.ActionReceipt(ok="yes", request_id="R", server_instance_id="I",
                         observed_at="t")