"""09 §7.6 完整可回溯步骤状态:attempt 账本 + append-only 持久历史。

覆盖契约:
- attempt 记录 runId/stepId/attemptId/previousStepId/previousAttemptId/
  windowId/previousWindowId/transitionId/transitionBasis/stateBeforeRef/
  stateAfterRef/actionReceiptRef/assertionResultRefs/evidenceRefs/outcome;
- outcome 区分 pass/fail/unknown/skipped;unknown 不得推进为成功;
- 首步 previous 显式为空;重试单独记录 attempt;
- 脱敏:原生 handle/pid/租约 token 不进持久状态;
- append-only + 稳定事件 ID;重启后从落盘记录重建;
- 写盘失败先于动作 → 后续副作用被禁止;动作后写失败 → 标记不确定边界
  (unknown),不声称动作未执行;
- 失败/unknown 阻断推进;只读上一状态。
"""

from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.unit

from target.windowing import attempt_ledger


@pytest.fixture()
def ledger(tmp_path):
    store = attempt_ledger.AttemptLedger(tmp_path / "history")
    return store


def test_attempt_record_full_contract(ledger):
    """attempt 记录包含 §7.6 全部字段;首步 previous 显式为空。"""
    record = ledger.begin_attempt(
        run_id="run-1", step_id="step-0001", attempt_id="a1",
        window_id="W1", transition="first_bind",
        transition_basis="unique_candidate")
    assert record["runId"] == "run-1"
    assert record["stepId"] == "step-0001"
    assert record["attemptId"] == "a1"
    assert record["previousStepId"] is None
    assert record["previousAttemptId"] is None
    assert record["previousWindowId"] is None
    assert record["outcome"] == "unknown"  # 未完成为 unknown
    ledger.complete_attempt(
        record["eventId"], outcome="pass",
        state_before={"sequenceCursor": 0, "bindingRevision": 1,
                      "visible": True, "effectResult": None,
                      "observationRef": "OBS-1"},
        state_after={"sequenceCursor": 1, "bindingRevision": 1,
                     "visible": True, "effectResult": "appeared",
                     "observationRef": "OBS-2"},
        action_receipt_ref="RCPT-1",
        assertion_refs=[{"assertionId": "A1", "result": "pass"}],
        evidence_refs=["EVID-1"])
    history = ledger.rebuild_history(run_id="run-1")
    assert len(history) == 1
    loaded = history[0]
    assert loaded["outcome"] == "pass"
    assert loaded["stateBeforeRef"]["sequenceCursor"] == 0
    assert loaded["actionReceiptRef"] == "RCPT-1"
    assert loaded["assertionResultRefs"][0]["result"] == "pass"


def test_persisted_state_redacts_native_ids_and_tokens(ledger):
    """原生 handle/pid/租约 token 不得进入持久状态记录。"""
    record = ledger.begin_attempt(
        run_id="run-1", step_id="step-0001", attempt_id="a1",
        window_id="W1", transition="first_bind",
        transition_basis="unique_candidate")
    with pytest.raises(ValueError) as excinfo:
        ledger.complete_attempt(
            record["eventId"], outcome="pass",
            state_before={"sequenceCursor": 0, "handle": 100,
                          "pid": 42, "runToken": "tok"},
            state_after={"sequenceCursor": 1},
            action_receipt_ref="RCPT-1",
            assertion_refs=[], evidence_refs=[])
    assert "native" in str(excinfo.value).lower() or \
        "forbidden" in str(excinfo.value).lower(), (
        f"持久状态含原生身份字段必须拒绝,实际 {excinfo.value}")
    # 合法引用(ref 字符串)可以指向原生事实,但值本身不得是原生字段。
    persisted = ledger.load_event(record["eventId"])
    blob = json.dumps(persisted)
    assert "runToken" not in blob
    assert '"pid"' not in blob
    assert '"handle"' not in blob


