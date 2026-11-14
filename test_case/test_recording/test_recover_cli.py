"""CLI ``recover`` production entry (unified-v3 §5 recovery/JOB-<id>/ wiring).

收尾轮 P3(独立复验 2026-11-12「recovery 无生产入口」缺口):

- 显式 CLI 子命令 ``recover``:复用既有 recovery_job/rerecord_loop/
  trace_repair 模块与唯一受控执行链(execute_v3_window_stage_real),
  不新增恢复执行器。
- 无 GUI 授权(默认):只本地分析(真实 propose_repairs + 全点计划),
  零 GUI 派发;作业终态 awaiting_authorization;状态与输入输出引用落
  ``recovery/JOB-<id>/state.json``(RecoveryJob 持久文档)+ report.json。
- --allow-gui-rerecord:互斥门(recording_status 忙 → blocked)→ 修复
  候选经真实 v3 受控链验证 → 验证失败才进入既有重录;unknown 不自动重试。
- job_id 规则:``JOB-<uuid4hex32>``;state_path 归属:原 TRJ 夹
  ``recovery/JOB-<id>/``(taskKey+sourceRecording.sha256 确定性发现原
  TRJ;无原夹时落 ``<output_root>/recovery/`` 如实无 TRJ 关联)。

OS 事实层:全部用例无凭据本地适配(T3 fixture 真实 RecordingService
链 + 受约束 dispatcher 替身 + 真实 target.server 工具,仅 OS fake);
CLI 生产接线面经模块级工厂(monkeypatch 替换 MCP 载体构造,链路本体
零 mock)。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(Path(__file__).parent))

from agent import cli  # noqa: E402
import test_rerecord_loop as t3  # noqa: E402
from test_trace_repair_pipeline import _lang_recording  # noqa: E402

pytestmark = pytest.mark.unit


class _RecoverAgent:
    """CLI recover 的 agent 替身(与 test_replay._MCPAgent 同法):
    recording_status 探针返回 idle;v3 受控链工具转发到真实
    target.server 模块(OS 事实层由各用例的 fake 安装)。"""

    def __init__(self):
        self.calls = []
        self.status_state = "idle"

    def ensure_ready(self):
        return {"ok": True}

    def call_tool(self, name, arguments=None, timeout=None):
        self.calls.append((name, arguments))
        if name == "recording_status":
            return {"ok": True, "state": self.status_state}
        import target.server as _server
        if hasattr(_server, name):
            import json as _json
            fn = getattr(_server, name)
            try:
                return _json.loads(fn(**(arguments or {})))
            except TypeError as exc:
                return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        raise AssertionError(f"unexpected tool call in recover test: {name}")


@pytest.fixture(autouse=True)
def _recover_agent(monkeypatch):
    agent = _RecoverAgent()
    monkeypatch.setattr(cli, "_target_agent", lambda *_args: agent)
    return agent


@pytest.fixture(autouse=True)
def _fake_target_config(monkeypatch):
    """CLI 目标解析替身:win-dev → windows_hisec(与 test_replay 同法)。"""
    class _TargetConfig:
        def __init__(self, _path=None):
            pass

        def get_target_app_profile(self, name):
            return "windows_hisec"

        def get_default_target(self):
            return "win-dev"

    monkeypatch.setattr(cli, "TargetConfig", _TargetConfig)


def _write_weak_recording(tmp_path: Path) -> Path:
    """真实弱身份语料(语言菜单流,compile incomplete,修复层的正型输入)
    落盘 recording.json;同轮先 record compile 留下原 TRJ 夹(recover 的
    origin 发现锚)。"""
    from agent.recording.compiler import compile_recording
    from agent.recording import artifacts

    recording = _lang_recording()
    result = compile_recording(recording)
    assert result.golden.status == "incomplete"
    output_root = tmp_path / "record"
    output_root.mkdir(parents=True, exist_ok=True)
    source = output_root / "recording.json"
    source.write_text(json.dumps(recording.to_dict(), ensure_ascii=False),
                      encoding="utf-8")
    artifacts.write_compilation_artifacts(tmp_path, recording, result)
    return source


def _origin_trj(output_root: Path) -> Path:
    trajectories = output_root / "trajectories"
    found = sorted(p for p in trajectories.iterdir()
                   if p.name.startswith("TRJ-")) if trajectories.is_dir() else []
    assert len(found) == 1, found
    return found[0]


def _install_r01_validation_os(monkeypatch):
    """验证回放的 OS 事实层(R01 正型:入口 logo1 + 三个顺序重开实例,
    语言结果控件 French)——从 test_trace_repair_pipeline 的 R01 用例
    提取的最小等价安装(仅 OS fake;受控链全真实)。"""
    import copy

    import test_v3_real_server_adapter as adapter
    from test_trace_repair_pipeline import CHAIN, _ParentAwareBackend

    backend = _ParentAwareBackend({})
    backend._windows = {
        "50": {"handle": "50", "pid": 42, "title": "logo1",
               "visible": True, "foreground": True,
               "process_name": "EDRClient.exe",
               "root_control_id": "EdrMainWindow"},
        "100": {"handle": "100", "pid": 42, "title": "EDRClient-1",
                "visible": True, "foreground": True,
                "process_name": "EDRClient.exe",
                "parent_native_id": "50"},
    }
    backend._current_handle = "100"

    def _translated_dump(*args, **kwargs):
        tree = adapter._FakeOSBackend.dump_tree(backend, *args, **kwargs)
        native = str(backend._lock_state.get("handle"))
        if native == "50":
            tree["controls"].append({
                "class_name": "Button", "text": "Open", "control_id": 5,
                "automation_id": "EdrMainWindow.openBtn",
                "rectangle": {"x": 100, "y": 100, "w": 80, "h": 32},
                "is_visible": True, "is_enabled": True, "depth": 1})
            tree["controls"].append({
                "class_name": "Text", "text": "French", "control_id": 30,
                "automation_id": "currentLanguage",
                "rectangle": {"x": 100, "y": 20, "w": 120, "h": 20},
                "is_visible": True, "is_enabled": True, "depth": 1})
            return tree
        for i, (text, top) in enumerate(
                (("Setting Center", 76), ("Logs", 108), ("Idioma", 140),
                 ("About", 172))):
            tree["controls"].append({
                "class_name": "MenuItem", "text": text, "control_id": 20 + i,
                "automation_id": "", "rectangle": {
                    "x": 1000, "y": top, "w": 153, "h": 32},
                "is_visible": True, "is_enabled": True, "depth": 1,
                "ancestry": copy.deepcopy(CHAIN)})
        return tree

    backend.dump_tree = _translated_dump
    original_click = backend.click
    rotation = iter(("200", "300", None))

    def click_with_rotation(**kwargs):
        result = original_click(**kwargs)
        current = (str(backend._lock_state.get("handle"))
                   if backend._lock_state else backend._current_handle)
        if current != "50":
            nxt = next(rotation, None)
            backend._windows.pop(current, None)
            if nxt is not None:
                backend._windows[nxt] = {
                    "handle": nxt, "pid": 42,
                    "title": f"EDRClient-{nxt[0]}",
                    "visible": True, "foreground": True,
                    "process_name": "EDRClient.exe",
                    "parent_native_id": "50"}
                backend._current_handle = nxt
        return result

    backend.click = click_with_rotation
    adapter._install_fake_os(monkeypatch, backend)
    return backend


def _patch_local_recovery_chain(monkeypatch, tmp_path, *,
                                validate_candidate=None,
                                rerecord_service=None,
                                rerecord_dispatcher=None,
                                server_tools=None,
                                role_map=None):
    """把 CLI 的 MCP 载体工厂替换成本地无凭据适配(T3 fixture);
    production chain(compile/propose/execute_recovery/RecoveryJob)零替换。"""
    calls = {"validation": [], "rerecord": []}

    def _validation_replay(candidate_golden, *, agent, profile, trace_root,
                           timeout):
        calls["validation"].append(candidate_golden.status)
        from agent.recording.execution_ledger import (
            execute_v3_window_stage_real,
        )
        from target.windowing.attempt_ledger import AttemptLedger
        import target.server as server
        outcome = execute_v3_window_stage_real(
            golden=candidate_golden, ledger=AttemptLedger(tmp_path / "val-ev"),
            server_tools=server_tools if server_tools is not None else server,
            window_role_map=role_map or {"W1": "main", "W2": "dialog",
                                         "W3": "dialog", "W4": "dialog"})
        report = outcome.get("report") or {}
        return (outcome.get("latched") is False
                and report.get("outcome") == "pass")

    monkeypatch.setattr(cli, "_recover_validation_replay", _validation_replay)
    if rerecord_service is not None:
        monkeypatch.setattr(
            cli, "_recover_recording_service",
            lambda agent: rerecord_service)
        monkeypatch.setattr(
            cli, "_recover_dispatcher", lambda agent: rerecord_dispatcher)
        from target.recording.result_store import RecordingResultStore
        monkeypatch.setattr(
            cli, "_recover_result_store",
            lambda: RecordingResultStore(tmp_path / "result-store"))
    return calls


def test_recover_cli_entry_analyzes_and_blocks_without_gui_authorization(
        tmp_path, monkeypatch, capsys):
    """未授权(默认):真实 compile + 真实 propose_repairs/step_plan 分析
    落 job 文档;终态 awaiting_authorization;零 GUI(replay/rerecord 零
    调用);state.json + report.json 落原 TRJ 夹 recovery/JOB-<id>/。"""
    dispatcher = t3._RecordingDispatcher()
    _patch_local_recovery_chain(monkeypatch, tmp_path,
                                rerecord_service=object(),  # 不该被用
                                rerecord_dispatcher=dispatcher)
    output_root = tmp_path / "record"
    source = _write_weak_recording(output_root)
    origin = _origin_trj(output_root)

    exit_code = cli.main([
        "--target", "win-dev", "recover", str(source),
        "--output-root", str(output_root),
    ])
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 1, output
    assert output["ok"] is False
    assert output["outcome"] == "awaiting_authorization"
    assert output["authorizationBlocked"] is True
    # 零 GUI:重录从未启动,dispatcher 零调用。
    assert output["rerecordStarted"] is False
    assert dispatcher.calls == []
    # 零 GUI:验证回放零执行(候选只被静态分析记录)。
    assert output["validationReplays"] == []
    # 状态落原 TRJ 夹 recovery/JOB-<id>/。
    assert output["originTrajectory"] == origin.name
    job_dirs = list((origin / "recovery").glob("JOB-*")) \
        if (origin / "recovery").is_dir() else []
    assert len(job_dirs) == 1, job_dirs
    state_path = job_dirs[0] / "state.json"
    assert state_path.is_file()
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["recoveryJobId"] == output["recoveryJobId"]
    assert state["outcome"]["label"] == "awaiting_authorization"
    # 本地分析证据真实记录(修复提案 + 全点计划)。
    kinds = [e["kind"] for e in state["events"]]
    assert "analysis_repair_proposal" in kinds
    assert "analysis_step_plan" in kinds
    assert not any(e["kind"] == "rerecord_started" for e in state["events"])
    # 零 GUI:验证回放零执行(候选只被静态分析记录)。
    assert output["validationReplays"] == []
    # 报告面:blocked 阶段如实记录(零恢复层执行),报告落盘。
    report = json.loads((job_dirs[0] / "report.json").read_text(
        encoding="utf-8"))
    assert report["outcome"] == "awaiting_authorization"
    assert report["stagesRun"] == ["blocked"]
    assert "rerecord" in report["stagesNotRun"]
    # origin 关联:引用含 TRJ/修订与 raw 摘要。
    assert state["originArtifactRef"]["trajectoryId"] == origin.name
    assert state["originArtifactRef"]["revision"] == "revisions/REV-0001"
    assert state["originArtifactRef"]["sha256"].startswith("sha256:")


def test_recover_cli_repair_success_recovers_by_repair(tmp_path, monkeypatch,
                                                       capsys):
    """修复成功分支(真实链):授权 + 本地 T3 service/dispatcher 注入;
    候选经真实 v3 受控链验证(T3 fake-OS 语言链正型)通过 →
    recovered_by_repair;重录零调用(repair 命中不进 rerecord)。"""
    from target.recording.result_store import RecordingResultStore

    recording = _lang_recording()
    resolver = t3._Resolver()
    store = RecordingResultStore(tmp_path / "result-store")
    service = t3._make_service(resolver, store, tmp_path)
    dispatcher = t3._RecordingDispatcher()
    _install_r01_validation_os(monkeypatch)
    calls = _patch_local_recovery_chain(
        monkeypatch, tmp_path, rerecord_service=service,
        rerecord_dispatcher=dispatcher)
    source = tmp_path / "recording.json"
    source.write_text(json.dumps(recording.to_dict(), ensure_ascii=False),
                      encoding="utf-8")
    try:
        exit_code = cli.main([
            "--target", "win-dev", "recover", str(source),
            "--output-root", str(tmp_path / "record"),
            "--allow-gui-rerecord",
        ])
    finally:
        t3._reset_replay_env()
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 0, output
    assert output["outcome"] == "recovered_by_repair", output
    assert len(calls["validation"]) == 1
    assert calls["validation"][0] == "ready"
    # 修复命中:重录层从未启动(dispatcher 只被验证回放的受控链用过,
    # rerecord 的 plan dispatcher 零调用 → recording session 零启动)。
    assert output["rerecordStarted"] is False
    assert output["stagesRun"] == ["repair", "verification"]
    # 状态落盘可追溯。
    report = json.loads(Path(output["reportPath"]).read_text(
        encoding="utf-8"))
    assert report["outcome"] == "recovered_by_repair"


def test_recover_cli_repair_failure_enters_rerecord_branch(tmp_path,
                                                           monkeypatch,
                                                           capsys):
    """修复失败 → 既有重录分支(真实 run_rerecord_loop + T3 本地链,
    R02 正型):先录后做、新 compile ready、新 run 独立回放通过 →
    recovered_by_rerecord;rerecord 恰启动一次。"""
    from target.recording.result_store import RecordingResultStore

    recording = _lang_recording()
    resolver = t3._Resolver()
    store = RecordingResultStore(tmp_path / "result-store")
    service = t3._make_service(resolver, store, tmp_path)
    dispatcher = t3._RecordingDispatcher()
    t3._install_replay_os(monkeypatch, resolver)
    calls = _patch_local_recovery_chain(
        monkeypatch, tmp_path, rerecord_service=service,
        rerecord_dispatcher=dispatcher,
        # 验证失败注入:真实受控链跑完但结果判 False —— 候选验证不过
        # 才进入重录(§4:验证失败记录原因后进入步骤提取)。
        )
    # 用 patch 点让验证回放判定失败(仍走真实链,只是断言面收紧为
    # 语言控件实况与原期望不符的等价形状——T3 _Resolver(language_value=...)
    # 已提供该形状;这里直接用 resolver 变体)。
    source = tmp_path / "recording.json"
    source.write_text(json.dumps(recording.to_dict(), ensure_ascii=False),
                      encoding="utf-8")

    # 验证失败:用一个真实但语言值不符的 fake OS(候选回放时语言断言
    # failed)→ validate False → 重录(R02 正型 fake 是 French 一致;
    # 失败形状用 validation 判定 False 的注入替代,链路真实不变)。
    import agent.recording.execution_ledger as ledger_mod
    from target.windowing.attempt_ledger import AttemptLedger
    import target.server as server

    def _failing_validation(candidate_golden, *, agent, profile, trace_root,
                            timeout):
        calls["validation"].append(candidate_golden.status)
        outcome = ledger_mod.execute_v3_window_stage_real(
            golden=candidate_golden,
            ledger=AttemptLedger(tmp_path / "val-ev"),
            server_tools=server,
            window_role_map={"W1": "main", "W2": "dialog", "W3": "dialog",
                             "W4": "dialog"})
        return False  # 真实链跑完但候选验证判定失败(注入点,链路真实)

    monkeypatch.setattr(cli, "_recover_validation_replay", _failing_validation)
    try:
        exit_code = cli.main([
            "--target", "win-dev", "recover", str(source),
            "--output-root", str(tmp_path / "record"),
            "--allow-gui-rerecord",
        ])
    finally:
        t3._reset_replay_env()
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 0, output
    assert output["outcome"] == "recovered_by_rerecord", output
    assert len(calls["validation"]) == 1
    assert output["rerecordStarted"] is True
    assert output["validationReplays"] == ["ready"]
    # 阶段事实:recovery_report 按层 stage_done 列已运行层(repair/
    # step_plan/rerecord);验证内嵌在重录层的 fresh_replay 阶段。
    assert output["stagesRun"] == ["repair", "step_plan", "rerecord"]
    assert output["stagesNotRun"] == ["verification"]
    # 新 run 关联写回 job state(validationRunRef/newRecordingRef)。
    state = json.loads(Path(output["statePath"]).read_text(
        encoding="utf-8"))
    assert state["validationRunRef"]["runId"], state.get("validationRunRef")


def test_recover_cli_desktop_busy_blocks_before_any_gui(_recover_agent,
                                                        tmp_path, monkeypatch,
                                                        capsys):
    """授权但目标忙(recording_status 活动)→ blocked,零 GUI 消耗
    (service.start 零调用、验证回放零调用);作业终态 blocked。"""
    from target.recording.result_store import RecordingResultStore

    recording = _lang_recording()
    resolver = t3._Resolver()
    store = RecordingResultStore(tmp_path / "result-store")
    service = t3._make_service(resolver, store, tmp_path)

    class _BusyService:
        def __getattribute__(self, name):
            if name == "start":
                raise AssertionError("busy target must never start recording")
            return object.__getattribute__(self, name)

    dispatcher = t3._RecordingDispatcher()
    _patch_local_recovery_chain(monkeypatch, tmp_path,
                                rerecord_service=_BusyService(),
                                rerecord_dispatcher=dispatcher)
    source = tmp_path / "recording.json"
    source.write_text(json.dumps(recording.to_dict(), ensure_ascii=False),
                      encoding="utf-8")

    # 互斥探针经 agent 替身返回忙状态;_BusyService 保证即使探针被绕过,
    # 任何 service.start 都会炸出来。
    _recover_agent.status_state = "recording"
    exit_code = cli.main([
        "--target", "win-dev", "recover", str(source),
        "--output-root", str(tmp_path / "record"),
        "--allow-gui-rerecord",
    ])
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 1, output
    assert output["outcome"] == "blocked"
    assert output["authorizationBlocked"] is True
    assert ("recording_status", {"heartbeat": False}) in _recover_agent.calls, \
        "mutual-exclusion probe must run"
    assert output["rerecordStarted"] is False


def test_recover_cli_unknown_interrupted_rerecord_never_refires(
        tmp_path, monkeypatch, capsys):
    """unknown 阻断:上次运行在重录中途中断(state 已有 rerecord_started
    无 stage_done)→ 重入同 jobId 终态 unknown;零自动重试零重放。"""
    from agent.recording.recovery_job import RecoveryJob
    from target.recording.result_store import RecordingResultStore

    recording = _lang_recording()
    resolver = t3._Resolver()
    service = t3._make_service(resolver, RecordingResultStore(
        tmp_path / "result-store"), tmp_path)
    dispatcher = t3._RecordingDispatcher()
    _patch_local_recovery_chain(monkeypatch, tmp_path,
                                rerecord_service=service,
                                rerecord_dispatcher=dispatcher)
    output_root = tmp_path / "record"
    source = tmp_path / "recording.json"
    source.write_text(json.dumps(recording.to_dict(), ensure_ascii=False),
                      encoding="utf-8")
    from agent.recording.compiler import compile_recording
    from agent.recording import artifacts
    result = compile_recording(recording)
    artifacts.write_compilation_artifacts(output_root, recording, result)
    origin = _origin_trj(output_root)
    state_dir = origin / "recovery" / ("JOB-" + "a" * 32)
    state_dir.mkdir(parents=True)
    job = RecoveryJob(state_dir / "state.json",
                      recovery_job_id="JOB-" + "a" * 32,
                      origin={"trajectoryId": origin.name},
                      failure_category="trace_defect",
                      failure_reason="compile_weak_identity_unresolved")
    job.record_event("rerecord", "rerecord_started", {})

    exit_code = cli.main([
        "--target", "win-dev", "recover", str(source),
        "--output-root", str(output_root),
        "--allow-gui-rerecord", "--job-id", "JOB-" + "a" * 32,
    ])
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 1, output
    assert output["outcome"] == "unknown"
    assert output["rerecordStarted"] is True  # 之前那次,非本次
    assert dispatcher.calls == [], "interrupted rerecord is never re-fired"
    state = json.loads((state_dir / "state.json").read_text(
        encoding="utf-8"))
    started = [e for e in state["events"] if e["kind"] == "rerecord_started"]
    assert len(started) == 1, started


def test_recover_cli_restart_rereads_job_state_and_budget(tmp_path,
                                                          monkeypatch,
                                                          capsys):
    """重启追溯:同一 jobId 重入已完成作业 → alreadyTerminal 如实返回,
    预算/事件零新增;报告可从磁盘重建。"""
    dispatcher = t3._RecordingDispatcher()
    _patch_local_recovery_chain(monkeypatch, tmp_path,
                                rerecord_service=object(),
                                rerecord_dispatcher=dispatcher)
    output_root = tmp_path / "record"
    source = _write_weak_recording(output_root)
    job_id = "JOB-" + "b" * 32

    first = cli.main([
        "--target", "win-dev", "recover", str(source),
        "--output-root", str(output_root), "--job-id", job_id,
    ])
    first_out = json.loads(capsys.readouterr().out)
    assert first_out["outcome"] == "awaiting_authorization"
    state_path = Path(first_out["statePath"])
    state_before = json.loads(state_path.read_text(encoding="utf-8"))

    second = cli.main([
        "--target", "win-dev", "recover", str(source),
        "--output-root", str(output_root), "--job-id", job_id,
    ])
    second_out = json.loads(capsys.readouterr().out)

    assert second_out["alreadyTerminal"] is True
    state_after = json.loads(state_path.read_text(encoding="utf-8"))
    assert len(state_after["events"]) == len(state_before["events"])
    assert state_after["budgetUsed"] == state_before["budgetUsed"]
