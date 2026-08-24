"""P1.2 acceptance gate — expectation evaluators."""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest


_REPO = Path(__file__).resolve().parents[2]
for p in (str(_REPO / "target"), str(_REPO / "agent")):
    if p not in sys.path:
        sys.path.insert(0, p)


from action_dispatcher import ActionReceipt  # noqa: E402

from execution import (  # noqa: E402
    EVALUATORS_NOT_AVAILABLE,
    EXPECTATION_REGISTRY,
    ExpectationNotAvailable,
    StepStatus,
)
from execution.expectations import _EvidenceView  # noqa: E402
from trace import EvidenceRecord  # noqa: E402


def _exp(type_: str, value=None, timeout_seconds: float = 1.0):
    # local import to avoid pulling protocol_models top-level
    from protocol_models import Expectation
    return Expectation(type=type_, value=value, timeout_seconds=timeout_seconds)


def _ok_receipt(action_id="gui.click"):
    return ActionReceipt(
        code="ok",
        ok=True,
        action_id=action_id,
        action_code="A020",
        request_id="R-1",
        server_instance_id="inst-x",
        result={"data": {"ok": True}},
    )


def _err_receipt():
    return ActionReceipt(
        code="backend_error",
        ok=False,
        action_id="gui.click",
        action_code="A020",
        request_id="R-1",
        server_instance_id="inst-x",
        result={"error": "boom"},
    )


def test_registry_has_every_documented_evaluator():
    """Recording replay extends state assertions without removing existing evaluators."""
    expected = {
        "action_ok",
        "window_open",
        "window_closed",
        "active_window_owner",
        "control_exists",
        "control_absent",
        "control_text_equals",
        "control_text_contains",
        "control_value_equals",
        "control_checked_equals",
        "control_enabled_equals",
        "window_text_contains",
        "control_text_contains_time",
        "window_text_contains_time",
        "visual_evidence_captured",
    }
    assert set(EXPECTATION_REGISTRY.keys()) == expected


def test_visual_evidence_is_now_available():
    """P1.4: visual_evidence_captured ships as a real evaluator."""
    assert "visual_evidence_captured" not in EVALUATORS_NOT_AVAILABLE
    assert EXPECTATION_REGISTRY.get("visual_evidence_captured") is not None


def _evidence_record(tmp_path, *, role="after", content=b"evidence"):
    relative_path = "screenshots/S001.png"
    path = tmp_path / relative_path
    path.parent.mkdir()
    path.write_bytes(content)
    return EvidenceRecord(
        evidence_id="IMG-1", kind="screenshot", role=role,
        relative_path=relative_path, media_type="image/png",
        sha256="sha256:" + hashlib.sha256(content).hexdigest(),
        bytes_size=len(content), width=1, height=1,
        captured_at="2026-01-01T00:00:00Z", snapshot_id="OBS-1",
        event_id="EVT-1", step_id="S001", process_name="app",
        pid=1, window_title="window", redaction_applied=True,
    )


def _evaluate_visual(step, expectation, record, root):
    token = _EvidenceView.set_evidence_index({record.evidence_id: record}, root)
    try:
        return EXPECTATION_REGISTRY["visual_evidence_captured"](
            step, expectation, None, None,
        )
    finally:
        _EvidenceView.reset(token)


def test_visual_evidence_requires_matching_role_and_digest(tmp_path):
    from protocol_models import AtomicTestStep

    step = AtomicTestStep(step_id="S001", step_no=1, action_id="gui.click")
    record = _evidence_record(tmp_path)
    result = _evaluate_visual(
        step, _exp("visual_evidence_captured", "after"), record, tmp_path,
    )
    assert result.status is StepStatus.PASSED

    mismatched = _evaluate_visual(
        step, _exp("visual_evidence_captured", "before"), record, tmp_path,
    )
    assert mismatched.status is StepStatus.FAILED
    assert "expected role" in mismatched.diagnostic

    (tmp_path / record.relative_path).write_bytes(b"tampered")
    corrupt = _evaluate_visual(
        step, _exp("visual_evidence_captured", "after"), record, tmp_path,
    )
    assert corrupt.status is StepStatus.FAILED
    assert "invalid digest" in corrupt.diagnostic