def test_unknown_outcome_cannot_advance(ledger):
    """unknown 不得推进为成功:advance_gate 拒绝 unknown/fail/skipped。"""
    assert ledger.can_advance("pass") is True
    for outcome in ("fail", "unknown", "skipped"):
        assert ledger.can_advance(outcome) is False, outcome


def test_previous_references_actual_prior_attempt(ledger):
    """后续 attempt 引用实际执行的前一步;重试单独记录 attempt。"""
    first = ledger.begin_attempt(
        run_id="run-1", step_id="step-0001", attempt_id="a1",
        window_id="W1", transition="first_bind",
        transition_basis="unique_candidate")
    completed = ledger.complete_attempt(
        first["eventId"], outcome="pass",
        state_before={"sequenceCursor": 0},
        state_after={"sequenceCursor": 1},
        action_receipt_ref="R1",
        assertion_refs=[], evidence_refs=[])
    second = ledger.begin_attempt(
        run_id="run-1", step_id="step-0002", attempt_id="a2",
        window_id="W1", transition="same_window_reverified",
        transition_basis="binding_reverified",
        previous=completed)
    assert second["previousStepId"] == "step-0001"
    assert second["previousAttemptId"] == "a1"
    assert second["previousWindowId"] == "W1"
    # 重试:同 step 新 attempt,引用同一前一步。
    retry = ledger.begin_attempt(
        run_id="run-1", step_id="step-0002", attempt_id="a3",
        window_id="W2", transition="switched_bound_window",
        transition_basis="declared_window_change",
        previous=completed)
    assert retry["attemptId"] == "a3"
    assert retry["previousAttemptId"] == "a1"


def test_append_only_and_rebuild_after_restart(ledger, tmp_path):
    """append-only + 稳定事件 ID;新 store 实例(模拟重启)从落盘重建。"""
    first = ledger.begin_attempt(
        run_id="run-1", step_id="step-0001", attempt_id="a1",
        window_id="W1", transition="first_bind",
        transition_basis="unique_candidate")
    completed = ledger.complete_attempt(
        first["eventId"], outcome="pass",
        state_before={"sequenceCursor": 0},
        state_after={"sequenceCursor": 1},
        action_receipt_ref="R1",
        assertion_refs=[], evidence_refs=[])
    event_id = first["eventId"]
    completion_id = completed["eventId"]
    # 追加不可变:同一 attempt 重复 complete 拒绝。
    with pytest.raises(ValueError):
        ledger.complete_attempt(first["eventId"], outcome="fail",
                                state_before={}, state_after={},
                                action_receipt_ref="R2",
                                assertion_refs=[], evidence_refs=[])
    # 重启重建:全新实例读取同一目录。
    rebuilt = attempt_ledger.AttemptLedger(tmp_path / "history")
    history = rebuilt.rebuild_history(run_id="run-1")
    assert [e["stepId"] for e in history] == ["step-0001"]
    assert history[0]["outcome"] == "pass"
    assert history[0]["eventId"] == completion_id
    assert history[0]["of"] == event_id


def test_write_failure_before_action_blocks_side_effects(ledger):
    """动作前写盘失败:begin_attempt 抛出 → blocked_for_actions 标记,
    后续副作用被禁止。"""
    ledger.fail_storage()  # 测试注入:模拟磁盘故障
    with pytest.raises(OSError):
        ledger.begin_attempt(
            run_id="run-1", step_id="step-0001", attempt_id="a1",
            window_id="W1", transition="first_bind",
            transition_basis="unique_candidate")
    assert ledger.blocked_for_actions("run-1") is True


