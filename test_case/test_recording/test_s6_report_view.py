"""片4:§7.6 失败步 JSON 与带图报告贯通(09 §10)。

- 从账本重建任意失败步的 JSON 视图:上一步窗口/动作/attempt/断言、
  切窗依据(transitionId+basis)、前后状态引用;
- 截图证据:标注对应 W 与观测时间;本地 fixture 截图明确标记
  (不虚称 Windows 截图);缺图 evidence_missing,不拿旧图冒充;
- 报告/审计不持久化 token/原生 handle/pid(脱敏)。
"""

from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.unit

from target.windowing import attempt_ledger
from target.windowing.report_view import (
    build_failure_trace,
    evidence_missing,
    fixture_screenshot,
)


@pytest.fixture()
def ledger(tmp_path):
    return attempt_ledger.AttemptLedger(tmp_path / "history")


def _run_history(ledger):
    a1 = ledger.begin_attempt(
        run_id="run-1", step_id="step-0001", attempt_id="a1",
        window_id="W1", transition="first_bind",
        transition_basis="unique_candidate")
    ledger.complete_attempt(
        a1["eventId"], outcome="pass",
        state_before={"sequenceCursor": 0, "bindingRevision": 1,
                      "observationRef": "OBS-1"},
        state_after={"sequenceCursor": 1, "bindingRevision": 1,
                     "observationRef": "OBS-2"},
        action_receipt_ref="RCPT-1", assertion_refs=[],
        evidence_refs=["EVID-1"])
    a2 = ledger.begin_attempt(
        run_id="run-1", step_id="step-0002", attempt_id="a2",
        window_id="W2", transition="switched_bound_window",
        transition_basis="declared_window_change",
        previous=ledger.rebuild_history(run_id="run-1")[-1])
    ledger.complete_attempt(
        a2["eventId"], outcome="fail",
        state_before={"sequenceCursor": 1, "bindingRevision": 2,
                      "observationRef": "OBS-3"},
        state_after={"sequenceCursor": 1, "bindingRevision": 2,
                     "observationRef": "OBS-4"},
        action_receipt_ref="RCPT-2",
        assertion_refs=[{"assertionId": "A1", "result": "fail",
                         "detail": "row count mismatch"}],
        evidence_refs=[])
    return ledger.rebuild_history(run_id="run-1")


def test_failure_trace_carries_previous_window_and_transition(ledger):
    """任意失败步可查:上一步窗口/attempt/断言、切窗依据、前后状态。"""
    history = _run_history(ledger)
    trace = build_failure_trace(history, step_id="step-0002")
    assert trace["stepId"] == "step-0002"
    assert trace["windowId"] == "W2"
    assert trace["transitionId"] == "switched_bound_window"
    assert trace["transitionBasis"] == "declared_window_change"
    assert trace["previous"]["stepId"] == "step-0001"
    assert trace["previous"]["windowId"] == "W1"
    assert trace["previous"]["outcome"] == "pass"
    assert trace["assertionResultRefs"][0]["result"] == "fail"
    assert trace["stateBeforeRef"]["observationRef"] == "OBS-3"
    assert trace["stateAfterRef"]["observationRef"] == "OBS-4"


def test_missing_screenshot_marked_not_substituted(ledger):
    """失败步无截图:evidence_missing 明确标记,不用旧图冒充。"""
    history = _run_history(ledger)
    trace = build_failure_trace(history, step_id="step-0002",
                                screenshots={})
    assert trace["evidence"] == [{"status": "evidence_missing",
                                  "windowId": "W2",
                                  "reason": "no screenshot for this step"}]


def test_fixture_screenshot_is_labeled(ledger):
    """本地 fixture 截图必须标明 fixture 来源,不虚称 Windows 截图。"""
    labeled = fixture_screenshot(
        window_id="W2", captured_at_monotonic=123.5,
        source="local-fixture", data_ref="fake://frame/1")
    assert labeled["source"] == "local-fixture"
    assert labeled["is_windows_capture"] is False
    assert labeled["windowId"] == "W2"
    blob = json.dumps(labeled)
    assert "windows" not in blob.lower() or labeled["is_windows_capture"] is False


def test_report_blob_never_leaks_tokens_or_native_ids(ledger, tmp_path):
    """报告/审计不持久化 token/原生 handle/pid。"""
    history = _run_history(ledger)
    trace = build_failure_trace(history, step_id="step-0002",
                                screenshots={
                                    "W2": fixture_screenshot(
                                        window_id="W2", captured_at_monotonic=1,
                                        source="local-fixture",
                                        data_ref="fake://frame/2"),
                                })
    blob = json.dumps(trace)
    for forbidden in ("run_token", "binding_token", '"handle"',
                      '"pid"', '"native_id"'):
        assert forbidden not in blob, forbidden


def test_evidence_missing_helper():
    marker = evidence_missing(window_id="W1", reason="capture refused")
    assert marker["status"] == "evidence_missing"
    assert marker["reason"] == "capture refused"