def test_evidence_view_restores_previous_context(tmp_path):
    record = _evidence_record(tmp_path)
    outer = _EvidenceView.set_evidence_index({"outer": record}, tmp_path)
    inner = _EvidenceView.set_evidence_index({}, None)
    _EvidenceView.reset(inner)
    try:
        assert _EvidenceView.current_evidence_index() == {"outer": record}
        assert _EvidenceView.current_trace_dir() == tmp_path
    finally:
        _EvidenceView.reset(outer)


# -------- action_ok --------


def test_action_ok_passes_when_receipt_ok():
    r = EXPECTATION_REGISTRY["action_ok"](None, _exp("action_ok"), None, _ok_receipt())
    assert r.status is StepStatus.PASSED
    assert r.actual is True


def test_action_ok_fails_when_receipt_failed():
    r = EXPECTATION_REGISTRY["action_ok"](None, _exp("action_ok"), None, _err_receipt())
    assert r.status is StepStatus.FAILED


def test_action_ok_fails_when_no_receipt():
    r = EXPECTATION_REGISTRY["action_ok"](None, _exp("action_ok"), None, None)
    assert r.status is StepStatus.FAILED


def test_action_ok_negative_expected():
    """`expected=False` flips the assertion (negative test case)."""
    r = EXPECTATION_REGISTRY["action_ok"](None, _exp("action_ok", False), None, _err_receipt())
    assert r.status is StepStatus.PASSED


# -------- window_open / window_closed --------


def test_window_open_passes_on_match():
    obs = {"windows": [{"process": "notepad", "title": "Untitled"}]}
    r = EXPECTATION_REGISTRY["window_open"](
        None, _exp("window_open", {"process": "notepad"}), obs, None,
    )
    assert r.status is StepStatus.PASSED


def test_window_open_fails_when_no_match():
    obs = {"windows": [{"process": "mspaint", "title": "x"}]}
    r = EXPECTATION_REGISTRY["window_open"](
        None, _exp("window_open", {"process": "notepad"}), obs, None,
    )
    assert r.status is StepStatus.FAILED


def test_window_open_supports_title_regex_match():
    obs = {"windows": [{"process_name": "EDRClient.exe", "title": "策略设置 - EDR"}]}
    result = EXPECTATION_REGISTRY["window_open"](
        None,
        _exp("window_open", {"process_name": "EDRClient.exe", "title_regex": "^策略设置"}),
        obs,
        None,
    )
    assert result.status is StepStatus.PASSED


def test_window_closed_passes_when_absent():
    obs = {"windows": [{"process": "mspaint"}]}
    r = EXPECTATION_REGISTRY["window_closed"](
        None, _exp("window_closed", {"process": "notepad"}), obs, None,
    )
    assert r.status is StepStatus.PASSED


# -------- active_window_owner --------


def test_active_window_owner_passes():
    obs = {"active_window": {"owner": "EDRClient.exe", "title": "Home"}}
    r = EXPECTATION_REGISTRY["active_window_owner"](
        None, _exp("active_window_owner", "EDRClient.exe"), obs, None,
    )
    assert r.status is StepStatus.PASSED


def test_active_window_owner_fails_on_mismatch():
    obs = {"active_window": {"owner": "chrome.exe"}}
    r = EXPECTATION_REGISTRY["active_window_owner"](
        None, _exp("active_window_owner", "EDRClient.exe"), obs, None,
    )
    assert r.status is StepStatus.FAILED


# -------- control_exists / control_absent --------


def test_control_exists_passes():
    obs = {"controls": [{"automation_id": "btn-ok", "text": "OK"}]}
    r = EXPECTATION_REGISTRY["control_exists"](
        None, _exp("control_exists", {"automation_id": "btn-ok"}), obs, None,
    )
    assert r.status is StepStatus.PASSED


def test_control_exists_fails_when_missing():
    obs = {"controls": [{"automation_id": "btn-cancel"}]}
    r = EXPECTATION_REGISTRY["control_exists"](
        None, _exp("control_exists", {"automation_id": "btn-ok"}), obs, None,
    )
    assert r.status is StepStatus.FAILED


def test_control_absent_passes():
    obs = {"controls": [{"automation_id": "btn-cancel"}]}
    r = EXPECTATION_REGISTRY["control_absent"](
        None, _exp("control_absent", {"automation_id": "btn-ok"}), obs, None,
    )
    assert r.status is StepStatus.PASSED