def test_write_failure_after_action_marks_unknown_boundary(ledger):
    """动作后写盘失败:complete_attempt 抛出 → 该 attempt 标记
    unknown 边界(不可声称动作未执行);后续推进被阻断。"""
    record = ledger.begin_attempt(
        run_id="run-1", step_id="step-0001", attempt_id="a1",
        window_id="W1", transition="first_bind",
        transition_basis="unique_candidate")
    ledger.fail_storage()
    with pytest.raises(OSError):
        ledger.complete_attempt(record["eventId"], outcome="pass",
                                state_before={"sequenceCursor": 0},
                                state_after={"sequenceCursor": 1},
                                action_receipt_ref="R1",
                                assertion_refs=[], evidence_refs=[])
    # 重启恢复后:该 attempt 必须呈现为 unknown(不确定边界),
    # 不是 pass(动作已进入但结果未知),也不是"未执行"。
    recovered = attempt_ledger.AttemptLedger(ledger.root)
    history = recovered.rebuild_history(run_id="run-1")
    assert history[0]["outcome"] == "unknown", history
    assert recovered.can_advance(history[0]["outcome"]) is False


def test_failed_previous_blocks_next_step(ledger):
    """上一结果不允许推进时,下一步不能靠重新聚焦绕过失败。"""
    first = ledger.begin_attempt(
        run_id="run-1", step_id="step-0001", attempt_id="a1",
        window_id="W1", transition="first_bind",
        transition_basis="unique_candidate")
    ledger.complete_attempt(first["eventId"], outcome="fail",
                            state_before={"sequenceCursor": 0},
                            state_after={"sequenceCursor": 0},
                            action_receipt_ref="R1",
                            assertion_refs=[{"assertionId": "A1",
                                             "result": "fail"}],
                            evidence_refs=[])
    previous = ledger.load_event(first["eventId"])
    with pytest.raises(ValueError):
        ledger.begin_attempt(
            run_id="run-1", step_id="step-0002", attempt_id="a2",
            window_id="W2", transition="switched_bound_window",
            transition_basis="declared_window_change",
            previous=previous)


# ---- 2112 缺口2:嵌套结构脱敏(合成哨兵,非真实凭据) ----

@pytest.mark.parametrize("sentinel_state", [
    {"nested": {"run_token": "synthetic-secret"}},
    {"rows": [{"binding_token": "synthetic-secret"}]},
    {"deep": {"deeper": [{"leasetoken": "synthetic-secret"}]}},
    {"receipt": {"run_token": "synthetic-secret"}},
    {"assertionRefs": [{"pid": 1234}]},
    {"evidenceRefs": [{"native_id": "100"}]},
])
def test_nested_native_identity_rejected_at_any_depth(ledger, sentinel_state):
    """嵌套 dict/list/receipt/assertion/evidence 中的原生身份字段在
    任意深度都被拒绝持久化(合成哨兵,不使用真实凭据)。"""
    record = ledger.begin_attempt(
        run_id="run-1", step_id="step-0001", attempt_id="a1",
        window_id="W1", transition="first_bind",
        transition_basis="unique_candidate")
    with pytest.raises(ValueError) as excinfo:
        ledger.complete_attempt(
            record["eventId"], outcome="pass",
            state_before=sentinel_state, state_after={},
            action_receipt_ref="R1", assertion_refs=[], evidence_refs=[])
    assert "forbidden" in str(excinfo.value).lower() or \
        "native identity" in str(excinfo.value).lower()
    # 拒绝后事件文件里不得残留哨兵值。
    persisted = json.dumps(ledger.load_event(record["eventId"]))
    assert "synthetic-secret" not in persisted
    assert "1234" not in persisted or "pid" not in persisted


def test_nested_clean_state_persists(ledger):
    """嵌套但无原生身份字段的状态正常持久化(不误伤)。"""
    record = ledger.begin_attempt(
        run_id="run-1", step_id="step-0001", attempt_id="a1",
        window_id="W1", transition="first_bind",
        transition_basis="unique_candidate")
    ledger.complete_attempt(
        record["eventId"], outcome="pass",
        state_before={"nested": {"cursor": 0, "rows": [1, 2]}},
        state_after={"nested": {"cursor": 1}},
        action_receipt_ref="R1", assertion_refs=[], evidence_refs=[])
    history = ledger.rebuild_history(run_id="run-1")
    assert history[0]["stateBeforeRef"]["nested"]["rows"] == [1, 2]
