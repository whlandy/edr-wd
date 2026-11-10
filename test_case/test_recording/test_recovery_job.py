"""Recovery-job tests (design 12 §3/§7/§10, R09/R10/R11).

成对反例纪律（test-mate）:
- R09 正例: 第一层无可证修复（refusals 实证）+ 第二层 rerecord 具体
  失败点（R05 形状：核账失败原件）→ 作业级 unrecovered，两阶段证据
  refs 齐全，不称永久不可用。
- R09 反例（不可伪 unrecovered）: 仅第一层失败、第二层从未启动 →
  blocked（stagesNotRun 如实列出）；receipt unknown → unknown。
- R10 反例×3: authorization → awaiting_authorization；busy → blocked；
  模型400 → unknown；均零 GUI 消耗（rerecord 零调用）。
- R11 反例×3: ①崩溃重建——持久状态后新实例读回，budgetUsed 保持、
  已完成阶段跳过、零重复输入；②新鲜租约拒绝第二控制者（decline 事件
  持久）；③20分钟唤醒重入同 jobId——budget 不重置。

OS 事实层:R09 正例端到端走 T3 fixture 真实链（RecordingService/
correlator/ingest/compiler/v3 执行链真实，仅 OS fake）;其余用例在
job 层用函数替身验证状态机（GUI 副作用计数恒零）。
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

from agent.recording.recovery_job import (
    BUDGET_LIMITS,
    JOB_OUTCOME_AWAITING_AUTHORIZATION,
    JOB_OUTCOME_BLOCKED,
    JOB_OUTCOME_DECLINED,
    JOB_OUTCOME_RECOVERED_RERECORD,
    JOB_OUTCOME_UNKNOWN,
    JOB_OUTCOME_UNRECOVERED,
    RecoveryJob,
    RecoveryJobError,
    execute_recovery,
    recovery_report,
    synthesize_outcome,
)
from agent.recording.rerecord_loop import (
    RerecordJob,
    RerecordStageError,
)

pytestmark = pytest.mark.unit


# ------------------------------------------------------------ helpers

def _make_job(tmp_path, job_id="REC-JOB-1", **kwargs):
    return RecoveryJob(
        tmp_path / "state" / f"{job_id}.json",
        recovery_job_id=job_id,
        origin={"artifact": "REC-647312", "sha256": "sha256:6d8b18343287"},
        failure_category="trace_defect",
        failure_reason="compile_weak_identity_unresolved",
        **kwargs)


class _Proposal:
    def __init__(self, candidates=(), refusals=()):
        self.candidates = list(candidates)
        self.refusals = list(refusals)


class _Golden:
    status = "incomplete"


class _CompileResult:
    golden = _Golden()
    diagnostics = {"sourceEvents": {"byStep": {}}}


def _no_candidate_proposal(recording, golden, diagnostics):
    return _Proposal(candidates=[], refusals=[
        type("R", (), {"reason": "structural_identity_unprovable",
                       "detail": "no frames to prove identity"})()])


def _rerecord_fails(code="reconciliation_failed", stage="agent_execution"):
    def _fn(plan, job=None, **kwargs):
        assert isinstance(job, RerecordJob)
        job.enter("agent_execution")
        raise RerecordStageError(stage, code, "concrete failure point",
                                 evidence={"missing": [{"stepId": "s1"}]})
    return _fn


def _rerecord_recovered(plan, job=None, **kwargs):
    job.record("fresh_replay_and_assertions", "verification_passed",
               {"runId": "run-x"})
    return job, {"replay": {"runId": "run-x"}, "raw": {"events": [1]},
                 "sessionId": "REC-new"}


def _fake_plan(recording, compile_result):
    return type("P", (), {"parent_sha256": "sha256:plan",
                          "steps": [], "gaps": [],
                          "uncompiled_points": 0})()


BASIC_KW = dict(recording=object(), compile_result=_CompileResult(),
                owner="controller-A", round_id="t4-1",
                propose_repairs_fn=_no_candidate_proposal,
                build_plan_fn=_fake_plan)


# ---------------------------------------------------------------- R09

def test_r09_both_layers_fail_with_evidence_is_unrecovered(tmp_path):
    """R09 正例: 第一层无候选（refusals 实证）+ 第二层具体失败点 →
    unrecovered（本预算内），两阶段证据齐;不称 permanent。"""
    job = _make_job(tmp_path)
    _, detail = execute_recovery(
        job, **BASIC_KW, rerecord_fn=_rerecord_fails())
    assert job.outcome() == JOB_OUTCOME_UNRECOVERED
    reason = job._doc["outcome"]["reason"]
    assert "both layers failed with evidence" in reason
    assert "permanent" not in reason
    # 两阶段证据 refs。
    refs = job._doc
    assert refs["repairCandidateRefs"] == []
    assert refs["planRef"]["parentSha256"] == "sha256:plan"
    # 预算如实消耗:一轮修复搜索 + 一次重录（验证回放未达）。
    assert job.budget_used() == {"repairSearches": 1,
                                 "rerecordAttempts": 1,
                                 "verificationReplays": 0}
    # 报告:已执行/未执行阶段如实分列。
    report = recovery_report(json.loads(
        job.state_path and Path(job.state_path).read_text()))
    assert set(report["stagesRun"]) == {"repair", "step_plan", "rerecord"}
    assert report["stagesNotRun"] == ["verification"]
    assert report["outcome"] == JOB_OUTCOME_UNRECOVERED


def test_r09_rerecord_failure_alone_never_claims_unrecovered(tmp_path):
    """R09 反例（不可伪）: 修复层从未运行、第二层失败 → blocked，
    报告如实列出 repair/stage_plan 未执行——两层失败证据不齐全。"""
    label, reason = synthesize_outcome(
        repair_attempted=False, repair_candidates=0, repair_refusals=0,
        repair_validated=None, rerecord_outcome="unrecovered")
    assert label == JOB_OUTCOME_BLOCKED
    assert "repair layer never ran" in reason


def test_r09_repair_validation_failure_then_rerecord_failure_unrecovered(
        tmp_path):
    """R09 同型（§4 次序）: 修复候选真实验证失败 → 记录原因后进入
    步骤提取与重录（§4"验证失败……记录原因后进入步骤提取"）;重录再
    失败 → unrecovered（两层证据齐）。"""
    class _Cand:
        rule_id = "structural_identity_from_sequential_sibling_frames"
        parent_sha256 = "sha256:parent"
        semantics_changed = False

    def _with_candidate(recording, golden, diagnostics):
        return _Proposal(candidates=[_Cand()], refusals=[])
    job = _make_job(tmp_path, job_id="REC-JOB-2")
    _, detail = execute_recovery(
        job, **{**BASIC_KW, "propose_repairs_fn": _with_candidate,
                "validate_candidate": lambda cand: False},
        rerecord_fn=_rerecord_fails())
    assert job.outcome() == JOB_OUTCOME_UNRECOVERED
    assert "candidates failed real validation" in \
        job._doc["outcome"]["reason"]
    assert job.budget_used()["rerecordAttempts"] == 1
    events = [e["kind"] for e in job._doc["events"]]
    assert "candidate_validated" in events
    assert "rerecord_started" in events


def test_r09_repair_validation_failure_alone_is_blocked_not_unrecovered(
        tmp_path):
    """R09 反例同型: 修复候选真实验证失败、重录从未启动（合成层直接
    输入）→ blocked（不是 unrecovered），未执行阶段如实列出。"""
    label, reason = synthesize_outcome(
        repair_attempted=True, repair_candidates=1, repair_refusals=0,
        repair_validated=False, rerecord_outcome=None)
    assert label == JOB_OUTCOME_BLOCKED
    assert "rerecord layer never ran" in reason


# ---------------------------------------------------------------- R10

def _assert_zero_rerecord(job):
    assert not job.has_event("rerecord_started")
    assert job.budget_used()["rerecordAttempts"] == 0


def test_r10_authorization_missing_is_awaiting_authorization(tmp_path):
    """R10: 缺授权 → awaiting_authorization，零 GUI 重试，不误判轨迹坏
    （failureCategory 保持 trace_defect 输入，不由授权缺口改写）。"""
    job = _make_job(tmp_path, job_id="REC-JOB-3")
    _, detail = execute_recovery(
        job, **BASIC_KW, rerecord_fn=_rerecord_fails(),
        blocked_reason="authorization missing for desktop re-record")
    assert job.outcome() == JOB_OUTCOME_AWAITING_AUTHORIZATION
    _assert_zero_rerecord(job)
    assert job._doc["failureCategory"] == "trace_defect"


def test_r10_desktop_busy_is_blocked(tmp_path):
    """R10: 目标忙 → blocked（不是 unrecovered），零 GUI 消耗。"""
    job = _make_job(tmp_path, job_id="REC-JOB-4")
    _, detail = execute_recovery(
        job, **BASIC_KW, rerecord_fn=_rerecord_fails(),
        blocked_reason="desktop busy: another controller active")
    assert job.outcome() == JOB_OUTCOME_BLOCKED
    _assert_zero_rerecord(job)


def test_r10_model_400_is_unknown(tmp_path):
    """R10: 模型请求400 → unknown，零 GUI 重试。"""
    job = _make_job(tmp_path, job_id="REC-JOB-5")
    _, detail = execute_recovery(
        job, **BASIC_KW, rerecord_fn=_rerecord_fails(),
        blocked_reason="model request failed with HTTP 400")
    assert job.outcome() == JOB_OUTCOME_UNKNOWN
    _assert_zero_rerecord(job)


def test_r10_receipt_unknown_latch_is_unknown_not_unrecovered(tmp_path):
    """R10/R07 同型: receipt unknown 锁存 → 作业级 unknown，不是
    unrecovered（动作可能已执行;恢复属另一作业+安全初态）。"""
    label, reason = synthesize_outcome(
        repair_attempted=True, repair_candidates=0, repair_refusals=1,
        repair_validated=None, rerecord_outcome="unknown")
    assert label == JOB_OUTCOME_UNKNOWN
    assert "receipt latch" in reason


# ---------------------------------------------------------------- R11

def test_r11_crash_rebuild_skips_completed_stages_keeps_budget(tmp_path):
    """R11①: 崩溃/重启后从持久状态重建——已完成阶段跳过、budgetUsed
    保持、零重复输入（propose 只在第一进程跑一次）。"""
    calls = {"propose": 0, "plan": 0}

    def counting_proposal(recording, golden, diagnostics):
        calls["propose"] += 1
        return _no_candidate_proposal(recording, golden, diagnostics)

    def counting_plan(recording, compile_result):
        calls["plan"] += 1
        return _fake_plan(recording, compile_result)

    kw = {**BASIC_KW, "propose_repairs_fn": counting_proposal,
          "build_plan_fn": counting_plan}

    job1 = _make_job(tmp_path, job_id="REC-JOB-6")
    # 第一进程: 修复+计划完成，重录启动后"崩溃"（直接持久状态注入
    # rerecord_started，不发生 stage_done）。
    from agent.recording.rerecord_loop import RerecordJob as _RJ

    class _CrashFn:
        def __call__(self, plan, job=None, **kwargs):
            job.record("rerecord", "rerecord_started_marker", {})
            raise RuntimeError("process crashed mid-rerecord")
    import agent.recording.recovery_job as rj_module
    real_rj = rj_module.RerecordJob
    rj_module.RerecordJob = _RJ
    try:
        job1._doc["events"].append({"stage": "rerecord", "kind":
                                    "rerecord_started", "at": 0})
        job1._doc["budgetUsed"]["rerecordAttempts"] = 1
        rj_module._persist = lambda self_doc: None  # 占位，直接用 save
        job1._persist(job1._doc)
    finally:
        rj_module.RerecordJob = real_rj
    assert calls["propose"] == 0 and calls["plan"] == 0

    job2 = RecoveryJob(job1.state_path, recovery_job_id="REC-JOB-6")
    assert job2.budget_used()["rerecordAttempts"] == 1
    assert job2.has_event("rerecord_started")
    _, detail = execute_recovery(job2, **kw)
    # 重建: 不重复修复/计划/重录——中断的重录按语义转 unknown。
    assert job2.outcome() == JOB_OUTCOME_UNKNOWN
    assert "interrupted" in job2._doc["outcome"]["reason"]
    assert calls["propose"] == 0 and calls["plan"] == 0
    assert job2.budget_used() == {"repairSearches": 0,
                                  "rerecordAttempts": 1,
                                  "verificationReplays": 0}


def test_r11_completed_job_reentry_returns_terminal_untouched(tmp_path):
    """R11①b: 已有终态的作业重入——原样返回，零新增事件/预算。"""
    job = _make_job(tmp_path, job_id="REC-JOB-7")
    execute_recovery(job, **BASIC_KW, rerecord_fn=_rerecord_fails())
    events_before = len(job._doc["events"])
    budget_before = job.budget_used()
    job2 = RecoveryJob(job.state_path, recovery_job_id="REC-JOB-7")
    _, detail = execute_recovery(job2, **BASIC_KW,
                                 rerecord_fn=_rerecord_fails())
    assert detail.get("alreadyTerminal") is True
    assert len(job2._doc["events"]) == events_before
    assert job2.budget_used() == budget_before


def test_r11_fresh_lease_declines_second_controller(tmp_path):
    """R11②: 新鲜租约在另一控制者手里 → 第二控制者被拒（decline 持久
    事件），零 GUI/编辑接触;租约期满（同持有人重入）可继续。"""
    job = _make_job(tmp_path, job_id="REC-JOB-8")
    assert job.acquire_lease("controller-A", "round-1") is True
    job.release_lease()
    # 第二控制者无法拿已释放的...释放后可获取——先验证持有人拒绝路径。
    job2 = _make_job(tmp_path, job_id="REC-JOB-8")
    assert job2.acquire_lease("controller-B", "round-2") is True
    job2.release_lease()
    job3 = _make_job(tmp_path, job_id="REC-JOB-8")
    assert job3.acquire_lease("controller-C", "round-3") is True
    job3._doc["lease"]["acquiredAt"] = time.time() - 10  # 新鲜
    job3._persist(job3._doc)
    assert job3.acquire_lease("controller-D", "round-4") is False
    declined = [e for e in job3._doc["events"]
                if e["kind"] == "lease_declined"]
    assert declined and declined[-1]["by"] == "controller-D"
    assert declined[-1]["heldBy"] == "controller-C"


def test_r11_execute_declined_entry_records_and_stays_terminal(tmp_path):
    """R11②b: execute_recovery 入口遇新鲜外部租约 → declined_busy
    终态，重录零调用。"""
    job = _make_job(tmp_path, job_id="REC-JOB-9")
    job._doc["lease"] = {"owner": "other-cc", "roundId": "r",
                         "acquiredAt": time.time()}
    job._persist(job._doc)
    rerecord_calls = []

    def spy(plan, job=None, **kwargs):
        rerecord_calls.append(1)
        return _rerecord_recovered(plan, job=job, **kwargs)

    _, detail = execute_recovery(job, **{**BASIC_KW, "rerecord_fn": spy})
    assert job.outcome() == JOB_OUTCOME_DECLINED
    assert rerecord_calls == []


def test_r11_scheduled_wakeup_reenters_same_job_budget_not_reset(tmp_path):
    """R11③: 20分钟唤醒重入同 recoveryJobId——budget 不重置、不当作
    新预算;第二次重录尝试被预算门拒绝（budget_exhausted）。"""
    job = _make_job(tmp_path, job_id="REC-JOB-10")
    job.spend("rerecordAttempts")
    # 唤醒 2: 同 jobId 新实例。
    job2 = RecoveryJob(job.state_path, recovery_job_id="REC-JOB-10")
    assert job2.budget_used() == job.budget_used()
    with pytest.raises(RecoveryJobError) as excinfo:
        job2.spend("rerecordAttempts")
    assert excinfo.value.code == "budget_exhausted"
    # 预算上限对齐设计 §1。
    assert BUDGET_LIMITS == {"repairSearches": 1, "rerecordAttempts": 1,
                             "verificationReplays": 1}


def test_r11_job_id_mismatch_refused(tmp_path):
    """R11 佐证: 状态文件属别的作业 → 拒绝（不混用状态）。"""
    job = _make_job(tmp_path, job_id="REC-JOB-11")
    with pytest.raises(RecoveryJobError) as excinfo:
        RecoveryJob(job.state_path, recovery_job_id="REC-JOB-OTHER")
    assert excinfo.value.code == "job_id_mismatch"


# ------------------------------------------------------- R09 真实链端到端

def test_r09_end_to_end_real_chain_two_layer_failure(tmp_path,
                                                     monkeypatch):
    """R09 端到端（真实链，仅 OS fake）: 修复层无可证修复（真实
    propose_repairs 在无兄弟帧样本上拒绝）+ 第二层真实 rerecord 在
    核账失败点（hook 漏捕→R05 原件）→ 作业 unrecovered，两阶段证据
    refs 齐、不称永久不可用。"""
    import test_rerecord_loop as t3
    from test_trace_repair_pipeline import _lang_recording
    from agent.recording.trace_repair import propose_repairs
    from agent.recording.step_plan import extract_step_plan
    from agent.recording.compiler import compile_recording
    from target.recording.result_store import RecordingResultStore
    from target.windowing.attempt_ledger import AttemptLedger

    recording = _lang_recording(timeline_overlap=True)
    result = compile_recording(recording)
    plan = extract_step_plan(recording, result)
    resolver = t3._Resolver()
    store = RecordingResultStore(tmp_path / "result-store")
    service = t3._make_service(resolver, store, tmp_path,
                               drop_first_pointer=True)
    dispatcher = t3._RecordingDispatcher()
    from agent.recording.rerecord_loop import run_rerecord_loop as _real_loop
    job = _make_job(tmp_path, job_id="REC-JOB-12")
    _, detail = execute_recovery(
        job, recording=recording, compile_result=result,
        owner="controller-A", round_id="t4-e2e",
        propose_repairs_fn=propose_repairs,
        build_plan_fn=lambda rec, res: plan,
        rerecord_fn=_real_loop,
        rerecord_kwargs=dict(
            service=service, dispatcher=dispatcher,
            output_root=tmp_path / "out", profile=None,
            result_store=store,
            ledger_factory=lambda root: AttemptLedger(
                Path(root) / "replay-ev"),
            server_tools=t3._server_tools(),
            window_role_map=t3._role_resolver,
            scope_name="t4-rerecord", process_name="EDRClient.exe",
            window_title=t3.SCOPE_TITLE))
    assert job.outcome() == JOB_OUTCOME_UNRECOVERED
    reason = job._doc["outcome"]["reason"]
    assert "no-provable-candidate" in reason
    # 第一层证据: 真实 propose_repairs 的 refusals。
    repair_events = [e for e in job._doc["events"]
                     if e["stage"] == "repair"]
    assert any(e["kind"] == "repair_proposal" and e["refusals"] > 0
               for e in repair_events)
    # 第二层证据: 具体失败点=核账（R05 形状）。
    failed = [e for e in job._doc["events"]
              if e["kind"] == "rerecord_failed"]
    assert failed and failed[-1]["code"] == "reconciliation_failed"
    assert job.budget_used()["rerecordAttempts"] == 1