# -------- control_text_* --------


def test_control_text_equals_passes():
    obs = {"controls": [{"automation_id": "btn-ok", "text": "OK"}]}
    r = EXPECTATION_REGISTRY["control_text_equals"](
        None,
        _exp("control_text_equals", {"match": {"automation_id": "btn-ok"}, "text": "OK"}),
        obs, None,
    )
    assert r.status is StepStatus.PASSED


def test_control_text_equals_fails_on_mismatch():
    obs = {"controls": [{"automation_id": "btn-ok", "text": "Cancel"}]}
    r = EXPECTATION_REGISTRY["control_text_equals"](
        None,
        _exp("control_text_equals", {"match": {"automation_id": "btn-ok"}, "text": "OK"}),
        obs, None,
    )
    assert r.status is StepStatus.FAILED


def test_control_text_contains_passes():
    obs = {"controls": [{"automation_id": "btn-ok", "text": "Open Settings"}]}
    r = EXPECTATION_REGISTRY["control_text_contains"](
        None,
        _exp("control_text_contains", {"match": {"automation_id": "btn-ok"}, "text": "Settings"}),
        obs, None,
    )
    assert r.status is StepStatus.PASSED


def test_control_text_contains_fails_on_substring_miss():
    obs = {"controls": [{"automation_id": "btn-ok", "text": "Cancel"}]}
    r = EXPECTATION_REGISTRY["control_text_contains"](
        None,
        _exp("control_text_contains", {"match": {"automation_id": "btn-ok"}, "text": "Settings"}),
        obs, None,
    )
    assert r.status is StepStatus.FAILED


# -------- window_text_contains --------


def test_window_text_contains_passes_via_window():
    obs = {"windows": [{"title": "Protection Center"}], "controls": []}
    r = EXPECTATION_REGISTRY["window_text_contains"](
        None, _exp("window_text_contains", "Protection"), obs, None,
    )
    assert r.status is StepStatus.PASSED


def test_window_text_contains_falls_back_to_control_text():
    obs = {"windows": [{"title": "Home"}], "controls": [{"text": "Welcome to Protection"}]}
    r = EXPECTATION_REGISTRY["window_text_contains"](
        None, _exp("window_text_contains", "Protection"), obs, None,
    )
    assert r.status is StepStatus.PASSED


def test_window_text_contains_fails():
    obs = {"windows": [{"title": "Home"}], "controls": [{"text": "Welcome"}]}
    r = EXPECTATION_REGISTRY["window_text_contains"](
        None, _exp("window_text_contains", "Protection"), obs, None,
    )
    assert r.status is StepStatus.FAILED


# -------- robustness --------


def test_evaluator_returns_passed_or_failed_only():
    """Architecture §9.3: an evaluator returns passed/failed/error.
    P1.2's StepStatus enum only models passed/failed; the executor
    promotes an exception to blocked at a higher level."""
    for name, evaluator in EXPECTATION_REGISTRY.items():
        result = evaluator(None, _exp(name, "x"), {"windows": [], "controls": []}, None)
        assert result.status in (StepStatus.PASSED, StepStatus.FAILED), name


def test_unmatched_observation_is_handled_gracefully():
    """Observations can be None or have empty lists without crashing.

    Negative evaluators (`window_closed`, `control_absent`) return
    passed when the tree is empty; positive evaluators return
    failed. This test covers the positive side only.
    """
    positive = {
        "window_open",
        "active_window_owner",
        "control_exists",
        "control_text_equals",
        "control_text_contains",
        "window_text_contains",
    }
    for name in positive:
        evaluator = EXPECTATION_REGISTRY[name]
        result = evaluator(None, _exp(name, "x"), None, None)
        assert result.status is StepStatus.FAILED, name


def test_expectation_not_available_sentinel_still_exposed():
    """EVALUATORS_NOT_AVAILABLE is now empty (P1.4); the sentinel
    still exists for callers that branch on it."""
    from execution import EVALUATORS_NOT_AVAILABLE
    # P1.4: empty set; future evaluator types can register here.
    assert isinstance(EVALUATORS_NOT_AVAILABLE, frozenset)
    # visual_evidence_captured moved from not-available to the registry.
    assert "visual_evidence_captured" not in EVALUATORS_NOT_AVAILABLE
