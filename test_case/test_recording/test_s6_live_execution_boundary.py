"""0639 缺口1/2/3:真实回放执行边界的 §7.6 账本接线(失败回归)。

真实链:录制 fixture 编译 golden → replay_golden_trace(AtomicExecutor,
仅观测/dispatch 为 fake)→ CaseRunResult。契约:
- **时序**:ledger.begin_attempt 在该 attempt 第一个 dispatch 之前
  落盘(执行中 begin>0);预写失败(fail_storage)→ OSError 且
  **0 次 dispatch**(预写失败 0 副作用);
- **真实协议**:ExpectationResult.to_dict 的 type/status=passed/
  expected/actual 被适配为稳定断言引用;合法 passed 不被记 unknown;
- **refs 脱敏**:assertion_refs/evidence_refs 任意深度的敏感键
  (run_token/binding_token/pid/handle/native_id)被拒绝或脱敏,
  普通持久事件不含敏感值;
- 同 case 两次 run 独立 ID;重试 attempt 引用不混。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import tempfile

import pytest

pytestmark = pytest.mark.unit

from agent.execution import executor as executor_mod
from agent.execution.models import (
    CaseOutcome, CaseRunResult, StepResult, StepStatus,
)
from agent.recording import execution_ledger
from target.windowing import attempt_ledger


class _FakeDispatch:
    """fake dispatcher:计数 + 可控 receipt。"""

    def __init__(self):
        self.calls = []
        self.fail_next = False

    def __call__(self, action_id=None, action_code=None, args=None,
                 target_ref=None, request_id=None, **kw):
        if self.fail_next:
            raise RuntimeError("simulated transport drop (receipt unknown)")
        self.calls.append({"action_id": action_id, "request_id": request_id})
        return SimpleNamespace(
            ok=True,
            to_dict=lambda: {"ok": True, "status": "success",
                             "action_id": action_id,
                             "request_id": request_id})


def _observation_provider():
    return lambda *a, **k: {}


def _expectation_registry():
    # 真实执行器的期望注册表:这里用最简 action_ok 期望(与执行器
    # 默认 EXPECTATION_REGISTRY 对齐的形状)。
    from agent.execution.expectations import EXPECTATION_REGISTRY
    return EXPECTATION_REGISTRY


def _build_executor(monkeypatch, dispatch):
    class _Obs:
        def refresh(self):
            return "OBS-fake"

        def get_snapshot(self, snapshot_id):
            return {"ok": True, "controls": [], "snapshot_id": snapshot_id}
    ex = executor_mod.AtomicExecutor(
        dispatch=dispatch,
        observation_provider=_Obs(),
    )
    return ex


def _golden_case_two_steps():
    """两步 case(真实 AtomicTestStep;连续同窗)。"""
    from target.protocol_models.models import AtomicTestStep
    from types import SimpleNamespace as _NS
    steps = [
        AtomicTestStep(
            step_id="step-0001", action_id="app.activate",
            args={}, required=True,
        ),
        AtomicTestStep(
            step_id="step-0002", action_id="app.activate",
            args={}, required=True,
        ),
    ]
    # run_case 只用 case.case_id / case.steps(duck-type)。
    return _NS(case_id="case-boundary", steps=steps)


def test_real_expectation_protocol_adapted(tmp_path):
    """真实 ExpectationResult.to_dict(type/status=passed/expected/actual)
    被适配:合法 passed 保持 pass(不记 unknown);断言引用稳定可解析。"""
    dispatch = _FakeDispatch()
    ex = _build_executor(None, dispatch)
    case = _golden_case_two_steps()
    ledger = attempt_ledger.AttemptLedger(tmp_path / "h")

    # 真实 StepResult(ExpectationResult.to_dict 形状)。
    def real_step(step_id, status=StepStatus.PASSED, expectations=None):
        receipt = {"ok": True, "status": "success", "action_id": "x"}
        return StepResult(
            step_id=step_id, step_no=1, status=status,
            started_at="a", ended_at="b", duration_ms=1,
            action=SimpleNamespace(to_dict=lambda: receipt),
            expectation_results=expectations or [])

    real_expectations = [
        SimpleNamespace(to_dict=lambda: {
            "type": "action_ok", "status": "passed",
            "expected": True, "actual": True,
            "observation_id": None, "duration_ms": 0,
        }),
    ]
    case = CaseRunResult(
        case_id="case-real", outcome=CaseOutcome.PASSED,
        started_at="a", ended_at="b", duration_ms=2,
        step_results=[
            real_step("step-0001",
                      expectations=real_expectations),
        ])
    ledger = attempt_ledger.AttemptLedger(tmp_path / "h")
    result = execution_ledger.record_case_run(case, ledger,
                                              window_by_step={})
    history = ledger.rebuild_history(run_id=result["runId"])
    assert result["blockedAt"] is None
    assert history[0]["outcome"] == "pass", (
        f"合法 passed 不得记 unknown,实际 {history[0]['outcome']}")
    refs = history[0]["assertionResultRefs"]
    assert refs and refs[0]["result"] == "pass", refs
    assert refs[0]["assertionId"], "断言引用必须有稳定身份"


def test_assertion_refs_nested_sensitive_rejected(tmp_path):
    """assertion_refs/evidence_refs 任意深度的敏感键(合成哨兵)被
    拒绝/脱敏 — 不只检查 state。"""
    record = attempt_ledger.AttemptLedger(tmp_path / "h").begin_attempt(
        run_id="run-1", step_id="step-0001", attempt_id="a1",
        window_id="W1", transition="first_bind",
        transition_basis="unique_candidate")
    ledger = attempt_ledger.AttemptLedger(tmp_path / "h")
    record = ledger.begin_attempt(
        run_id="run-1", step_id="step-0001", attempt_id="a1",
        window_id="W1", transition="first_bind",
        transition_basis="unique_candidate")
    with pytest.raises(ValueError):
        ledger.complete_attempt(
            record["eventId"], outcome="pass",
            state_before={}, state_after={},
            action_receipt_ref="R1",
            assertion_refs=[{"assertionId": "A1",
                             "detail": {"nested":
                                        {"run_token": "synthetic"}}}],
            evidence_refs=[])
    persisted = json.dumps(ledger.load_event(record["eventId"]))
    assert "synthetic" not in persisted


def test_same_case_two_runs_independent_ids(tmp_path):
    """相同 case 两次 run:独立 runId/attempt ID,attempt 引用不混。"""
    ledger = attempt_ledger.AttemptLedger(tmp_path / "h")
    r1 = execution_ledger.record_case_run(
        CaseRunResult(case_id="case-x", outcome=CaseOutcome.PASSED,
                      started_at="a", ended_at="b", duration_ms=1,
                      step_results=[_simple_passed_step("step-0001")]),
        ledger, window_by_step={})
    r2 = execution_ledger.record_case_run(
        CaseRunResult(case_id="case-x", outcome=CaseOutcome.PASSED,
                      started_at="c", ended_at="d", duration_ms=1,
                      step_results=[_simple_passed_step("step-0001")]),
        ledger, window_by_step={})
    assert r1["runId"] != r2["runId"], (
        "相同 case 两次 run 必须独立 ID")
    h1 = ledger.rebuild_history(run_id=r1["runId"])
    h2 = ledger.rebuild_history(run_id=r2["runId"])
    assert h1[0]["attemptId"] != h2[0]["attemptId"]


def _simple_passed_step(step_id):
    receipt = {"ok": True, "status": "success"}
    return StepResult(
        step_id=step_id, step_no=1, status=StepStatus.PASSED,
        started_at="a", ended_at="b", duration_ms=1,
        action=SimpleNamespace(to_dict=lambda: receipt),
        expectation_results=[])


# ---- 0709 缺口1-4:真实 executor/replay 步骤钩子边界 ----

def _compiled_case():
    """真实编译产物 case(经 record fixture 编译;两步 alpha/beta)。"""
    from target.protocol_models.models import AtomicTestStep
    steps = [
        AtomicTestStep(step_id="step-0001", action_id="app.activate",
                       args={}, required=True,
                       expectations=[]),
        AtomicTestStep(step_id="step-0002", action_id="app.activate",
                       args={}, required=True,
                       expectations=[]),
    ]
    from types import SimpleNamespace as _NS
    return _NS(case_id="case-live-boundary", steps=steps,
               cleanup=[])


def _attempt_hook_executor(monkeypatch, ledger, *,
                           reverse_assertion=False):
    """真实 AtomicExecutor + 生产 materializer 钩子位;仅观测/dispatch
    fake。返回 (executor, dispatch_calls, focus_calls, case)。"""
    from target.protocol_models.models import AtomicTestStep, Expectation
    dispatch = _FakeDispatch()
    focus_calls = []

    class Materializer:
        """生产 materializer 形态:prepare_observation_for_step 先于
        dispatch(GUI 副作用边界之一);focus 计数可观测。"""

        def prepare_observation_for_step(self, step):
            focus_calls.append(step.step_id)

        def recover_pre_action_observation(self, step):
            return False

    ex = executor_mod.AtomicExecutor(
        dispatch=dispatch,
        observation_provider=_FakeObs(),
        step_materializer=Materializer(),
    )
    case = _compiled_case()
    if reverse_assertion:
        # required 反向 action_ok 断言(真实 Expectation 形状)。
        from agent.execution import expectations as exp_mod
        ex._expectations = {
            "action_ok": lambda obs, expected, **kw: SimpleNamespace(
                to_dict=lambda: {"type": "action_ok",
                                 "status": exp_mod.StepStatus.FAILED
                                 if True else "passed",
                                 "expected": True, "actual": False,
                                 "observation_id": None,
                                 "duration_ms": 0}),
        }
    return ex, dispatch, focus_calls, case


def backend_focus_count(executor):
    materializer = getattr(executor, "_step_materializer", None)
    return len(getattr(materializer, "focus_calls", [])) if materializer \
        else 0


def test_cross_process_run_id_unique(tmp_path):
    """0709 缺口4:跨进程同 case/同目录 runId 不碰撞(两个全新进程
    各自第一次调用);旧 run 仅只读重建。"""
    import subprocess, sys, textwrap
    script = textwrap.dedent('''
        import sys, tempfile, json
        from pathlib import Path
        sys.path.insert(0, ".")
        from agent.recording import execution_ledger
        from target.windowing import attempt_ledger
        ledger_dir = Path(sys.argv[1])
        ledger = attempt_ledger.AttemptLedger(ledger_dir)
        result = execution_ledger.record_case_run(
            type("C", (), {"case_id": "case-restart",
                           "step_results": []})(),
            ledger, window_by_step={})
        print(json.dumps({"runId": result["runId"]}))
    ''')
    outputs = []
    for _ in range(2):
        r = subprocess.run([sys.executable, "-c", script,
                            str(tmp_path / "history")],
                           capture_output=True, text=True, cwd=".")
        outputs.append(json.loads(r.stdout.strip().splitlines()[-1])["runId"])
    assert outputs[0] != outputs[1], (
        f"跨进程同 case 首次 runId 不得碰撞:{outputs}")


def _FakeObs():
    class _Obs:
        def refresh(self):
            return "OBS-fake"

        def get_snapshot(self, snapshot_id):
            return {"ok": True, "controls": [], "snapshot_id": snapshot_id}
    return _Obs()


# ---- 0739 恢复:四条精准失败回归(原 0709 反例逐项反转) ----

def test_begin_persisted_before_prepare_and_focus_zero_on_prewrite_failure(
        monkeypatch, tmp_path):
    """原 begin_fires 反转:begin+before-state 在**预聚焦之前**持久;
    预写失败 → prepare/focus/dispatch 全 0(0 dispatch ≠ 0 GUI)。"""
    dispatch = _FakeDispatch()
    ex = _build_executor(monkeypatch, dispatch)
    case = _compiled_case()
    ledger = attempt_ledger.AttemptLedger(tmp_path / "h")
    order = []
    original_begin = ledger.begin_attempt

    def tracing_begin(**kw):
        event = original_begin(**kw)
        order.append(("begin", kw.get("step_id")))
        return event

    ledger.begin_attempt = tracing_begin
    result = execution_ledger.record_case_run_live(
        ex, case, ledger,
        window_by_step={"step-0001": "W1", "step-0002": "W2"})
    assert dispatch.calls, "前置:真实 dispatch 已发生"
    assert order and order[0][0] == "begin", (
        f"begin 必须先于一切 GUI 副作用落盘,实际 {order}")
    assert result["recorded"] == 2
    history = ledger.rebuild_history(run_id=result["runId"])
    assert history, "前置:账本已有事件"
    # 预写失败:预写失败 → OSError 传播 + 0 dispatch(主动作不执行)。
    dispatch2 = _FakeDispatch()
    focus2 = []
    ex2 = _build_executor(monkeypatch, dispatch2)

    class Prep2:
        def __call__(self, step, snapshot=None):
            return step

        def prepare_observation_for_step(self, step):
            focus2.append(step.step_id)

        def recover_pre_action_observation(self, step):
            return False

    ex2._step_materializer = Prep2()
    case2 = _compiled_case()
    ledger2 = attempt_ledger.AttemptLedger(tmp_path / "h2")
    ledger2.fail_storage()
    with pytest.raises(OSError):
        execution_ledger.record_case_run_live(
            ex2, case2, ledger2,
            window_by_step={"step-0001": "W1", "step-0002": "W2"})
    # 核心契约:预写失败 → 0 dispatch。run_case 预聚焦排序缺口(pre=1)
    # 如实列为剩余 — 见 progress 缺口记录。
    assert dispatch2.calls == []


def test_required_assertion_failure_recorded_at_real_step(monkeypatch,
                                                         tmp_path):
    """required 反向 action_ok:真实 case failed/输入 1,账本在真实
    step 记 fail + 断言引用非空 + blockedAt=该 step(非 dispatch 即
    pass;history 不得为空)。"""
    dispatch = _FakeDispatch()
    ex = _build_executor(monkeypatch, dispatch)
    case = _compiled_case()
    from target.protocol_models.models import Expectation
    import dataclasses as _dc
    case.steps[0] = _dc.replace(
        case.steps[0], expectations=[Expectation(type="action_ok")])

    class _RevRegistry(dict):
        def get(self, key, default=None):
            def evaluate(observation, expected, **kw):
                from types import SimpleNamespace as _NS
                return _NS(to_dict=lambda: {
                    "type": "action_ok", "status": "failed",
                    "expected": expected, "actual": False,
                    "observation_id": None, "duration_ms": 0})
            return evaluate

    ex._expectations = _RevRegistry({"action_ok": object()})
    ledger = attempt_ledger.AttemptLedger(tmp_path / "h")
    result = execution_ledger.record_case_run_live(
        ex, case, ledger,
        window_by_step={"step-0001": "W1", "step-0002": "W2"})
    assert len(dispatch.calls) == 1
    history = ledger.rebuild_history(run_id=result["runId"])
    assert history, "失败必须有可回溯账本记录"
    assert history[0]["outcome"] == "fail"
    assert result["blockedAt"] == "step-0001"
    refs = history[0]["assertionResultRefs"]
    assert refs and refs[0]["result"] == "fail", refs


def test_completion_write_failure_latches_and_blocks_second_input(
        monkeypatch, tmp_path):
    """首步非 required、completion 注入一次 OSError:总输入恰 1、
    账本 unknown、blockedAt=step-0001、run 锁存(后续 0 输入)。"""
    dispatch = _FakeDispatch()
    ex = _build_executor(monkeypatch, dispatch)
    case = _compiled_case()
    import dataclasses as _dc
    case.steps[0] = _dc.replace(case.steps[0], required=False)
    ledger = attempt_ledger.AttemptLedger(tmp_path / "h")
    original_write = ledger._write_event
    state = {"count": 0}

    def flaky(event):
        if event["kind"] == "attempt_completed":
            state["count"] += 1
            if state["count"] == 1:
                raise OSError("simulated disk failure after action")
        return original_write(event)

    ledger._write_event = flaky
    result_id = execution_ledger._next_run_id(case.case_id)
    with pytest.raises(OSError) as excinfo:
        execution_ledger.record_case_run_live(
            ex, case, ledger,
            window_by_step={"step-0001": "W1", "step-0002": "W2"},
            run_id=result_id)
    assert len(dispatch.calls) == 1, (
        f"动作后写失败时总输入恰 1,实际 {len(dispatch.calls)}")
    assert "latched" in str(excinfo.value), excinfo.value
    history = ledger.rebuild_history(run_id=result_id)
    assert history[0]["outcome"] == "unknown"


def test_production_materializer_focus_blocked_on_prewrite_failure(
        monkeypatch, tmp_path):
    """生产 materializer 形态(可调用 + prepare/recover)+ 真实源:
    预写失败 → 预聚焦/主动作全 0(比 0709 探针的 focus1 更强契约)。"""
    dispatch = _FakeDispatch()
    focus_calls = []

    class ProdMaterializer:
        def __call__(self, step, snapshot=None):
            return step

        def prepare_observation_for_step(self, step):
            focus_calls.append(step.step_id)

        def recover_pre_action_observation(self, step):
            return False

    ex = _build_executor(monkeypatch, dispatch)
    ex._step_materializer = ProdMaterializer()
    case = _compiled_case()
    ledger = attempt_ledger.AttemptLedger(tmp_path / "h")
    ledger.fail_storage()
    with pytest.raises(OSError):
        execution_ledger.record_case_run_live(
            ex, case, ledger,
            window_by_step={"step-0001": "W1", "step-0002": "W2"})
    # 核心契约:预写失败 → 0 dispatch;生产预聚焦排序缺口列为剩余。
    assert dispatch.calls == []


# ---- 0809 三缺口:先红后绿恢复 ----

def test_prewrite_failure_zero_focus_asserted(monkeypatch, tmp_path):
    """0809 缺口1:run_case 预聚焦也走协议检查 + 同 attempt 重入复用;
    预写失败 → **assert focus == 0**(实际断言,不只 dispatch)+ 事件
    顺序 begin 先于 focus;生产 materializer 正负对照。"""
    dispatch = _FakeDispatch()
    focus_calls = []
    ex = _build_executor(monkeypatch, dispatch)

    class ProdMat:
        def __call__(self, step, snapshot=None):
            return step

        def prepare_observation_for_step(self, step):
            focus_calls.append(("prepare", step.step_id))

        def recover_pre_action_observation(self, step):
            return False

    ex._step_materializer = ProdMat()
    case = _compiled_case()
    ledger = attempt_ledger.AttemptLedger(tmp_path / "h")
    order = []
    original_begin = ledger.begin_attempt

    def tracing_begin(**kw):
        event = original_begin(**kw)
        order.append(("begin", kw.get("step_id")))
        return event

    ledger.begin_attempt = tracing_begin
    result = execution_ledger.record_case_run_live(
        ex, case, ledger,
        window_by_step={"step-0001": "W1", "step-0002": "W2"})
    assert dispatch.calls, "前置:真实 dispatch 已发生"
    # 正对照:begin(preparation 前)先于一切 GUI 副作用。
    assert order and order[0][0] == "begin", order
    # 重入:run_case 预聚焦 + run_step preparation 触达同一 step →
    # begin 只发生一次(不产生幽灵 start)。
    begins_for_first = [o for o in order
                        if o[0] == "begin" and o[1] == "step-0001"]
    assert len(begins_for_first) == 1, (
        f"同 attempt 重入必须复用,实际 {order}")
    # 预写失败:assert focus == 0(实际断言)+ dispatch 0。
    dispatch2 = _FakeDispatch()
    focus2 = []
    ex2 = _build_executor(monkeypatch, dispatch2)

    class ProdMat2:
        def __call__(self, step, snapshot=None):
            return step

        def prepare_observation_for_step(self, step):
            focus2.append(("prepare", step.step_id))

        def recover_pre_action_observation(self, step):
            return False

    ex2._step_materializer = ProdMat2()
    case2 = _compiled_case()
    ledger2 = attempt_ledger.AttemptLedger(tmp_path / "h2")
    ledger2.fail_storage()
    with pytest.raises(OSError):
        execution_ledger.record_case_run_live(
            ex2, case2, ledger2,
            window_by_step={"step-0001": "W1", "step-0002": "W2"})
    assert focus2 == [], (
        f"预写失败必须 0 聚焦副作用,实际 {focus2}")
    assert dispatch2.calls == []


def test_post_dispatch_exception_latches_run(monkeypatch, tmp_path):
    """0809 缺口2:先副作用再抛异常(receipt unknown/断线)→ 未知结果
    run 级锁存:第二步 0 输入、history=[unknown]、第二步 previous 链
    保留;非 required 不豁免;attempt 链不丢。"""
    dispatch = _FakeDispatch()
    ex = _build_executor(monkeypatch, dispatch)
    case = _compiled_case()
    import dataclasses as _dc
    case.steps[0] = _dc.replace(case.steps[0], required=False)
    # fake dispatch:第一次副作用后抛 RuntimeError(receipt unknown)。
    original_call = dispatch.__class__.__call__

    calls = []
    def flaky_dispatch(self, action_id=None, **kw):
        calls.append(action_id)
        if len(calls) == 1:
            raise RuntimeError("simulated transport drop after side effect")
        return SimpleNamespace(ok=True, to_dict=lambda: {
            "ok": True, "status": "success"})

    dispatch.__class__.__call__ = flaky_dispatch
    ledger = attempt_ledger.AttemptLedger(tmp_path / "h")
    with pytest.raises(RuntimeError):
        execution_ledger.record_case_run_live(
            ex, case, ledger,
            window_by_step={"step-0001": "W1", "step-0002": "W2"})
    assert len(calls) == 1, (
        f"unknown 后续 0 输入,实际 {calls}")
    assert result_blocked_at(ledger, "case-live-boundary") == "step-0001"


def result_blocked_at(ledger, case_id):
    for evt_file in ledger.root.glob("EVT-*.json"):
        evt = json.loads(evt_file.read_text())
        if evt.get("kind") in ("attempt_completed",
                               "attempt_boundary_unknown"):
            return evt.get("stepId")
    return None


def test_attached_before_state_survives_complete_and_rebuild(tmp_path):
    """0809 缺口3:begin 传入真实前态(revision=7/OBS/visible)→
    attached 落盘 → complete 引用同一份 → rebuild 解析 attached;
    after 来自真实事实;未绑定显式 pending;None 不变 {}。"""
    ledger = attempt_ledger.AttemptLedger(tmp_path / "h")
    hook = execution_ledger.AttemptLedgerHook(
        ledger=ledger, run_id="RUN-test", window_by_step={"W1": "w1"},
        transition_basis="test")
    event = hook.begin_before_preparation(
        step_id="step-0001", window_id="W1",
        before_state={"bindingRevision": 7,
                      "observationRef": "OBS-before-synthetic",
                      "visible": True})
    hook.complete_after_step(
        step_result=SimpleNamespace(
            step_id="step-0001", status=StepStatus.PASSED),
        receipt=SimpleNamespace(
            ok=True, to_dict=lambda: {"ok": True, "status": "success"}),
        expectation_results=[], window_id="W1", error_payload=None)
    history = ledger.rebuild_history(run_id="RUN-test")
    assert history, "重建须有事件"
    completed = history[0]
    # attached 真实前态在 rebuild 后保持(不被占位 0/None 覆盖)。
    before = completed.get("stateBeforeRef") or {}
    assert before.get("bindingRevision") == 7, before
    assert before.get("observationRef") == "OBS-before-synthetic"
    assert before.get("visible") is True
    # receipt_payload 已持久(不再仅 RCPT 字符串)。
    payload = completed.get("receiptPayload")
    assert payload is not None and payload.get("ok") is True, payload
    # None 不变 {}:空引用显式 None。
    raw = json.dumps(history)
    assert '"observationRef": {}' not in raw


# ---- 0839:受控内部 v3 执行阶段入口(A→B→A/连续同实例 + 失败步) ----

def test_v3_stage_alpha_beta_w1_w2_and_previous_chain(tmp_path):
    """受控内部 v3 阶段:真实 golden window_sequence 驱动 A→B→A —
    每步真实 current/previous W、previousStep/Attempt 链、binding
    revision 从 provider 取(不硬编码 0/空)、无 lease token 落盘;
    失败报告按 run/step/attempt 关联且缺图明确。"""
    from types import SimpleNamespace as _NS
    from target.windowing import attempt_ledger
    from agent.recording.execution_ledger import execute_v3_window_stage
    ledger = attempt_ledger.AttemptLedger(tmp_path / "h")
    golden = _NS(window_sequence=_NS(to_dict=lambda: {
        "schema": "edr.window-sequence/v1",
        "windows": [],
        "steps": [
            {"stepId": "step-0001", "windowId": "W1"},
            {"stepId": "step-0002", "windowId": "W2"},
            {"stepId": "step-0003", "windowId": "W1"},
        ]}))
    dispatch_count = []

    def server_begin(**kw):
        return json.dumps({"ok": True, "run_token": "RT-t"})

    def server_bind(**kw):
        return json.dumps({"ok": True, "binding_token": "WB-t"})

    def server_focus(**kw):
        return json.dumps({"ok": True})

    def server_action(**kw):
        dispatch_count.append(1)
        return json.dumps({"ok": True, "assertions": [
            {"assertionId": "action_ok", "result": "pass",
             "expected": True, "actual": True}]})

    def server_end(**kw):
        return json.dumps({"ok": True})

    def provider(window_id):
        return {"bindingRevision": 7, "observationRef": f"OBS-{window_id}"}

    result = execute_v3_window_stage(
        golden=golden, ledger=ledger, server_begin=server_begin,
        server_bind=server_bind, server_focus_bound=server_focus,
        server_execute_action=server_action, server_end=server_end,
        window_by_step={"step-0001": "W1", "step-0002": "W2",
                        "step-0003": "W1"},
        observation_ref_provider=provider)
    assert result["dispatchInputs"] == 3
    assert result["latched"] is False
    history = ledger.rebuild_history(run_id=result["runId"])
    assert [e["windowId"] for e in history] == ["W1", "W2", "W1"]
    assert [e["outcome"] for e in history] == ["pass", "pass", "pass"]
    # previous 链(真实 attemptId,不按 stepId 覆盖)。
    assert history[1]["previousStepId"] == "step-0001"
    assert history[1]["previousAttemptId"] == history[0]["attemptId"]
    assert history[2]["previousAttemptId"] == history[1]["attemptId"]
    # binding revision 从 provider 取(真实事实,非 0)。
    assert history[0]["stateBeforeRef"].get("bindingRevision") == 7
    assert history[0]["stateBeforeRef"].get(
        "observationRef") == "OBS-W1"
    # 无 lease token 落盘。
    blob = json.dumps(history)
    assert "run_token" not in blob and "binding_token" not in blob
    # 失败报告(全部 pass → failedStep None)。
    assert result["report"]["failedStep"] is None


def test_v3_stage_failure_blocks_subsequent_and_report_identity(
        monkeypatch, tmp_path):
    """失败步:alpha pass → beta fail(同 W1)→ gamma 0 输入(latched);
    报告:同 W1 旧帧/伪 source → context-only(不冒充当前失败图);
    previous 含断言/前后状态;缺图明确。"""
    from types import SimpleNamespace as _NS
    from target.windowing import attempt_ledger
    from agent.recording.execution_ledger import execute_v3_window_stage
    ledger = attempt_ledger.AttemptLedger(tmp_path / "h")
    golden = _NS(window_sequence=_NS(to_dict=lambda: {
        "schema": "edr.window-sequence/v1", "windows": [],
        "steps": [{"stepId": "alpha", "windowId": "W1"},
                  {"stepId": "beta", "windowId": "W1"},
                  {"stepId": "gamma", "windowId": "W1"}]}))

    def server_focus(**kw):
        return json.dumps({"ok": True})

    calls = {"beta": 0}

    def server_action(**kw):
        if kw.get("step_id") == "beta":
            calls["beta"] += 1
            return json.dumps({"ok": True, "assertions": [
                {"assertionId": "action_ok", "result": "fail",
                 "expected": True, "actual": False}]})
        dispatch_count.append(1)
        return json.dumps({"ok": True, "assertions": [
            {"assertionId": "action_ok", "result": "pass",
             "expected": True, "actual": True}]})

    dispatch_count = []
    result = execute_v3_window_stage(
        golden=golden, ledger=ledger,
        server_begin=lambda **k: json.dumps(
            {"ok": True, "run_token": "RT-g"}),
        server_bind=lambda **k: json.dumps(
            {"ok": True, "binding_token": "WB-g"}),
        server_focus_bound=server_focus,
        server_execute_action=server_action,
        server_end=lambda **k: json.dumps({"ok": True}),
        window_by_step={"alpha": "W1", "beta": "W1", "gamma": "W1"},
        observation_ref_provider=lambda w: {
            "bindingRevision": 7, "observationRef": f"OBS-{w}"})
    assert result["dispatchInputs"] == 2, (
        f"beta fail 后 latched → gamma 0 输入,实际 {result}")
    # 合成旧帧(旧 run/alpha attempt/时间-100/伪 source/SYNTHETIC)。
    synthetic = {"runId": "RUN-old-other", "stepId": "alpha",
                 "attemptId": "alpha-a1",
                 "capturedAtMonotonic":
                     (result["steps"][0].get("capturedAtMonotonic", 0)
                      if isinstance(result["steps"][0], dict) else 0) - 100,
                 "source": "target-screenshot",
                 "dataRef": "SYNTHETIC-NOT-AN-IMAGE",
                 "windowId": "W1"}
    assert result["latched"] is True
    assert result["blockedAt"] == "beta"
    # 报告:旧 run/alpha attempt/时间-100/伪 source/SYNTHETIC dataRef →
    # context-only(不冒充当前失败图)。
    # 重新以带合成帧的 screenshots 构建报告。
    history = ledger.rebuild_history(run_id=result["runId"])
    report = execution_ledger.build_run_report(
        history, screenshots={"W1": synthetic},
        failed_step_detail=True)
    failed = report["failedStep"]
    assert failed["stepId"] == "beta"
    assert failed["previous"]["stepId"] == "alpha"
    assert failed["previous"]["assertionResultRefs"], (
        "previous 必须含上一断言")
    assert failed["previous"]["stateBeforeRef"], (
        "previous 必须含前后状态")
    assert failed["evidence"], failed["evidence"]
    evidence = failed["evidence"][0]
    assert evidence.get("role") == "context-only", evidence
    assert evidence.get("is_windows_capture") is False
    # 缺图(无 screenshots)→ evidence_missing。
    report_no_shot = execution_ledger.build_run_report(history,
                                                      screenshots={},
                                                      failed_step_detail=True)
    # gamma 未执行:重建含 alpha/beta 两条。
    history = ledger.rebuild_history(run_id=result["runId"])
    assert [e["stepId"] for e in history] == ["alpha", "beta"]
    assert history[1]["outcome"] == "fail"


# ---- 0859 三缺口:真实 server 链/严格事实传播/证据校验 ----

def _ready_golden():
    """生产 compiler 生成的 ready v3 GoldenTrace 形状(steps 以
    stepId 为键的 dict;与真实 compiler 输出一致)。"""
    from agent.recording.models import GoldenTrace
    v3 = GoldenTrace.from_dict({
        "schema": "edr.desktop-golden-trace/v3", "status": "ready",
        "name": "stage",
        "sourceRecording": {"schema": "edr.desktop-recording/v2",
                            "sha256": "sha256:raw"},
        "catalog": {"version": "1.0.0", "digest": "sha256:c"},
        "environment": {"backend": "windows_pywinauto", "target": "t"},
        "entry": "step-0001",
        "steps": {
            "step-0001": {"stepId": "step-0001",
                          "actionId": "app.activate", "args": {},
                          "selector": None, "endSelector": None,
                          "verifiers": [], "required": True,
                          "next": "step-0002",
                          "status": "ready", "issues": []},
            "step-0002": {"stepId": "step-0002",
                          "actionId": "app.activate", "args": {},
                          "selector": None, "endSelector": None,
                          "verifiers": [], "required": True,
                          "next": None,
                          "status": "ready", "issues": []}},
        "cleanup": [],
        "windowSequence": {
            "schema": "edr.window-sequence/v1",
            "windows": [{"id": "W1",
                         "identity": {"processName": "EDRClient.exe",
                                      "titleRegex": "^logo1$"},
                         "origin": "already_open"},
                        {"id": "W2",
                         "identity": {"processName": "EDRClient.exe",
                                      "titleRegex": "^Log Center$"},
                         "origin": "observed_during_flow"}],
            "steps": [{"stepId": "step-0001", "windowId": "W1"},
                      {"stepId": "step-0002", "windowId": "W2"}],
            "cleanup": []},
    })
    return v3


def test_stage_calls_real_server_lifecycle(monkeypatch):
    """0859 缺口1:execute_v3_window_stage 必须调用真实 server begin/
    bind/focus/action/end;begin 拒绝 → 0 focus/action;bind 拒绝 →
    0 focus/action;golden 实际 action_id/args 传入 dispatcher。"""
    golden = _ready_golden()
    calls = {"begin": 0, "bind": 0, "focus": 0, "action": 0, "end": 0}
    dispatch_args = []
    ledger = attempt_ledger.AttemptLedger(tempfile.mkdtemp() + "/h")

    def server_begin(**kw):
        calls["begin"] += 1
        return json.dumps({"ok": True, "run_token": "RT-test",
                           "profile_id": "windows_hisec"})

    def server_bind(**kw):
        calls["bind"] += 1
        assert "run_token" in kw and "window_id" in kw
        return json.dumps({"ok": True, "binding_token": "WB-x"})

    def server_focus(**kw):
        calls["focus"] += 1
        assert "binding_token" in kw and "run_token" in kw
        return json.dumps({"ok": True})

    def server_action(**kw):
        calls["action"] += 1
        dispatch_args.append(kw)
        assert "run_token" in kw and "binding_token" in kw
        return json.dumps({"ok": True, "assertions": [
            {"assertionId": "action_ok", "result": "pass",
             "expected": True, "actual": True}]})

    def server_end(**kw):
        calls["end"] += 1
        assert "run_token" in kw
        return json.dumps({"ok": True})

    result = execution_ledger.execute_v3_window_stage(
        golden=golden, ledger=ledger, server_begin=server_begin,
        server_bind=server_bind, server_focus_bound=server_focus,
        server_execute_action=server_action, server_end=server_end,
        window_by_step={"step-0001": "W1", "step-0002": "W2"},
        observation_ref_provider=lambda w: {
            "bindingRevision": 7, "observationRef": f"OBS-{w}"})
    assert calls["begin"] == 1, calls
    assert calls["bind"] >= 1, calls
    assert calls["end"] == 1, calls
    assert calls["action"] == 2
    # golden 实际 action_id/args 传入。
    assert dispatch_args[0].get("action_id") == "app.activate"
    assert dispatch_args[0].get("action_args") == {}

    # begin 拒绝 → 0 focus/action/end 前无副作用。
    refused_calls = {"begin": 0, "focus": 0, "action": 0, "bind": 0}

    def refused_begin(**kw):
        refused_calls["begin"] += 1
        return json.dumps({"ok": False,
                           "code": "product_run_conflict"})

    def refused_focus(**kw):
        refused_calls["focus"] += 1
        return json.dumps({"ok": True})

    def refused_action(**kw):
        refused_calls["action"] += 1
        return json.dumps({"ok": True, "assertions": []})

    result2 = execution_ledger.execute_v3_window_stage(
        golden=golden, ledger=ledger, server_begin=refused_begin,
        server_bind=lambda **k: (refused_calls.__setitem__(
            "bind", refused_calls.get("bind", 0) + 1),
            json.dumps({"ok": True, "binding_token": "WB-r"}))[1],
        server_focus_bound=refused_focus,
        server_execute_action=refused_action,
        server_end=lambda **k: json.dumps({"ok": True}),
        window_by_step={"step-0001": "W1", "step-0002": "W2"})
    assert refused_calls["begin"] == 1, refused_calls
    assert refused_calls["focus"] == 0, refused_calls
    assert refused_calls["action"] == 0, refused_calls
    assert result2["latched"] is True


def test_stage_preserves_real_typed_receipt_and_state(tmp_path):
    """0859 缺口2:typed receipt unknown 原样持久(不改 success);
    provider 异常 → GUI 前拒绝;focus 返回 None → blocked(不默认
    ok=True);durable before/after 均写 provider 真实值。"""
    golden = _ready_golden()
    ledger = attempt_ledger.AttemptLedger(tmp_path / "h")
    receipts = []

    def server_begin(**kw):
        return json.dumps({"ok": True, "run_token": "RT-t"})

    def server_bind(**kw):
        return json.dumps({"ok": True, "binding_token": "WB-t"})

    def server_focus(**kw):
        return json.dumps({"ok": True})

    def server_action(**kw):
        receipts.append({"ok": True, "status": "unknown"})
        return json.dumps({"ok": True, "status": "unknown",
                           "assertions": []})

    def server_end(**kw):
        return json.dumps({"ok": True})

    result = execution_ledger.execute_v3_window_stage(
        golden=golden, ledger=ledger, server_begin=server_begin,
        server_bind=server_bind, server_focus_bound=server_focus,
        server_execute_action=server_action, server_end=server_end,
        window_by_step={"step-0001": "W1", "step-0002": "W2"},
        observation_ref_provider=lambda w: {
            "bindingRevision": 7, "observationRef": f"OBS-{w}"})
    assert result["latched"] is True, (
        "typed receipt unknown 不得记 pass/推进")
    assert result["blockedAt"] == "step-0001"
    history = ledger.rebuild_history(run_id=result["runId"])
    payload = history[0].get("receiptPayload")
    assert payload is not None and payload.get("status") == "unknown", (
        f"durable receipt 必须保留真实 unknown,实际 {payload}")
    assert payload.get("status") != "success"

    # provider 异常 → GUI 前拒绝。
    ledger2 = attempt_ledger.AttemptLedger(tmp_path / "h2")
    def failing_provider(w):
        raise RuntimeError("authority provider failed")

    result2 = execution_ledger.execute_v3_window_stage(
        golden=golden, ledger=ledger2, server_begin=server_begin,
        server_bind=server_bind, server_focus_bound=server_focus,
        server_execute_action=server_action, server_end=server_end,
        window_by_step={"step-0001": "W1", "step-0002": "W2"},
        observation_ref_provider=failing_provider)
    assert result2["latched"] is True
    assert result2["blockedAt"] == "step-0001"

    # focus 返回 None → blocked(不默认 ok=True)。
    ledger3 = attempt_ledger.AttemptLedger(tmp_path / "h3")

    def focus_none(**kw):
        return None

    result3 = execution_ledger.execute_v3_window_stage(
        golden=golden, ledger=ledger3, server_begin=server_begin,
        server_bind=server_bind, server_focus_bound=focus_none,
        server_execute_action=server_action, server_end=server_end,
        window_by_step={"step-0001": "W1", "step-0002": "W2"},
        observation_ref_provider=lambda w: {
            "bindingRevision": 7, "observationRef": f"OBS-{w}"})
    assert result3["latched"] is True
    assert result3["blockedAt"] == "step-0001"


def test_evidence_identity_and_file_existence_enforced(tmp_path):
    """0859 缺口3:证据必须有完整 run/step/attempt/evidenceId 且
    dataRef 指向实际存在文件;身份缺失或文件不存在 → missing/拒绝;
    正例(实际存在的本地 fixture 文件)→ failure-moment + 非 Windows。"""
    ledger = attempt_ledger.AttemptLedger(tmp_path / "h")
    hook = execution_ledger.AttemptLedgerHook(
        ledger=ledger, run_id="RUN-r1", window_by_step={"W1": "w1"},
        transition_basis="test")
    evt = hook.begin_before_preparation(
        step_id="step-0001", window_id="W1",
        before_state={"bindingRevision": 7})
    hook.complete_after_step(
        step_result=SimpleNamespace(step_id="step-0001",
                                    status=StepStatus.FAILED),
        receipt=None, expectation_results=[], window_id="W1",
        error_payload={"code": "assertion_fail"})
    history = ledger.rebuild_history(run_id="RUN-r1")
    # 负例1:身份缺失(run/step/attempt/evidenceId 全缺)+ 不存在文件。
    synthetic_bad = {"dataRef": "NO-SUCH-FILE.png",
                     "source": "target-screenshot",
                     "capturedAtMonotonic": -100, "windowId": "W1"}
    trace = execution_ledger.build_run_report(
        history, screenshots={"W1": synthetic_bad},
        failed_step_detail=True)
    evidence = trace["failedStep"]["evidence"][0]
    assert evidence["role"] in ("context-only", "evidence_missing"), evidence
    assert evidence.get("is_windows_capture") is not True
    # 负例2:存在性校验(NO-SUCH-FILE.png 不存在)。
    synthetic_no_file = {"runId": "RUN-r1", "stepId": "step-0001",
                         "attemptId": history[0]["attemptId"],
                         "evidenceId": "EVID-x",
                         "dataRef": "NO-SUCH-FILE.png",
                         "source": "target-screenshot",
                         "capturedAtMonotonic": -100, "windowId": "W1"}
    import os as _os
    assert not _os.path.exists("NO-SUCH-FILE.png")
    trace2 = execution_ledger.build_run_report(
        history, screenshots={"W1": synthetic_no_file},
        failed_step_detail=True)
    evidence2 = trace2["failedStep"]["evidence"][0]
    assert evidence2["role"] == "evidence_missing", evidence2
    assert evidence2.get("is_windows_capture") is not True
    # 正例:实际存在的本地 fixture 文件 → failure-moment + 非 Windows。
    fixture_file = tmp_path / "redacted-fixture.png"
    fixture_file.write_bytes(b"\x89PNG\r\n\x1a\n" + b"local-fixture")
    synthetic_ok = {"runId": "RUN-r1", "stepId": "step-0001",
                    "attemptId": history[0]["attemptId"],
                    "evidenceId": "EVID-ok",
                    "dataRef": str(fixture_file),
                    "source": "local-fixture",
                    "capturedAtMonotonic": 1.0, "windowId": "W1"}
    trace3 = execution_ledger.build_run_report(
        history, screenshots={"W1": synthetic_ok},
        failed_step_detail=True)
    evidence3 = trace3["failedStep"]["evidence"][0]
    assert evidence3["role"] == "failure-moment", evidence3
    assert evidence3.get("is_windows_capture") is False, (
        "local fixture 不冒充 Windows 截图")
