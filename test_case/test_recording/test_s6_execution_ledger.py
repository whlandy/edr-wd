"""2112 缺口4:helper 接入真实执行链(replay 结果 → §7.6 账本 → 报告)。

- agent.recording.execution_ledger.record_case_run:把真实
  CaseRunResult(StepResult 列表)按步骤写入 AttemptLedger(append-only,
  每步 begin→complete);
- 动作前持久化:begin 先于动作结果落盘(顺序可检测);
- 动作后写盘失败 → unknown 边界(不声称未执行);
- 上一结果非 pass → 下一步 begin 被 ledger 拒绝(gate_next_step 语义);
- 业务断言严格 pass 才推进(effect_gate);
- build_failure_trace 从重建历史产出带图报告(缺图 evidence_missing);
- 持久状态递归脱敏(嵌套哨兵)。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.unit

from agent.execution.models import CaseOutcome, CaseRunResult, StepResult, StepStatus
from agent.recording import execution_ledger
from target.windowing import attempt_ledger


def _step(step_id, status, *, action_ok=True, assertions=None):
    receipt = {"ok": action_ok, "status":
               "success" if action_ok else "unknown",
               "action_id": "gui.click"}
    return StepResult(
        step_id=step_id, step_no=1, status=status,
        started_at="2026-11-03T13:00:00Z", ended_at="2026-11-03T13:00:01Z",
        duration_ms=1000,
        action=SimpleNamespace(to_dict=lambda: receipt),
        expectation_results=assertions or [])


def _case(steps, outcome=CaseOutcome.PASSED):
    return CaseRunResult(case_id="case-1", outcome=outcome,
                         started_at="2026-11-03T13:00:00Z",
                         ended_at="2026-11-03T13:00:05Z", duration_ms=5000,
                         step_results=steps)


def test_real_case_run_recorded_to_ledger(tmp_path):
    """真实 CaseRunResult 按步骤入账:每步 begin+complete,pass 推进。"""
    case = _case([
        _step("step-0001", StepStatus.PASSED,
              assertions=[_expectation("A1", "pass")]),
        _step("step-0002", StepStatus.PASSED,
              assertions=[_expectation("A2", "pass")]),
    ])
    ledger = attempt_ledger.AttemptLedger(tmp_path / "h")
    result = execution_ledger.record_case_run(case, ledger,
                                              window_by_step={
                                                  "step-0001": "W1",
                                                  "step-0002": "W1"})
    assert result["recorded"] == 2
    history = ledger.rebuild_history(run_id=result["runId"])
    assert [e["stepId"] for e in history] == ["step-0001", "step-0002"]
    assert all(e["outcome"] == "pass" for e in history)
    # previous 链:step-0002 引用 step-0001。
    assert history[1]["previousStepId"] == "step-0001"


def _expectation(aid, result):
    return SimpleNamespace(to_dict=lambda aid=aid, result=result: {
        "expectation_id": aid, "result": result})


def test_unknown_assertion_blocks_next_step_in_real_chain(tmp_path):
    """缺口3 的真实链:断言 unknown → 本步 unknown → 下一步 begin 被拒,
    不会偷推进(不删断言求绿)。"""
    case = _case([
        _step("step-0001", StepStatus.PASSED,
              assertions=[_expectation("A1", "unknown")]),
        _step("step-0002", StepStatus.PASSED),
    ])
    ledger = attempt_ledger.AttemptLedger(tmp_path / "h")
    result = execution_ledger.record_case_run(case, ledger,
                                              window_by_step={
                                                  "step-0001": "W1",
                                                  "step-0002": "W1"})
    # 阻断:step-0002 未入账(begin 从未被调用),blockedAt 指向 unknown 步。
    assert result["blockedAt"] == "step-0001"
    assert result["recorded"] == 1
    history = ledger.rebuild_history(run_id=result["runId"])
    assert [e["stepId"] for e in history] == ["step-0001"]
    assert history[-1]["outcome"] == "unknown"


def test_failed_receipt_marks_unknown_and_blocks(tmp_path):
    """receipt unknown → 步骤 unknown;下一步不 begin。"""
    case = _case([
        _step("step-0001", StepStatus.FAILED, action_ok=False),
        _step("step-0002", StepStatus.PASSED),
    ], outcome=CaseOutcome.FAILED)
    ledger = attempt_ledger.AttemptLedger(tmp_path / "h")
    result = execution_ledger.record_case_run(case, ledger,
                                              window_by_step={
                                                  "step-0001": "W1"})
    assert result["blockedAt"] == "step-0001"
    history = ledger.rebuild_history(run_id=result["runId"])
    assert history[-1]["outcome"] == "unknown"


def test_post_action_write_failure_marks_unknown(tmp_path):
    """动作后写盘失败:该步落 unknown 边界(不是 pass/未执行);
    后续步被阻断。"""
    case = _case([_step("step-0001", StepStatus.PASSED),
                  _step("step-0002", StepStatus.PASSED)])
    ledger = attempt_ledger.AttemptLedger(tmp_path / "h")
    original = ledger._write_event
    state = {"count": 0}

    def flaky(event):
        if event["kind"] == "attempt_completed":
            state["count"] += 1
            if state["count"] == 1:
                raise OSError("simulated disk failure after action")
        return original(event)

    ledger._write_event = flaky
    run_id = "RUN-deterministic-post-action-write-test"
    with pytest.raises(OSError):
        execution_ledger.record_case_run(
            case, ledger, window_by_step={"step-0001": "W1"},
            run_id=run_id)
    # 重启重建:step-0001 呈现 unknown(动作可能已执行)。
    rebuilt = attempt_ledger.AttemptLedger(tmp_path / "h")
    history = rebuilt.rebuild_history(run_id=run_id)
    assert history[0]["outcome"] == "unknown"
    assert history[0]["reason"] == "post_action_write_failed"


def test_failure_trace_with_screenshot_contract(tmp_path):
    """带图报告契约:失败步可查上一步/切窗依据;缺图 evidence_missing;
    fixture 截图标记非 Windows 来源。"""
    case = _case([
        _step("step-0001", StepStatus.PASSED),
        _step("step-0002", StepStatus.FAILED, action_ok=False),
    ], outcome=CaseOutcome.FAILED)
    ledger = attempt_ledger.AttemptLedger(tmp_path / "h")
    result = execution_ledger.record_case_run(
        case, ledger,
        window_by_step={"step-0001": "W1", "step-0002": "W2"})
    history = ledger.rebuild_history(run_id=result["runId"])
    trace = execution_ledger.build_run_report(
        history, screenshots={}, failed_step_detail=True)
    failed = trace["failedStep"]
    assert failed["stepId"] == "step-0002"
    assert failed["previous"]["stepId"] == "step-0001"
    assert failed["evidence"][0]["status"] == "evidence_missing"
    # fixture 截图进入报告时明确标注。
    labeled = execution_ledger.build_run_report(history, failed_step_detail=True, screenshots={
        "W2": {"source": "local-fixture", "dataRef": "fake://1",
               "capturedAtMonotonic": 1.0}})
    shot = labeled["failedStep"]["evidence"][0]
    assert shot["source"] == "local-fixture"
    assert shot.get("is_windows_capture") is False


def test_persisted_history_has_no_credentials(tmp_path):
    """端到端:真实链落盘历史无 token/handle/pid(递归)。"""
    case = _case([_step("step-0001", StepStatus.PASSED)])
    ledger = attempt_ledger.AttemptLedger(tmp_path / "h")
    result = execution_ledger.record_case_run(case, ledger,
                                              window_by_step={"step-0001":
                                                              "W1"})
    for path in (tmp_path / "h").glob("EVT-*.json"):
        blob = path.read_text().lower()
        for forbidden in ("run_token", "binding_token", '"handle"',
                          '"pid"', '"native_id"'):
            assert forbidden not in blob, (path.name, forbidden)
