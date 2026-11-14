"""S2:golden v3 窗口序列与显式源事件映射(09 号设计 §5/6/9,§7.6 基础)。

失败回归先行。范围:
1. 显式 stepId→源事件稳定映射(§6.1):double-click/HOME/END/导航合并/
   独立与绑定断言/transition 折叠/cleanup 全覆盖,不 zip 猜;compile-report
   保留可追查 raw 引用(诊断)。
2. raw v2 默认产出严格 golden v3 + windowSequence:窗口声明(录制实例→稳定
   身份)、每步/每个 cleanup 步骤恰有一个 windowId 归属;effects 仅 kind=
   appeared/absent 且有稳定 id;selector.window 与 identity 完全一致
   (§5.1);entryWindow(如存在)与首步归属窗口一致,冲突拒绝。
3. 门槛:引用/唯一性/覆盖严格拒绝;证据不足保持 incomplete 且
   case/golden/编译诊断一致(§6.5);temporal 关联只诊断,不产生强 effects。
4. 兼容(§9):新 reader 读 golden v1/v2 保持既有行为;旧 reader 对 v3 显式
   拒绝;**v3 编译产物不可执行** —— S3/S4 未实现,所有执行/生成/导出路径
   必须能力拒绝(在任何 GUI 副作用前),不得丢 windowSequence 按旧 selector 执行。

窗口归属判定沿用 S1 已验收的实例不变量(同 handle+pid=同实例;
A→B→A 复用 W;同 HWND owner 变化=新实例)。
"""

from __future__ import annotations

import copy
import json

import pytest

from agent.recording.compiler import compile_recording
from agent.recording.models import (GOLDEN_TRACE_SCHEMA, GOLDEN_TRACE_SCHEMA_V2,
                                    GoldenTrace, RecordedStep, WindowSequence)
from target.recording.models import RawRecording

pytestmark = pytest.mark.unit


def _recording(events, windows=None, snapshots=None):
    return RawRecording.from_dict({
        "schema": "edr.desktop-recording/v2", "name": "flow",
        "sessionId": "REC-S2", "events": list(events),
        "controlSnapshots": list(snapshots or []),
        "captureDiagnostics": {"droppedPackets": 0, "correlationErrorCount": 0,
                                "recorderUiEvents": 0,
                                "unidentifiedTargetEvents": 0,
                                "outOfScopeEvents": 0},
        "rejections": [], "windows": list(windows or []),
    })


def _click(seq, ms, causal, handle, automation_id="btnApply", pid=42,
           instance_id=None, snapshot_id="S1", title="logo1",
           window_title=None):
    return {
        "sequence": seq, "type": "pointer_click",
        "wallTime": "2026-11-03T00:00:00Z", "monotonicMs": ms,
        "causalId": causal,
        "scope": {"backend": "windows_pywinauto",
                  "processName": "EDRClient.exe", "target": "target",
                  "windowTitle": window_title or "^logo1$"},
        "input": {"button": "left"},
        "observedTarget": {"automationId": automation_id,
                           "controlType": "Button", "identifier": None,
                           "fingerprint": "sha256:abc", "text": "Apply",
                           "rect": [0, 0, 10, 10], "ancestry": [],
                           "snapshotId": snapshot_id, "targetId": f"T{seq:04d}",
                           "protected": None, "sameNameSiblings": None},
        "evidence": {"window": {"handle": handle, "title": title,
                                 "processName": "EDRClient.exe",
                                 **({"instanceId": instance_id}
                                    if instance_id else {})},
                     "foregroundPid": pid},
        "assertion": None,
    }


def _transition(seq, ms, kind, causal, handle, title):
    return {
        "sequence": seq, "type": "window_transition",
        "wallTime": "2026-11-03T00:00:00Z", "monotonicMs": ms,
        "causalId": causal,
        "scope": {"backend": "windows_pywinauto",
                  "processName": "EDRClient.exe", "target": "target",
                  "windowTitle": "^logo1$"},
        "input": {"kind": kind, "title": title, "processName": "EDRClient.exe",
                  "timeoutSeconds": 10.0},
        "observedTarget": None,
        "evidence": {"handle": handle},
        "assertion": None,
    }


_MAIN = {"title": "logo1", "processName": "EDRClient.exe", "handle": 100,
         "pid": 42, "origin": "already_open", "closed": False,
         "instanceId": "RI-0001"}
_DIALOG = {"title": "日志中心", "processName": "EDRClient.exe", "handle": 200,
           "pid": 42, "origin": "opened", "closed": False,
           "instanceId": "RI-0002",
           "openedBy": "CAUSE-1"}


def _snap(snap_id, ms, handle, root, controls, complete=True, title="logo1"):
    return {"snapshotId": snap_id, "capturedAtMs": ms, "reason": "step",
            "window": {"handle": handle, "processName": "EDRClient.exe",
                       "rootAutomationId": root, "title": title},
            "contentHash": "sha256:" + "a" * 64,
            "stateHash": "sha256:" + "b" * 64,
            "truncated": False, "complete": complete,
            "controlCount": len(controls), "controls": list(controls)}




def _with_timeline(raw, instances=("RI-0001", "RI-0002"), ms=10):
    """最小合法 timeline(覆盖门要求);instance 存活贯穿全流程。"""
    import dataclasses
    events = [
        {"id": f"WE{i}", "order": i, "monotonicMs": ms + (i - 1),
         "instanceId": ri, "kind": "initial_observed",
         "source": "initial_inventory"}
        for i, ri in enumerate(instances, start=1)]
    return dataclasses.replace(raw, window_timeline={
        "schema": "edr.recorded-window-timeline/v1", "events": tuple(events),
        "coverage": {"destroyEvents": "supported", "droppedEvents": 0}})

# ---- 1:显式源事件映射 ----

def test_source_map_covers_merges_and_bindings():
    """§6.1:合并/断言/transition 折叠后的每一步都能追到 raw 事件序号,
    不靠列表位置猜。compile-report diagnostics 保留映射。"""
    events = [
        _click(1, 100, "CAUSE-1", 100),                      # 主窗口点击
        _transition(2, 150, "opened", "CAUSE-1", 200, "日志中心"),
        _click(3, 300, "CAUSE-2", 200, "logRow"),            # 对话框内点击
    ]
    result = compile_recording(
        _with_timeline(_recording(events, windows=[_MAIN, _DIALOG])),
        profile="windows_hisec")
    mapping = result.diagnostics.get("sourceEvents")
    assert mapping, "编译诊断必须包含显式源事件映射"
    step_map = mapping["byStep"] if "byStep" in mapping else mapping
    steps = result.golden.steps
    assert len(steps) >= 2
    # 每个步骤的映射都是非空 raw 序号列表,且并集覆盖全部已编译事件序号。
    seen = set()
    for step in steps:
        refs = step_map.get(step.step_id)
        assert refs, f"{step.step_id} 缺源事件引用"
        for s in refs:
            seen.add(int(s))
    # §6.1(0747 修订):绑定 transition 的 raw 序号归属于它验证的动作步骤
    # —— 全部 raw 事件可追查,不再"折叠不占步"地丢失。
    assert seen == {1, 2, 3}, f"映射应覆盖动作与绑定事件: {seen}"


def test_double_click_maps_both_source_sequences():
    """双击 coalesce 成 pointer_double_click 单事件(seq=1,第二击被吸收为
    同一事件的证据帧)— 映射如实记录它的事实来源;合并事实通过事件类型
    pointer_double_click 可识别,不靠 zip 猜。"""
    events = [
        _click(1, 100, "CAUSE-1", 100),
        _click(2, 150, "CAUSE-1", 100),  # 双击第二击:coalesce 吸收
    ]
    result = compile_recording(
        _with_timeline(_recording(events, windows=[_MAIN]),
                       instances=("RI-0001",)),
        profile="windows_hisec")
    mapping = result.diagnostics.get("sourceEvents")
    assert mapping
    step_map = mapping["byStep"] if "byStep" in mapping else mapping
    all_refs = {int(r) for refs in step_map.values() for r in refs}
    assert all_refs == {1}, (
        f"coalesce 后唯一事件的事实来源必须可追查,实际 {step_map}")
    assert result.golden.steps[0].action_id == "pointer.double_click", (
        "双击编译为动作目录内的 double_click 动作;coalesce 事实可由事件追查")


# ---- 2:v3 windowSequence ----

def test_raw_v2_compiles_to_v3_with_window_sequence():
    """raw v2 默认产出 v3:窗口声明 + 每步归属;selector-identity 一致。"""
    events = [
        _click(1, 100, "CAUSE-1", 100, instance_id="RI-0001"),
        _transition(2, 150, "opened", "CAUSE-1", 200, "日志中心"),
        _click(3, 300, "CAUSE-2", 200, "logRow", instance_id="RI-0002"),
    ]
    result = compile_recording(
        _with_timeline(_recording(events, windows=[_MAIN, _DIALOG])),
        profile="windows_hisec")
    golden = result.golden
    assert golden.schema == "edr.desktop-golden-trace/v3"
    assert golden.window_sequence is not None
    seq = golden.window_sequence.to_dict()
    assert {w["id"] for w in seq["windows"]} >= {"W1", "W2"}
    w1 = next(w for w in seq["windows"] if w["id"] == "W1")
    assert w1["identity"]["processName"] == "EDRClient.exe"
    assert w1["origin"] == "already_open"
    # 每步恰有一个归属。
    assert {s["windowId"] for s in seq["steps"]} <= {"W1", "W2"}
    assert len(seq["steps"]) == len(golden.steps)
    # 第一(主窗口)与对话框步骤归属不同 W。
    first = seq["steps"][0]["windowId"]
    dialog = next(s for s in seq["steps"] if s["windowId"] != first)
    assert dialog["windowId"] != first


def test_v3_selector_identity_consistency_rejected_on_mismatch():
    """§5.1:selector.window 与 identity 不一致(同 W 引用不同 root)→
    编译拒绝(incomplete + 诊断),不得静默。"""
    # 主窗口快照 root=MainRoot;步骤 3 在对话框(handle 200)但 selector 的
    # root 却是 MainRoot —— 由 handle→实例→root 关联校验捕捉。
    events = [
        _click(1, 100, "CAUSE-1", 100),
        _transition(2, 150, "opened", "CAUSE-1", 200, "日志中心"),
        _click(3, 300, "CAUSE-2", 200, "logRow"),
    ]
    snapshots = [_snap("CS-0001", 90, 100, "MainRoot",
                       [{"automationId": "EdrMainWindow", "controlType": "Window"}]),
                 # 对话框的快照声称 MainRoot —— 与主窗口快照同 root 冲突证据
                 _snap("CS-0002", 250, 200, "MainRoot",
                       [{"automationId": "LogCenterWindow", "controlType": "Window"}])]
    result = compile_recording(
        _recording(events, windows=[_MAIN, _DIALOG], snapshots=snapshots),
        profile="windows_hisec")
    # 同实例不同 root = compile_window_identity_conflict(S1 已有);
    # 这里校验:v3 产物(若产生)的 W 身份与 selector 一致,或如实 incomplete。
    assert result.golden.status == "incomplete"
    assert result.case.status == "incomplete"


# ---- 3:门槛 ----

def test_effects_only_from_explicit_assertion_not_temporal():
    """temporal_forward 窗口出现不产生强 appeared effect;只有显式断言
    (window_transition 绑定)才投影为 effects(§4.3/§5)。"""
    events = [
        _click(1, 100, "CAUSE-1", 100, instance_id="RI-0001"),
        _transition(2, 150, "opened", "CAUSE-1", 200, "日志中心"),
        _click(3, 300, "CAUSE-2", 200, "logRow", instance_id="RI-0002"),
    ]
    result = compile_recording(
        _with_timeline(_recording(events, windows=[_MAIN, _DIALOG])),
        profile="windows_hisec")
    seq = result.golden.window_sequence.to_dict()
    step1 = next(s for s in seq["steps"] if s["stepId"] == "step-0001")
    effects = step1.get("effects") or []
    for effect in effects:
        assert effect.get("verification") in {"explicit_assertion"}, (
            f"temporal 关联不得生成强 effects: {effect}")


def test_insufficient_evidence_stays_incomplete_consistently():
    """title-only 步骤无 root/complete 证据 → incomplete 且三方一致
    (supervised_1102b 定位)。"""
    import os
    path = "recordings/supervised_1102b/recording.json"
    if not os.path.exists(path):
        pytest.skip("历史样本不在本环境")
    from target.recording.models import RawRecording as Raw
    recording = Raw.from_dict(json.load(open(path)))
    result = compile_recording(recording, profile="windows_hisec")
    assert result.golden.status == "incomplete"
    assert result.case.status == "incomplete"
    # step-0009 弱控件身份缺口如实保留。
    s9 = next(s for s in result.golden.steps if s.step_id == "step-0009")
    assert "compile_weak_identity_unresolved" in s9.issues


# ---- 4:兼容与执行拒绝门 ----

def test_v3_reader_accepts_and_v1_reader_rejects():
    """新 reader 读 v3;旧(当前)reader 的 allowed 集不含 windowSequence →
    显式拒绝。"""
    v3 = GoldenTrace.from_dict({
        "schema": "edr.desktop-golden-trace/v3", "status": "ready",
        "name": "flow",
        "sourceRecording": {"schema": "edr.desktop-recording/v2",
                            "sha256": "sha256:raw"},
        "catalog": {"version": "1.0.0", "digest": "sha256:c"},
        "environment": {"backend": "windows_pywinauto", "target": "t"},
        "entry": "step-0001",
        "steps": {"step-0001": {
            "stepId": "step-0001", "actionId": "gui.click", "args": {},
            "selector": {"window": {"processName": "EDRClient.exe",
                                    "titleRegex": "^logo1$"},
                         "control": {"automationId": "b"}, "visual": None},
            "endSelector": None, "verifiers": [], "required": True,
            "status": "ready", "issues": []}},
        "cleanup": [],
        "windowSequence": {
            "schema": "edr.window-sequence/v1",
            "windows": [{"id": "W1",
                         "identity": {"processName": "EDRClient.exe",
                                      "titleRegex": "^logo1$"},
                         "origin": "already_open"}],
            "steps": [{"stepId": "step-0001", "windowId": "W1"}],
            "cleanup": [],
        },
    })
    assert v3.schema == "edr.desktop-golden-trace/v3"
    assert v3.window_sequence.steps[0]["windowId"] == "W1"
    # 旧 schema 文档(构造一个带 v3 字段的 v2 文档)被当前严格解析拒绝。
    with pytest.raises(Exception):
        GoldenTrace.from_dict({
            "schema": "edr.desktop-golden-trace/v2", "status": "ready",
            "name": "flow",
            "sourceRecording": {"schema": "edr.desktop-recording/v2",
                                "sha256": "sha256:raw"},
            "catalog": {"version": "1.0.0", "digest": "sha256:c"},
            "environment": {"backend": "windows_pywinauto", "target": "t"},
            "entry": "step-0001", "steps": {}, "cleanup": [],
            "windowSequence": {"schema": "edr.window-sequence/v1",
                               "windows": [], "steps": [], "cleanup": []},
        })


def test_v3_execution_paths_refuse_before_any_gui_side_effect():
    """§9/S2 边界:v3 编译产物不可执行 —— replay 入口在任何 GUI 副作用前
    能力拒绝;pytest 生成器显式 unsupported;不丢 windowSequence 执行。"""
    golden = GoldenTrace.from_dict({
        "schema": "edr.desktop-golden-trace/v3", "status": "ready",
        "name": "flow",
        "sourceRecording": {"schema": "edr.desktop-recording/v2",
                            "sha256": "sha256:raw"},
        "catalog": {"version": "1.0.0", "digest": "sha256:c"},
        "environment": {"backend": "windows_pywinauto", "target": "t",
                        "profile": "windows_hisec"},
        "entry": "step-0001",
        "steps": {"step-0001": {
            "stepId": "step-0001", "actionId": "gui.click", "args": {},
            "selector": {"window": {"processName": "EDRClient.exe",
                                    "titleRegex": "^logo1$"},
                         "control": {"automationId": "b"}, "visual": None},
            "endSelector": None, "verifiers": [], "required": True,
            "status": "ready", "issues": []}},
        "cleanup": [],
        "windowSequence": {
            "schema": "edr.window-sequence/v1",
            "windows": [{"id": "W1",
                         "identity": {"processName": "EDRClient.exe",
                                      "titleRegex": "^logo1$"},
                         "origin": "already_open"}],
            "steps": [{"stepId": "step-0001", "windowId": "W1"}],
            "cleanup": [],
        },
    })

    class _CapableAgent:
        def tools_list(self):
            return {"tools": [{"name": n} for n in (
                "begin_product_run", "end_product_run",
                "observe_window_bundle", "focus_window_role",
                "focus_window_selector", "focus_entry_window")]}

        state = None

        def call_tool(self, name, args=None, timeout=None):
            raise AssertionError("v3 在 S3/S4 前不得有任何目标调用")

    from agent.recording.runtime import build_replay_runtime
    from target.recording.models import RecordingModelError
    with pytest.raises(RecordingModelError) as excinfo:
        build_replay_runtime(_CapableAgent(), golden,
                             profile="windows_hisec", entry_recovery=True)
    assert excinfo.value.code == "window_sequence_execution_unsupported"


def test_generated_pytest_refuses_v3():
    """生成/写出层对 v3 显式 unsupported;核心产物先完整落盘。"""
    from agent.recording import artifacts as artifacts_mod
    import tempfile
    from pathlib import Path
    recording = _with_timeline(
        _recording(
            [_click(1, 100, "CAUSE-1", 100, instance_id="RI-0001")],
            windows=[_MAIN]), instances=("RI-0001",))
    result = compile_recording(recording, profile="windows_hisec")
    assert result.golden.schema == "edr.desktop-golden-trace/v3"
    with tempfile.TemporaryDirectory() as tmp:
        artifacts_mod.write_compilation_artifacts(
            Path(tmp) / "out", recording, result)
        files = sorted(p.name for p in (Path(tmp) / "out").rglob("*")
                       if p.is_file())
        assert "compile-report.json" in files
        pytest_files = [f for f in files if f.startswith("test_")]
        for pf in pytest_files:
            content = (Path(tmp) / "out" / "flow" / pf).read_text()
            executable = [ln for ln in content.splitlines()
                          if ln.strip() and not ln.strip().startswith("#")]
            assert not executable, f"占位必须不可执行: {pf}"
        report = json.load(open(
            Path(tmp) / "out" / "flow" / "compile-report.json"))
        assert report.get("pytestExport", {}).get("supported") is False


# ---- 0747 复验反例(先红后绿,第二轮修复)----

def dataclasses_replace_input(raw, seq, new_input, assertion=None):
    """Replace input (and optionally the assertion) of the given sequence."""
    import dataclasses
    events = []
    for event in raw.events:
        if event.sequence == seq:
            event = dataclasses.replace(event, input=new_input,
                                        assertion=assertion)
        events.append(event)
    return dataclasses.replace(raw, events=tuple(events))


def _two_generation_recording():
    import dataclasses
    a = _click(1, 100, "C1", 100, pid=42)
    b = _click(2, 300, "C2", 100, pid=99)
    a["evidence"]["window"]["instanceId"] = "RI-0001"
    b["evidence"]["window"]["instanceId"] = "RI-0002"
    windows = [dict(_MAIN),
               {**_MAIN, "instanceId": "RI-0002", "pid": 99,
                "origin": "opened"}]
    raw = _recording([a, b], windows=windows)
    return dataclasses.replace(raw, window_timeline={
        "schema": "edr.recorded-window-timeline/v1", "events": (
            {"id": "WE1", "order": 1, "monotonicMs": 50,
             "instanceId": "RI-0001", "kind": "initial_observed",
             "source": "initial_inventory"},
            {"id": "WE2", "order": 2, "monotonicMs": 250,
             "instanceId": "RI-0002", "kind": "identity_changed",
             "source": "native_identity_change"}),
        "coverage": {"destroyEvents": "supported", "droppedEvents": 0}})


def test_0747_generations_are_distinct_windows_not_ready():
    """0747 P1-1:同 HWND 两代必须两个 W;coverage unknown/丢包不得 ready。"""
    result = compile_recording(_two_generation_recording(),
                               profile="windows_hisec")
    seq_dict = result.golden.window_sequence.to_dict()
    ids = [s["windowId"] for s in seq_dict["steps"]]
    assert ids[0] != ids[1], f"两代必须两个 W,实际 {seq_dict['windows']}"
    import dataclasses
    degraded = dataclasses.replace(
        _two_generation_recording(),
        window_timeline={**_two_generation_recording().window_timeline,
                         "coverage": {"destroyEvents": "unknown",
                                      "droppedEvents": 9}})
    degraded_result = compile_recording(degraded, profile="windows_hisec")
    assert degraded_result.golden.status == "incomplete"


def test_0747_source_map_keeps_coalesce_and_binding_provenance():
    """0747 P1-2:全部 raw 事件(含 coalesce 吸收/continue 分支)可追查。"""
    events = [
        _click(1, 100, "CAUSE-1", 100, instance_id="RI-0001"),
        _click(2, 150, "CAUSE-1", 100, instance_id="RI-0001"),
        _transition(3, 150, "opened", "CAUSE-1", 200, "日志中心"),
        _click(4, 300, "CAUSE-2", 200, "logRow", instance_id="RI-0002"),
    ]
    result = compile_recording(
        _with_timeline(_recording(events, windows=[_MAIN, _DIALOG])),
        profile="windows_hisec")
    mapping = result.diagnostics["sourceEvents"]["byStep"]
    all_refs = {int(r) for refs in mapping.values() for r in refs}
    merged = result.diagnostics.get("coalescedSources") or {}
    traced = all_refs | {int(s) for refs in merged.values() for s in refs}
    assert {1, 2, 3, 4} <= traced, (
        f"§6.1 全 raw 可追查,实际 {mapping} / {merged}")


def test_0747_effects_require_explicit_and_never_dropped():
    """0747 P1-3:temporal 不生成 effect;显式断言不因声明顺序被丢。"""
    events = [
        _click(1, 100, "C1", 100, instance_id="RI-0001"),
        _transition(2, 150, "opened", "C1", 100, "logo1"),
    ]
    result = compile_recording(
        _with_timeline(_recording(events, windows=[_MAIN]),
                       instances=("RI-0001",)),
        profile="windows_hisec")
    assert result.golden.window_sequence is not None
    seq_dict = result.golden.window_sequence.to_dict()
    for entry in seq_dict["steps"]:
        for effect in entry.get("effects") or []:
            raise AssertionError(f"temporal 不得生成强 effect: {effect}")
    raw = _with_timeline(_recording([
        _click(1, 100, "CAUSE-1", 100, instance_id="RI-0001"),
        _transition(2, 150, "opened", "CAUSE-1", 200, "日志中心"),
        _click(3, 300, "CAUSE-2", 200, "logRow", instance_id="RI-0002"),
    ], windows=[_MAIN, _DIALOG]))
    raw = dataclasses_replace_input(raw, 2, {
        "kind": "opened", "title": "日志中心",
        "processName": "EDRClient.exe", "timeoutSeconds": 10.0},
        assertion={"type": "window_open",
                   "expected": {"exists": True, "title": "日志中心",
                                "processName": "EDRClient.exe"}})
    result2 = compile_recording(raw, profile="windows_hisec")
    seq2 = result2.golden.window_sequence.to_dict()
    effects = [e for s in seq2["steps"] for e in (s.get("effects") or [])]
    assert effects, f"显式断言不得被静默丢弃,实际 {seq2['steps']}"
    assert all(e["verification"] == "explicit_assertion" for e in effects)


def test_0747_direct_replay_and_test_case_refuse_v3():
    """0747 P1-4:公开 replay_golden_trace 与 golden_to_test_case 拒绝 v3。"""
    import dataclasses
    import runpy
    replay_fixture = runpy.run_path("test_case/test_recording/test_replay.py")
    step = RecordedStep("step-0001", "gui.click", {},
                        replay_fixture["_selector"]())
    golden = dataclasses.replace(
        replay_fixture["_golden"](step),
        schema="edr.desktop-golden-trace/v3",
        window_sequence=WindowSequence(
            "edr.window-sequence/v1",
            ({"id": "W1", "identity": dict(step.selector.window),
              "origin": "already_open"},),
            ({"stepId": "step-0001", "windowId": "W1"},)))
    runtime, observations, calls = replay_fixture["_runtime"]([
        {"process_name": "EDRClient.exe", "window_title": "EDRClient",
         "automation_id": "btnApply", "control_type": "Button",
         "text": "应用", "fingerprint": "sha256:current"}])
    from agent.recording.replay import replay_golden_trace, golden_to_test_case
    from target.recording.models import RecordingModelError
    with pytest.raises(RecordingModelError) as excinfo:
        replay_golden_trace(runtime, golden)
    assert excinfo.value.code == "window_sequence_execution_unsupported"
    assert calls == [], "任何目标调用都不得发生"
    with pytest.raises(RecordingModelError):
        golden_to_test_case(golden)


def test_0747_artifacts_write_report_before_export_refusal():
    """0747 P1-4b:核心产物先完整落盘;可选导出占位不可执行+unsupported 标记。"""
    import tempfile
    from pathlib import Path
    from agent.recording import artifacts as artifacts_mod
    recording = _with_timeline(
        _recording(
            [_click(1, 100, "CAUSE-1", 100, instance_id="RI-0001")],
            windows=[_MAIN]), instances=("RI-0001",))
    result = compile_recording(recording, profile="windows_hisec")
    assert result.golden.schema == "edr.desktop-golden-trace/v3"
    with tempfile.TemporaryDirectory() as tmp:
        artifacts_mod.write_compilation_artifacts(
            Path(tmp) / "out", recording, result)
        files = sorted(p.name for p in (Path(tmp) / "out").rglob("*")
                       if p.is_file())
        assert "compile-report.json" in files
        for pf in [f for f in files if f.startswith("test_")]:
            content = (Path(tmp) / "out" / "flow" / pf).read_text()
            executable = [ln for ln in content.splitlines()
                          if ln.strip() and not ln.strip().startswith("#")]
            assert not executable, f"占位必须不可执行: {pf}"
        report = json.load(open(
            Path(tmp) / "out" / "flow" / "compile-report.json"))
        assert report.get("pytestExport", {}).get("supported") is False
        assert "sourceEvents" in report.get("diagnostics", {})


def test_0747_strict_model_rejects_malformed_v3():
    """0747 P2:selector/identity 不一致、非法 timeout、非字符串 identity、
    悬空引用 — 模型拒绝。"""
    import copy
    from target.recording.models import RecordingModelError
    base = GoldenTrace.from_dict({
        "schema": "edr.desktop-golden-trace/v3", "status": "ready",
        "name": "flow",
        "sourceRecording": {"schema": "edr.desktop-recording/v2",
                            "sha256": "sha256:raw"},
        "catalog": {"version": "1.0.0", "digest": "sha256:c"},
        "environment": {"backend": "windows_pywinauto", "target": "t"},
        "entry": "step-0001",
        "steps": {"step-0001": {
            "stepId": "step-0001", "actionId": "gui.click", "args": {},
            "selector": {"window": {"processName": "EDRClient.exe",
                                    "titleRegex": "^logo1$"},
                         "control": {"automationId": "b"}, "visual": None},
            "endSelector": None, "verifiers": [], "required": True,
            "status": "ready", "issues": []}},
        "cleanup": [],
        "windowSequence": {
            "schema": "edr.window-sequence/v1",
            "windows": [{"id": "W1",
                         "identity": {"processName": "EDRClient.exe",
                                      "titleRegex": "^logo1$"},
                         "origin": "already_open"}],
            "steps": [{"stepId": "step-0001", "windowId": "W1",
                       "effects": [{"id": "EF1", "kind": "appeared",
                                    "windowId": "W1",
                                    "verification": "explicit_assertion",
                                    "timeoutSeconds": 10}]}],
            "cleanup": [],
        },
    })
    good = base.to_dict()

    def mutated(mutator):
        data = copy.deepcopy(good)
        mutator(data)
        with pytest.raises(RecordingModelError):
            GoldenTrace.from_dict(data)

    mutated(lambda d: d["windowSequence"]["windows"][0].update(
        identity={"processName": "Other.exe", "titleRegex": "^Wrong$"}))
    mutated(lambda d: d["windowSequence"]["steps"][0]["effects"][0].update(
        timeoutSeconds=-999))
    mutated(lambda d: d["windowSequence"]["steps"][0]["effects"][0].update(
        timeoutSeconds=9999))
    mutated(lambda d: d["windowSequence"]["windows"][0]["identity"].update(
        titleRegex={"bad": "x"}))
    mutated(lambda d: d["windowSequence"]["steps"][0].update(windowId="W404"))


# ---- 0907 复验反例(先红后绿)----

def test_0907_lifecycle_evidence_gates_ready():
    """0907 P1-生命周期:缺 timeline/coverage、动作在 destroyed 之后、
    snapshot RI 不核对/多 root 冲突 —— 全部 incomplete。"""
    import dataclasses
    # a) raw v2 带 RI 但无 timeline/coverage → 不得 ready。
    bare = _recording([_click(1, 100, "C1", 100, instance_id="RI-0001")],
                      windows=[_MAIN])
    bare = dataclasses.replace(bare, window_timeline=None)
    result = compile_recording(bare, profile="windows_hisec")
    assert result.golden.status == "incomplete", (
        f"无 timeline/coverage 不得 ready,实际 {result.golden.status}")
    # b) RI1 10ms 首见 50ms destroyed,点击 100ms 仍引用 RI1 → incomplete。
    destroyed = dataclasses.replace(_two_generation_recording(),
                                    window_timeline={
        "schema": "edr.recorded-window-timeline/v1", "events": (
            {"id": "WE1", "order": 1, "monotonicMs": 10,
             "instanceId": "RI-0001", "kind": "initial_observed",
             "source": "initial_inventory"},
            {"id": "WE2", "order": 2, "monotonicMs": 50,
             "instanceId": "RI-0001", "kind": "destroyed",
             "source": "native_destroy"}),
        "coverage": {"destroyEvents": "supported", "droppedEvents": 0}})
    # 两 click:100ms 的动作在 destroyed 之后引用 RI1。
    dead_click = _click(1, 100, "C1", 100, pid=42)
    dead_click["evidence"]["window"]["instanceId"] = "RI-0001"
    raw_dead = _recording([dead_click], windows=[
        dict(_MAIN),
        {**_MAIN, "instanceId": "RI-0002", "pid": 99, "origin": "opened"}])
    raw_dead = dataclasses.replace(raw_dead, window_timeline=destroyed.window_timeline)
    result2 = compile_recording(raw_dead, profile="windows_hisec")
    assert result2.golden.status == "incomplete", (
        "销毁后引用旧 RI 的动作不得 ready")


def test_0907_real_assertion_entry_produces_effects():
    """0907 P1-断言:真实 type=assertion / assertion.type=window_open 的绑定
    分支必须产 source 与 effect(不由 transition 冒充)。"""
    assertion = _transition(2, 150, "opened", "C1", 100, "logo1")
    assertion["type"] = "assertion"
    assertion["assertion"] = {
        "type": "window_open",
        "expected": {"exists": True, "title": "logo1",
                     "processName": "EDRClient.exe"},
    }
    import dataclasses as _dc
    raw_assert = _recording([
        _click(1, 100, "C1", 100, instance_id="RI-0001"), assertion
    ], windows=[_MAIN])
    raw_assert = _dc.replace(raw_assert, window_timeline={
        "schema": "edr.recorded-window-timeline/v1", "events": (
            {"id": "WE1", "order": 1, "monotonicMs": 10,
             "instanceId": "RI-0001", "kind": "initial_observed",
             "source": "initial_inventory"},),
        "coverage": {"destroyEvents": "supported", "droppedEvents": 0}})
    result = compile_recording(raw_assert, profile="windows_hisec")
    verifiers = result.golden.steps[0].verifiers
    window_verifiers = [v for v in verifiers if v.get("type") == "window_open"]
    assert window_verifiers, "前置:真实断言已绑定 verifier"
    assert all(v.get("source") == "user_assertion"
               for v in window_verifiers), (
        f"真实断言必须标 user_assertion,实际 {window_verifiers}")
    effects = result.golden.window_sequence.to_dict()["steps"][0].get("effects")
    assert effects, f"真实断言必须生成 effect,实际 {effects}"


def test_0907_cancelled_transition_source_traceable():
    """0907 P2-来源:opposing(opened+closed)抵消 verifier 的 continue
    分支,raw3 也必须可追查。"""
    result = compile_recording(_recording([
        _click(1, 100, "C1", 100, instance_id="RI-0001"),
        _transition(2, 150, "opened", "C1", 100, "logo1"),
        _transition(3, 170, "closed", "C1", 100, "logo1"),
    ], windows=[_MAIN]), profile="windows_hisec")
    mapping = result.diagnostics["sourceEvents"]["byStep"]
    merged = result.diagnostics.get("coalescedSources") or {}
    traced = {int(r) for refs in mapping.values() for r in refs}
    traced |= {int(s) for refs in merged.values() for s in refs}
    assert {1, 2, 3} <= traced, (
        f"opposing 抵消的 raw3 不得消失,实际 {mapping} / {merged}")


def test_0907_strict_partition_entry_and_identity_matching():
    """0907 P2-模型:steps/cleanup 分区、entryWindow 严格匹配、identity
    字段两边都有才可比且不一致拒绝、全局 effect id。"""
    import copy
    from target.recording.models import RecordingModelError
    base = GoldenTrace.from_dict({
        "schema": "edr.desktop-golden-trace/v3", "status": "ready",
        "name": "flow",
        "sourceRecording": {"schema": "edr.desktop-recording/v2",
                            "sha256": "sha256:raw"},
        "catalog": {"version": "1.0.0", "digest": "sha256:c"},
        "environment": {"backend": "windows_pywinauto", "target": "t"},
        "entry": "step-0001",
        "steps": {"step-0001": {
            "stepId": "step-0001", "actionId": "gui.click", "args": {},
            "selector": {"window": {"processName": "EDRClient.exe",
                                    "titleRegex": "^logo1$"},
                         "control": {"automationId": "b"}, "visual": None},
            "endSelector": None, "verifiers": [], "required": True,
            "status": "ready", "issues": []}},
        "cleanup": [],
        "entryWindow": {"schema": "edr.entry-window-identity/v1",
                        "processName": "EDRClient.exe",
                        "titleRegex": "^logo1$", "origin": "already_open"},
        "windowSequence": {
            "schema": "edr.window-sequence/v1",
            "windows": [{"id": "W1",
                         "identity": {"processName": "EDRClient.exe",
                                      "titleRegex": "^logo1$"},
                         "origin": "already_open"}],
            "steps": [{"stepId": "step-0001", "windowId": "W1"}],
            "cleanup": [],
        },
    })
    good = base.to_dict()

    def mutated(mutator):
        data = copy.deepcopy(good)
        mutator(data)
        with pytest.raises(RecordingModelError):
            GoldenTrace.from_dict(data)

    # 主流程 step 的 ownership 移到 cleanup(分区不匹配)。
    mutated(lambda d: d["windowSequence"].update(
        cleanup=list(d["windowSequence"]["steps"]), steps=[]))
    # entryWindow title 改错(^WRONG$):严格匹配必须拒绝。
    mutated(lambda d: d["entryWindow"].update(titleRegex="^WRONG$"))
    # identity 删 title 换 OtherRoot(selector 有 title、identity 无 title
    # 且 root 不同)→ 缺字段不得绕过。
    mutated(lambda d: d["windowSequence"]["windows"][0].update(
        identity={"processName": "EDRClient.exe",
                  "rootAutomationId": "OtherRoot"}))
    # 全局 effect id 冲突(steps/cleanup 共享命名空间)。
    mutated(lambda d: (
        d["steps"].update({"cleanup-0001": {
            "stepId": "cleanup-0001", "actionId": "gui.click", "args": {},
            "selector": d["steps"]["step-0001"]["selector"],
            "endSelector": None, "verifiers": [], "required": True,
            "status": "ready", "issues": []}}),
        d["windowSequence"].update(
            steps=list(d["windowSequence"]["steps"]),
            cleanup=[{"stepId": "cleanup-0001", "windowId": "W1",
                      "effects": [{"id": "EF1", "kind": "absent",
                                   "windowId": "W1",
                                   "verification": "explicit_assertion",
                                   "timeoutSeconds": 10}]}])))


# ---- 0947 复验反例(有效生命周期/快照/效果目标,先红后绿)----

def test_0947_action_before_first_observation_is_unattributable():
    """0947 P1-1:动作 100ms、实例首次事实 200ms —— 首见之前的动作没有
    有效实例关联证据,不得凭 RI 字符串放行。"""
    import dataclasses
    raw = _with_timeline(
        _recording([_click(1, 100, "C1", 100, instance_id="RI-0001")],
                   windows=[_MAIN]), instances=("RI-0001",))
    raw = dataclasses.replace(raw, window_timeline={
        **raw.window_timeline,
        "events": [{**raw.window_timeline["events"][0], "monotonicMs": 200}]})
    result = compile_recording(raw, profile="windows_hisec")
    assert result.golden.status == "incomplete", (
        f"首见之前的动作不得归属实例,实际 {result.golden.status}")


def test_0947_live_window_snapshot_attribution():
    """0947 P1-2a:只有 initial_observed(未销毁)的存活窗口,其后同 RI/
    handle 的正常快照必须可用(root 提升为身份;title-only 不得吞掉证据)。"""
    import dataclasses
    raw = _with_timeline(
        _recording([_click(1, 100, "C1", 100, instance_id="RI-0001")],
                   windows=[_MAIN]), instances=("RI-0001",))
    snapshot = _snap("S2", 50, 100, "MainRoot", [])
    snapshot["window"]["instanceId"] = "RI-0001"
    raw = dataclasses.replace(raw, control_snapshots=(snapshot,))
    result = compile_recording(raw, profile="windows_hisec")
    assert result.golden.status == "ready", (
        f"存活窗口的正常快照必须可用,实际 {result.golden.status}: "
        f"{[i.code for i in result.issues]}")
    seq_dict = result.golden.window_sequence.to_dict()
    identity = seq_dict["windows"][0]["identity"]
    assert identity.get("rootAutomationId") == "MainRoot", (
        f"快照 root 必须提升为身份,实际 {identity}")


def test_0947_root_evidence_conflict_is_structured_not_crash():
    """0947 P1-2b:同一快照时间提前(与 selector title 证据同存)导致 root
    提升后 identity 与 selector title 冲突 —— compile 必须产结构化
    incomplete 结果,不得直接抛 RecordingModelError。"""
    import dataclasses
    raw = _with_timeline(
        _recording([_click(1, 100, "C1", 100, instance_id="RI-0001")],
                   windows=[_MAIN]), instances=("RI-0001",))
    snapshot = _snap("S2", 10, 100, "OtherRoot", [])
    snapshot["window"]["instanceId"] = "RI-0001"
    raw = dataclasses.replace(raw, control_snapshots=(snapshot,))
    result = compile_recording(raw, profile="windows_hisec")
    # 复验要求:公开 compile 入口不因严格模型抛错,产完整 CompilationResult
    #(结构化 incomplete 或诚实合并后的 ready 均可 — 不崩溃、不丢诊断)。
    assert result.golden.schema in {"edr.desktop-golden-trace/v2",
                                    "edr.desktop-golden-trace/v3"}
    if result.golden.status == "incomplete":
        assert any("conflict" in i.code or "identity" in i.code
                   for i in result.issues), (
            f"incomplete 必须带结构化身份诊断,实际 "
            f"{[i.code for i in result.issues]}")


def test_0947_effect_target_requires_full_identity_match():
    """0947 P1-3:window_open expected.processName=Other.exe 与唯一已声明
    窗口(EDRClient.exe)不符 —— 不得借同标题 W1;0/多候选保留 incomplete。"""
    assertion = _transition(2, 150, "opened", "C1", 100, "logo1")
    assertion["type"] = "assertion"
    assertion["assertion"] = {
        "type": "window_open",
        "expected": {"exists": True, "title": "logo1",
                     "processName": "Other.exe"},
    }
    raw = _with_timeline(_recording([
        _click(1, 100, "C1", 100, instance_id="RI-0001"), assertion,
    ], windows=[_MAIN]), instances=("RI-0001",))
    result = compile_recording(raw, profile="windows_hisec")
    assert result.golden.status == "incomplete", (
        f"目标进程不符的显式断言不得借窗口,实际 {result.golden.status}")
    assert any(i.code == "compile_window_effect_unverified"
               for i in result.issues), (
        f"必须保留结构化诊断,实际 {[i.code for i in result.issues]}")
    seq_dict = result.golden.window_sequence.to_dict()
    for entry in seq_dict["steps"]:
        assert not entry.get("effects"), (
            f"0/多候选时不得生成 effect,实际 {entry}")


# ---- 1007 复验反例(先红后绿)----

def test_1007_used_ri_without_own_facts_is_incomplete():
    """1007 P1a:windows 声明 RI1/RI2、动作引用 RI1,但 timeline 只有 RI2
    的事实 —— RI1 无可验证事实,不得凭默认无限区间放行。"""
    import dataclasses
    raw = _with_timeline(
        _recording([_click(1, 100, "C1", 100, instance_id="RI-0001")],
                   windows=[_MAIN,
                            {**_MAIN, "instanceId": "RI-0002",
                             "pid": 43, "origin": "opened"}]),
        instances=("RI-0002",))  # timeline 只声明 RI2
    result = compile_recording(raw, profile="windows_hisec")
    assert result.golden.status == "incomplete", (
        f"所用 RI 无自身事实不得 ready,实际 {result.golden.status}")


def test_1007_lost_instance_continuity_is_incomplete():
    """1007 P1b:RI1 10ms initial_observed、50ms lost/tracking_loss,
    100ms 动作仍引用 RI1 —— lost 非销毁,但连续性未知不得冒充已证明;
    也不得伪造死亡。"""
    import dataclasses
    raw = _with_timeline(
        _recording([_click(1, 100, "C1", 100, instance_id="RI-0001")],
                   windows=[_MAIN]), instances=("RI-0001",))
    raw = dataclasses.replace(raw, window_timeline={
        "schema": "edr.recorded-window-timeline/v1", "events": (
            {"id": "WE1", "order": 1, "monotonicMs": 10,
             "instanceId": "RI-0001", "kind": "initial_observed",
             "source": "initial_inventory"},
            {"id": "WE2", "order": 2, "monotonicMs": 50,
             "instanceId": "RI-0001", "kind": "lost",
             "source": "tracking_loss"}),
        "coverage": {"destroyEvents": "supported", "droppedEvents": 0}})
    result = compile_recording(raw, profile="windows_hisec")
    assert result.golden.status == "incomplete", (
        f"lost 后连续性未知不得 ready,实际 {result.golden.status}")


def test_1007_normal_hide_show_stays_ready():
    """1007 对照:HIDE/SHOW(同 owner、无 lost)的正常存活窗口保持 ready
    —— 未知连续性门槛不得误伤正常可见性切换。"""
    raw = _with_timeline(
        _recording([
            _click(1, 100, "C1", 100, instance_id="RI-0001"),
            _transition(2, 150, "opened", "C1", 100, "日志中心"),
        ], windows=[_MAIN, _DIALOG]), instances=("RI-0001", "RI-0002"))
    result = compile_recording(raw, profile="windows_hisec")
    assert result.golden.status == "ready", (
        f"正常流程不得被门槛误伤,实际 {result.golden.status}: "
        f"{[i.code for i in result.issues]}")


def test_1007_effect_target_verifies_title_regex():
    """1007 P2:window_open expected.titleRegex=^NEVER_MATCH$ 与唯一窗口的
    title 事实不符 —— 不得借 W1;incomplete + 结构化诊断。"""
    assertion = _transition(2, 150, "opened", "C1", 100, "logo1")
    assertion["type"] = "assertion"
    assertion["assertion"] = {
        "type": "window_open",
        "expected": {"exists": True, "processName": "EDRClient.exe",
                     "titleRegex": "^NEVER_MATCH$"},
    }
    raw = _with_timeline(_recording([
        _click(1, 100, "C1", 100, instance_id="RI-0001"), assertion,
    ], windows=[_MAIN]), instances=("RI-0001",))
    result = compile_recording(raw, profile="windows_hisec")
    assert result.golden.status == "incomplete", (
        f"titleRegex 不匹配不得借 W1,实际 {result.golden.status}")
    assert any(i.code == "compile_window_effect_unverified"
               for i in result.issues), (
        f"必须结构化诊断,实际 {[i.code for i in result.issues]}")
    seq_dict = result.golden.window_sequence.to_dict()
    for entry in seq_dict["steps"]:
        assert not entry.get("effects"), (
            f"不匹配时不得生成 effect,实际 {entry}")


# ---- 1027 复验反例(先红后绿)----

def test_1027_same_ri_title_change_does_not_split_windows():
    """1027 P1:同 handle/pid/RI 的点击,标题 logo1→renamed(无 destroy/
    lost)—— 标题变化不分新实例(§4.2);一 RI 至多一 W。title-only 多
    标题在当前契约无法安全表达为唯一身份 → 诚实 incomplete,不拆 W、
    不编造宽 regex。"""
    raw = _with_timeline(_recording([
        _click(1, 100, "C1", 100, instance_id="RI-0001"),
        _click(2, 300, "C2", 100, instance_id="RI-0001"),
    ], windows=[_MAIN]), instances=("RI-0001",))
    import dataclasses as _dc
    renamed_event = _dc.replace(
        raw.events[1],
        evidence={"window": {"handle": 100, "title": "renamed",
                             "processName": "EDRClient.exe",
                             "instanceId": "RI-0001"},
                  "foregroundPid": 42})
    raw = _dc.replace(raw, events=(raw.events[0], renamed_event))
    result = compile_recording(raw, profile="windows_hisec")
    if result.golden.window_sequence is not None:
        seq_dict = result.golden.window_sequence.to_dict()
        owned = {s["windowId"] for s in seq_dict["steps"]}
        assert len(owned) == 1, (
            f"同 RI 不得拆成多个 W,实际 {seq_dict['windows']}")
        w = next(w for w in seq_dict["windows"] if w["id"] in owned)
        # 双标题观测下任何单 titleRegex 都不完整 — 该场景就应 incomplete。
        pass
    assert result.golden.status == "incomplete", (
        f"title-only 多标题同 RI:诚实 incomplete,实际 {result.golden.status}")


def test_1027_same_ri_root_conflict_is_incomplete():
    """1027 P1 对照:同 RI 但 root 证据冲突 → incomplete(不是新 W)。"""
    import dataclasses
    raw = _with_timeline(_recording([
        _click(1, 100, "C1", 100, instance_id="RI-0001"),
        _click(2, 300, "C2", 100, instance_id="RI-0001"),
    ], windows=[_MAIN]), instances=("RI-0001",))
    snapshots = (_snap("CS-0001", 90, 100, "RootA", []),
                 _snap("CS-0002", 250, 100, "RootB", []))
    for s in snapshots:
        s["window"]["instanceId"] = "RI-0001"
    raw = _dc_replace_snapshots(raw, snapshots)
    result = compile_recording(raw, profile="windows_hisec")
    assert result.golden.status == "incomplete", (
        f"同 RI root 冲突必须 incomplete,实际 {result.golden.status}")


def _dc_replace_snapshots(raw, snapshots):
    import dataclasses
    return dataclasses.replace(raw, control_snapshots=tuple(snapshots))


def test_1027_different_ri_same_identity_reuses_not_merges():
    """1027 对照:不同 RI、相同身份 → 各自的 W(不合并成同 W)。"""
    a = _click(1, 100, "C1", 100, instance_id="RI-0001")
    b = _click(2, 300, "C2", 100, instance_id="RI-0002")
    windows = [dict(_MAIN),
               {**_MAIN, "instanceId": "RI-0002", "pid": 99,
                "origin": "opened"}]
    raw = _with_timeline(_recording([a, b], windows=windows),
                         instances=("RI-0001", "RI-0002"))
    result = compile_recording(raw, profile="windows_hisec")
    seq_dict = result.golden.window_sequence.to_dict()
    ids = [s["windowId"] for s in seq_dict["steps"]]
    assert ids[0] != ids[1], (
        f"不同 RI 不得合并同 W,实际 {seq_dict}")


def test_1027_invalid_regex_condition_is_not_unconditional():
    """1027 P2-1:expected.titleRegex='[' 非法正则 —— 编译失败不得等同
    无条件匹配;结构化 incomplete。"""
    assertion = _transition(2, 150, "opened", "C1", 100, "logo1")
    assertion["type"] = "assertion"
    assertion["assertion"] = {
        "type": "window_open",
        "expected": {"exists": True, "processName": "EDRClient.exe",
                     "titleRegex": "["},
    }
    raw = _with_timeline(_recording([
        _click(1, 100, "C1", 100, instance_id="RI-0001"), assertion,
    ], windows=[_MAIN]), instances=("RI-0001",))
    result = compile_recording(raw, profile="windows_hisec")
    assert result.golden.status == "incomplete", (
        f"非法正则条件不得变无条件,实际 {result.golden.status}")
    assert any(i.code == "compile_window_effect_unverified"
               for i in result.issues), (
        f"必须结构化诊断,实际 {[i.code for i in result.issues]}")
    seq_dict = result.golden.window_sequence.to_dict()
    for entry in seq_dict["steps"]:
        assert not entry.get("effects"), (
            f"非法条件下不得生成 effect,实际 {entry}")


def test_1027_repeated_same_window_assertions_dedup_not_crash():
    """1027 P2-2:同 C1 绑定两条相同窗口断言 —— 合并为一个 effect(保留
    两个 raw 来源),公开 compile 产完整结果,不得抛 RecordingModelError。"""
    assertion_a = _transition(2, 150, "opened", "C1", 100, "logo1")
    assertion_a["type"] = "assertion"
    assertion_a["assertion"] = {"type": "window_open",
                                "expected": {"exists": True,
                                             "title": "logo1",
                                             "processName": "EDRClient.exe"}}
    assertion_b = _transition(3, 170, "opened", "C1", 100, "logo1")
    assertion_b["type"] = "assertion"
    assertion_b["assertion"] = {"type": "window_open",
                                "expected": {"exists": True,
                                             "title": "logo1",
                                             "processName": "EDRClient.exe"}}
    raw = _with_timeline(_recording([
        _click(1, 100, "C1", 100, instance_id="RI-0001"),
        assertion_a, assertion_b,
    ], windows=[_MAIN]), instances=("RI-0001",))
    result = compile_recording(raw, profile="windows_hisec")
    assert result.golden.schema in {"edr.desktop-golden-trace/v2",
                                    "edr.desktop-golden-trace/v3"}, (
        f"公开 compile 必须产完整结果,实际 {result.golden.schema}")
    if result.golden.window_sequence is not None:
        seq_dict = result.golden.window_sequence.to_dict()
        ids = [e["id"] for s in seq_dict["steps"]
               for e in (s.get("effects") or [])]
        assert len(ids) == len(set(ids)), (
            f"同源窗口断言必须去重,实际 {ids}")


# ---- 1107 复验反例(effect ID/公开编译异常,先红后绿)----

def _assertion_event(seq, ms, causal, handle, expected):
    event = _transition(seq, ms, "opened", causal, handle,
                        expected.get("title") or "logo1")
    event["type"] = "assertion"
    event["assertion"] = {"type": "window_open", "expected": dict(expected)}
    return event


def test_1107_literal_and_regex_same_target_merge_or_distinct_ids():
    """1107-1:title=logo1 与 titleRegex=^logo1$ 同指 W1 —— 等价后置条件
    合并为一 effect(raw 来源保留)或稳定唯一 ID 分开;任一形态都不得撞
    全局 ID 或让公开 compile 抛错。"""
    raw = _with_timeline(_recording([
        _click(1, 100, "C1", 100, instance_id="RI-0001"),
        _assertion_event(2, 150, "C1", 100,
                         {"exists": True, "title": "logo1",
                          "processName": "EDRClient.exe"}),
        _assertion_event(3, 170, "C1", 100,
                         {"exists": True, "titleRegex": "^logo1$",
                          "processName": "EDRClient.exe"}),
    ], windows=[_MAIN]), instances=("RI-0001",))
    result = compile_recording(raw, profile="windows_hisec")
    assert result.golden.schema in {"edr.desktop-golden-trace/v2",
                                    "edr.desktop-golden-trace/v3"}, (
        "公开 compile 必须产完整结果")
    if result.golden.window_sequence is not None:
        ids = [e["id"] for s in result.golden.window_sequence.to_dict()["steps"]
               for e in (s.get("effects") or [])]
        assert len(ids) == len(set(ids)), f"全局 ID 不得重复: {ids}"


def test_1107_mutually_exclusive_conditions_kept_with_unique_ids():
    """1107-2:exists=true 与 exists=false 同指 W1 —— 互斥条件完整保留
    (不删一条、不假称可通过),稳定唯一 ID;编译结果如实(含诊断或
    incomplete 皆可,但不得抛模型错)。"""
    raw = _with_timeline(_recording([
        _click(1, 100, "C1", 100, instance_id="RI-0001"),
        _assertion_event(2, 150, "C1", 100,
                         {"exists": True, "title": "logo1",
                          "processName": "EDRClient.exe"}),
        _assertion_event(3, 170, "C1", 100,
                         {"exists": False, "title": "logo1",
                          "processName": "EDRClient.exe"}),
    ], windows=[_MAIN]), instances=("RI-0001",))
    result = compile_recording(raw, profile="windows_hisec")
    assert result.golden.schema in {"edr.desktop-golden-trace/v2",
                                    "edr.desktop-golden-trace/v3"}
    if result.golden.window_sequence is not None:
        seq_dict = result.golden.window_sequence.to_dict()
        effects = [e for s in seq_dict["steps"]
                   for e in (s.get("effects") or [])]
        ids = [e["id"] for e in effects]
        assert len(ids) == len(set(ids)), f"互斥条件须唯一 ID: {ids}"
        kinds = {e["kind"] for e in effects}
        assert {"appeared", "absent"} <= kinds, (
            f"互斥条件不得被删除: {effects}")


# ---- 1127 复验反例(首个失效边界,先红后绿)----

def _boundary_timeline(facts):
    import dataclasses
    raw = _with_timeline(
        _recording([_click(1, 100, "C1", 100, instance_id="RI-0001")],
                   windows=[_MAIN]), instances=("RI-0001",))
    events = [{"id": f"WE{i}", "order": i, "monotonicMs": ms,
               "instanceId": "RI-0001", "kind": kind,
               "source": source}
              for i, (kind, ms, source) in enumerate(facts, start=1)]
    return dataclasses.replace(raw, window_timeline={
        "schema": "edr.recorded-window-timeline/v1",
        "events": tuple(events),
        "coverage": {"destroyEvents": "supported", "droppedEvents": 0}})


def test_1127_first_unprovable_boundary_is_kept():
    """1127:首个 lost/destroyed 即不可证明边界;后续重复 lost/后续失效
    事实不得延后已结束的区间。"""
    import dataclasses
    # a) initial10 → lost50 → lost150;动作 100 引用 RI1(已过首边界)。
    raw = _boundary_timeline([
        ("initial_observed", 10, "initial_inventory"),
        ("lost", 50, "tracking_loss"),
        ("lost", 150, "tracking_loss"),
    ])
    result = compile_recording(raw, profile="windows_hisec")
    assert result.golden.status == "incomplete", (
        f"重复 lost 不得延后首边界,实际 {result.golden.status}")
    # b) initial10 → destroyed50 → lost150;动作 100(明确死亡后)。
    raw2 = _boundary_timeline([
        ("initial_observed", 10, "initial_inventory"),
        ("destroyed", 50, "native_destroy"),
        ("lost", 150, "tracking_loss"),
    ])
    result2 = compile_recording(raw2, profile="windows_hisec")
    assert result2.golden.status == "incomplete", (
        "明确死亡后的动作不得被后续 lost 重新放行")


def test_1127_normal_lifecycle_still_ready():
    """1127 对照:正常 HIDE/SHOW/新 RI 不被边界语义误伤。"""
    raw = _with_timeline(
        _recording([
            _click(1, 100, "C1", 100, instance_id="RI-0001"),
            _transition(2, 150, "opened", "C1", 100, "日志中心"),
        ], windows=[_MAIN, _DIALOG]), instances=("RI-0001", "RI-0002"))
    result = compile_recording(raw, profile="windows_hisec")
    assert result.golden.status == "ready", (
        f"正常流程不得误伤,实际 {result.golden.status}: "
        f"{[i.code for i in result.issues]}")


# ---- 1140 复验反例(model/schema 双验证,先红后绿)----

def _validate_schema(document, name):
    import jsonschema
    from pathlib import Path
    schema = json.loads(
        (Path(__file__).resolve().parents[1] / "schema" / name)
        .read_text(encoding="utf-8"))
    jsonschema.validate(document, schema)


def _compile_minimal_v3(handle=100, root=None, title_regex=None, instance="RI-0001"):
    """公开 compile 产出的真实 ready root-only/title-only v3。

    root 证据来自 snapshot(实例自身事实),manifest 不带 root 字段。"""
    import dataclasses as _dc
    click = _click(1, 100, "C1", handle, instance_id=instance)
    windows = [dict(_MAIN)]
    snapshots = (_snap("CS-0001", 50, handle, root,
                       [{"automationId": "EdrMainWindow",
                         "controlType": "Window"}]),
                 ) if root else ()
    raw = _with_timeline(
        _recording([click], windows=windows, snapshots=snapshots),
        instances=(instance,))
    return compile_recording(raw, profile="windows_hisec")


def test_1140_real_v3_artifacts_pass_schema_dual_validation():
    """1140:真实 compile 的 ready v3(root-only 与 title-only)必须同时过
    GoldenTrace 模型与 JSON schema —— 共享 $defs.window 缺 rootAutomationId、
    $defs.verifier 缺 source 的缺口由本回归锁定。"""
    # root-only 正例
    result = _compile_minimal_v3(handle=100, root="EdrMainWindow")
    assert result.golden.schema == "edr.desktop-golden-trace/v3"
    assert result.golden.status == "ready"
    document = result.golden.to_dict()
    GoldenTrace.from_dict(copy.deepcopy(document))  # model PASS
    _validate_schema(document, "desktop-golden-trace.schema.json")  # schema
    # title-only 对照
    result2 = _compile_minimal_v3(handle=100)
    document2 = result2.golden.to_dict()
    GoldenTrace.from_dict(copy.deepcopy(document2))
    _validate_schema(document2, "desktop-golden-trace.schema.json")


def test_1140_verifier_source_passes_schema():
    """user_assertion 与 native_transition 产物过 schema(共享 verifier
    定义必须接受 source 字段)。"""
    raw = _with_timeline(_recording([
        _click(1, 100, "C1", 100, instance_id="RI-0001"),
        _transition(2, 150, "opened", "C1", 100, "日志中心"),
        _click(3, 300, "CAUSE-2", 200, "logRow", instance_id="RI-0002"),
    ], windows=[_MAIN, _DIALOG]), instances=("RI-0001", "RI-0002"))
    # 给 transition2 挂真实用户断言 → user_assertion verifier。
    raw = dataclasses_replace_input(raw, 2, {
        "kind": "opened", "title": "日志中心",
        "processName": "EDRClient.exe", "timeoutSeconds": 10.0},
        assertion={"type": "window_open",
                   "expected": {"exists": True, "title": "日志中心",
                                "processName": "EDRClient.exe"}})
    result = compile_recording(raw, profile="windows_hisec")
    document = result.golden.to_dict()
    GoldenTrace.from_dict(copy.deepcopy(document))
    _validate_schema(document, "desktop-golden-trace.schema.json")


def test_1140_schema_rejects_unknown_and_invalid_source():
    """schema 负例:未知字段与非法 source 枚举拒绝(model 同步)。"""
    import copy
    result = _compile_minimal_v3(handle=100, root="EdrMainWindow")
    document = result.golden.to_dict()
    # 未知字段
    bad = copy.deepcopy(document)
    bad["windowSequence"]["windows"][0]["unexpected"] = True
    with pytest.raises(Exception):
        _validate_schema(bad, "desktop-golden-trace.schema.json")
    # 非法 source
    bad2 = copy.deepcopy(document)
    for step in bad2["steps"].values():
        for verifier in step.get("verifiers") or []:
            verifier["source"] = "made_up"
    if any(step.get("verifiers") for step in bad2["steps"].values()):
        with pytest.raises(Exception):
            _validate_schema(bad2, "desktop-golden-trace.schema.json")
        with pytest.raises(RecordingModelError):
            GoldenTrace.from_dict(copy.deepcopy(bad2))


def test_1140_v1_v2_still_validate():
    """§9 兼容:v1/v2 产物继续过 schema。"""
    result = compile_recording(
        RawRecording.from_dict(json.load(
            open("recordings/supervised_1102b/recording.json"))),
        profile="windows_hisec")
    _validate_schema(result.golden.to_dict(), "desktop-golden-trace.schema.json")
    v2_doc = _compile_minimal_v3(handle=100, root="EdrMainWindow").golden.to_dict()
    v2_doc["schema"] = "edr.desktop-golden-trace/v2"
    v2_doc.pop("windowSequence")
    v2_doc.pop("entryWindow", None)
    GoldenTrace.from_dict(copy.deepcopy(v2_doc))
    _validate_schema(copy.deepcopy(v2_doc), "desktop-golden-trace.schema.json")


def dc_replace_event_type(raw, seq, new_type):
    import dataclasses
    events = []
    for e in raw.events:
        if e.sequence == seq:
            e = dataclasses.replace(e, type=new_type)
        events.append(e)
    return dataclasses.replace(raw, events=tuple(events))

# ---- 10号设计:编译期窗口归属消解(跨窗口流程)----
# 评审修订:fixture 必须证据一致(B 窗口的 manifest/snapshot/click 三者一致),
# causalId 与打开 transition 一致;正例锁定 ready+非空 windowSequence+
# 完整步骤映射+A 复用 W+B 独立 W;冲突拒绝另设负例。


def _cross_window_events(extra_snapshots=()):
    """A→B→A 的一致 fixture:
    click1(handle100,logo1,causal CAUSE-1) → opened(handle200,Log Center)
    → click3(handle200,LogRow,causal CAUSE-2) → closed → click5(handle100,
    btnBack,causal CAUSE-3)。
    证据一致:manifest 两实例、快照 CS-0001(root=EdrMainWindow,title=logo1)
    与 CS-0002(root=LogCenterWindow,title=Log Center)、每步 click 的
    scope.windowTitle/evidence.window.title/snapshotId 均与所在窗口一致;
    click1 的 causalId 与 opened transition 相同(CAUSE-1)。"""
    events = [
        _click(1, 100, "CAUSE-1", 100, instance_id="RI-0001",
               snapshot_id="CS-0001"),
        _transition(2, 150, "opened", "CAUSE-1", 200, "Log Center"),
        _click(3, 300, "CAUSE-2", 200, instance_id="RI-0002",
               automation_id="logRow", snapshot_id="CS-0002",
               title="Log Center", window_title="^Log Center$"),
        _transition(4, 400, "closed", "CAUSE-2", 200, "Log Center"),
        _click(5, 500, "CAUSE-3", 100, instance_id="RI-0001",
               automation_id="btnBack", snapshot_id="CS-0001"),
    ]
    windows = [
        {"title": "logo1", "processName": "EDRClient.exe", "handle": 100,
         "pid": 42, "origin": "already_open", "closed": False,
         "instanceId": "RI-0001"},
        {"title": "Log Center", "processName": "EDRClient.exe",
         "handle": 200, "pid": 42, "origin": "opened", "closed": True,
         "instanceId": "RI-0002", "openedBy": "CAUSE-1"},
    ]
    snapshots = (
        _snap("CS-0001", 90, 100, "EdrMainWindow",
              [{"automationId": "EdrMainWindow", "controlType": "Window"}]),
        _snap("CS-0002", 250, 200, "LogCenterWindow",
              [{"automationId": "LogCenterWindow", "controlType": "Window"}],
              title="Log Center"),
        *extra_snapshots,
    )
    return _with_timeline(
        _recording(events, windows=windows, snapshots=snapshots),
        instances=("RI-0001", "RI-0002"))


def test_cross_window_a_b_a_window_sequence():
    """正例锁定(复验要求):ready、非空 windowSequence、完整步骤映射、
    A 返回复用 W、B 独立 W。一致 fixture 下任何一项缺失都是失败。"""
    raw = _cross_window_events()
    result = compile_recording(raw, profile="windows_hisec")
    golden = result.golden
    assert golden.status == "ready", (
        f"一致证据正例必须 ready,实际 {golden.status},issues="
        f"{[(i.code, i.sequence) for i in result.issues]}")
    assert not result.issues, (
        f"一致证据正例不得带 issue,实际 {result.issues}")
    assert golden.window_sequence is not None, (
        "一致证据正例必须生成 windowSequence")
    seq_dict = golden.window_sequence.to_dict()
    assert seq_dict["windows"], "windowSequence.windows 非空"
    steps_by_id = {s["stepId"]: s["windowId"] for s in seq_dict["steps"]}
    assert len(steps_by_id) == len(golden.steps), (
        f"每个编译步骤都必须有窗口归属,实际 {steps_by_id} vs "
        f"{[s.step_id for s in golden.steps]}")
    # A→B→A:首步与返回步复用同 W;B 步独立 W。
    w_first = steps_by_id["step-0001"]
    w_last = steps_by_id[f"step-{len(golden.steps):04d}"]
    assert w_first == w_last, (
        f"A→B→A 返回同窗口应复用同 W,实际 {steps_by_id}")
    w_mid = steps_by_id["step-0002"]
    assert w_mid != w_first, (
        f"B 窗口必须是独立 W,实际 {steps_by_id}")
    # B 的 W 身份确实指向 Log Center(不是借主窗口身份)。
    windows_by_id = {w["id"]: w for w in seq_dict["windows"]}
    b_identity = windows_by_id[w_mid]["identity"]
    assert b_identity.get("rootAutomationId") == "LogCenterWindow", (
        f"B 的 W 身份应来自 B 窗口快照,实际 {b_identity}")
    a_identity = windows_by_id[w_first]["identity"]
    assert a_identity.get("rootAutomationId") == "EdrMainWindow", (
        f"A 的 W 身份应来自 A 窗口快照,实际 {a_identity}")


def test_cross_window_source_map_covers_all_raw():
    """§6.1:全部 raw 事件(含 transition 折叠)可追查到编译步骤。
    复验修订:精确逐步映射 — step-0001:[1,2](click+opened)、
    step-0002:[3,4](click+closed)、step-0003:[5];删除任一映射
    (如 raw2/raw4)必须失败。"""
    raw = _cross_window_events()
    result = compile_recording(raw, profile="windows_hisec")
    mapping = result.diagnostics.get("sourceEvents")
    assert mapping and mapping.get("byStep"), "sourceEvents 必须存在"
    by_step = {step: sorted(int(r) for r in refs)
               for step, refs in mapping["byStep"].items()}
    assert by_step == {
        "step-0001": [1, 2],
        "step-0002": [3, 4],
        "step-0003": [5],
    }, f"来源映射必须逐步精确覆盖(含 transition),实际 {by_step}"
    all_refs = {r for refs in by_step.values() for r in refs}
    assert all_refs == {e.sequence for e in raw.events}, (
        f"全部 raw 序号(含 transition)必须可追查,实际 {all_refs}")


def test_cross_window_root_conflict_rejected_as_negative():
    """负例(与正例分开):B 窗口同时段出现冲突 root 快照 → incomplete,
    不得产出可执行 windowSequence(10 号 §2.4:冲突不静默覆盖)。"""
    conflict_snap = _snap("CS-0003", 260, 200, "EdrMainWindow",
                          [{"automationId": "EdrMainWindow",
                            "controlType": "Window"}],
                          title="Log Center")
    raw = _cross_window_events(extra_snapshots=(conflict_snap,))
    result = compile_recording(raw, profile="windows_hisec")
    assert result.golden.status == "incomplete", (
        f"root 冲突必须 incomplete,实际 {result.golden.status}")
    assert result.golden.window_sequence is None, (
        "root 冲突不得产出可执行 windowSequence")
    conflict_issues = [i for i in result.issues
                       if i.code == "compile_window_identity_conflict"]
    assert conflict_issues, (
        f"必须输出结构化冲突诊断,实际 "
        f"{[(i.code, i.sequence) for i in result.issues]}")


# ---- 10号遗留:drag 跨窗口归属正负例(编译期归属消解 × drag verifier) ----

def _cross_window_drag_events(extra_snapshots=(), *,
                              with_verifier=True):
    """drag 场景的一致 fixture:主窗口 drag(带显式结果 verifier)
    → 打开 Log Center → 日志中心动作。证据一致(causalId/快照/manifest)。
    顺序:click1 → opened transition → drag(主窗口) → 日志中心动作。"""
    drag_target = {
        "snapshotId": "CS-0001", "targetId": "T0002",
        "fingerprint": "sha256:thumb", "controlType": "Thumb",
        "automationId": "sliderThumb", "text": "阈值",
        "rect": [100, 100, 140, 140], "protected": False,
        "sameNameSiblings": None, "identifier": None,
    }
    drag_input = {
        "button": "left",
        "screenPoint": [110, 130],
        "endPoint": [305, 210],
        "endTarget": {
            "automationId": "sliderThumb", "controlType": "Thumb",
            "text": "阈值", "rect": [300, 200, 340, 240],
        },
    }
    events = [
        _click(1, 100, "CAUSE-1", 100, instance_id="RI-0001",
               snapshot_id="CS-0001"),
        _transition(2, 150, "opened", "CAUSE-1", 200, "Log Center"),
        {
            "sequence": 3, "type": "drag_commit",
            "wallTime": "2026-11-03T00:00:03Z", "monotonicMs": 200,
            "causalId": "CAUSE-DRAG",
            "scope": {"backend": "windows_pywinauto",
                      "processName": "EDRClient.exe", "target": "target",
                      "windowTitle": "^logo1$"},
            "input": drag_input,
            "observedTarget": drag_target,
            "evidence": {"window": {"handle": 100, "title": "logo1",
                                     "processName": "EDRClient.exe",
                                     "instanceId": "RI-0001"},
                         "foregroundPid": 42},
            "assertion": None,
        },
        {
            "sequence": 4, "type": "assertion",
            "wallTime": "2026-11-03T00:00:04Z", "monotonicMs": 230,
            "causalId": "CAUSE-DRAG",
            "scope": {"backend": "windows_pywinauto",
                      "processName": "EDRClient.exe", "target": "target",
                      "windowTitle": "^logo1$"},
            "input": {},
            "observedTarget": drag_target,
            "evidence": {"window": {"handle": 100, "title": "logo1",
                                     "processName": "EDRClient.exe",
                                     "instanceId": "RI-0001"},
                         "foregroundPid": 42},
            "assertion": ({"type": "visible", "expected": True,
                           "timeoutSeconds": 5.0}
                          if with_verifier else None),
        },
        _click(5, 300, "CAUSE-2", 200, instance_id="RI-0002",
               automation_id="logRow", snapshot_id="CS-0002",
               title="Log Center", window_title="^Log Center$"),
    ]
    windows = [
        {"title": "logo1", "processName": "EDRClient.exe", "handle": 100,
         "pid": 42, "origin": "already_open", "closed": False,
         "instanceId": "RI-0001"},
        {"title": "Log Center", "processName": "EDRClient.exe",
         "handle": 200, "pid": 42, "origin": "opened", "closed": True,
         "instanceId": "RI-0002", "openedBy": "CAUSE-1"},
    ]
    snapshots = (
        _snap("CS-0001", 90, 100, "EdrMainWindow",
              [{"automationId": "EdrMainWindow", "controlType": "Window"}]),
        _snap("CS-0002", 280, 200, "LogCenterWindow",
              [{"automationId": "LogCenterWindow", "controlType": "Window"}],
              title="Log Center"),
        *extra_snapshots,
    )
    return _with_timeline(
        _recording(events, windows=windows, snapshots=snapshots),
        instances=("RI-0001", "RI-0002"))


def test_cross_window_drag_with_verifier_window_sequence():
    """正例:带显式结果 verifier 的 drag 在跨窗口流程中编译 ready,
    windowSequence 归属正确 — drag 步留在主窗口 W,后续 B 步独立 W;
    drag 的窗口效果不被业务/结果 verifier 替代(§6.5)。"""
    raw = _cross_window_drag_events()
    result = compile_recording(raw, profile="windows_hisec")
    codes = [i.code for i in result.issues]
    assert result.golden.status == "ready", codes
    assert "compile_drag_verifier_required" not in codes
    seq_dict = result.golden.window_sequence.to_dict()
    steps_by_id = {s["stepId"]: s["windowId"] for s in seq_dict["steps"]}
    action_by_id = {s.step_id: s.action_id for s in result.golden.steps}
    drag_steps = [sid for sid in steps_by_id
                  if action_by_id.get(sid) == "pointer.drag"]
    assert drag_steps, f"drag 步必须在编译产物中,实际 {steps_by_id}"
    # drag 的归属 = 主窗口 W(与 step-0001 同 W);B 步独立。
    assert steps_by_id[drag_steps[0]] == steps_by_id["step-0001"]
    b_windows = {w["id"] for w in seq_dict["windows"]
                 if w["identity"].get("rootAutomationId") == "LogCenterWindow"}
    assert b_windows, "B 窗口独立 W 必须存在"


def test_cross_window_drag_without_verifier_stays_incomplete():
    """负例:同一跨窗口场景,drag 无结果 verifier → drag 步 incomplete
    (compile_drag_verifier_required),不因窗口归属正确而整体 ready;
    窗口归属正确不能替代结果 verifier(§6.5)。"""
    raw = _cross_window_drag_events(with_verifier=False)
    result = compile_recording(raw, profile="windows_hisec")
    codes = [i.code for i in result.issues]
    assert "compile_drag_verifier_required" in codes, codes
    assert result.golden.status != "ready", (
        "窗口归属消解不得替代 drag 结果 verifier")


def _cross_window_isolation_raw():
    """自包含 v2 raw:A(W1 btnApply)→opened W2→click W2 logRow→closed→
    click W1;末尾一条**显式因果绑定 drag 动作(W1)**的内容断言,但其
    evidence.window/observedTarget 是 W2 logRow(目的窗口 ≠ 动作窗口)。"""
    base = "sha256:" + "a" * 64
    def ev(seq, mono, type_, *, input_=None, observed=None, window=None,
           assertion=None, causal=None, control=None):
        return {
            "sequence": seq, "type": type_,
            "wallTime": f"2026-11-06T00:00:{seq:02d}.000Z",
            "monotonicMs": 1000 + mono,
            "causalId": causal or f"CAUSE-{seq:04d}",
            "scope": {"backend": "windows_pywinauto",
                      "processName": "EDRClient.exe", "target": "t",
                      "windowTitle": "^logo1$|^Log Center$"},
            "input": input_ or {},
            "observedTarget": observed,
            "evidence": {"window": window or {}, "foregroundPid": 42},
            "assertion": assertion,
        }
    w1 = {"handle": 100, "title": "logo1", "processName": "EDRClient.exe",
          "instanceId": "RI-0001"}
    w2 = {"handle": 200, "title": "Log Center",
          "processName": "EDRClient.exe", "instanceId": "RI-0002"}
    def obs(target_id, snap, control_type, automation_id, text, rect):
        return {"snapshotId": f"OBS-{target_id:04d}", "targetId": f"T{target_id:04d}",
                "fingerprint": "sha256:" + "c" * 64,
                "controlType": control_type, "automationId": automation_id,
                "identifier": None, "text": text, "rect": rect,
                "ancestry": [], "protected": False,
                "sameNameSiblings": None}
    t_apply = obs(1, 1, "Button", "btnApply", "Apply", [10, 20, 110, 60])
    t_row = obs(2, 2, "Row", "logRow", "row-1", [0, 0, 200, 30])
    events = [
        ev(1, 10, "pointer_click", input_={"button": "left",
                                           "screenPoint": [50, 40]},
           observed=t_apply, window=w1),
        ev(2, 20, "window_transition",
           input_={"kind": "opened", "processName": "EDRClient.exe",
                   "title": "Log Center", "pid": 42, "handle": 200},
           window=w1, causal="CAUSE-0001"),
        ev(3, 30, "pointer_click", input_={"button": "left",
                                           "screenPoint": [60, 40]},
           observed=t_row, window=w2),
        ev(4, 40, "window_transition",
           input_={"kind": "closed", "processName": "EDRClient.exe",
                   "title": "Log Center", "pid": 42, "handle": 200},
           window=w1, causal="CAUSE-0003"),
        ev(5, 50, "pointer_click", input_={"button": "left",
                                           "screenPoint": [50, 40]},
           observed=t_apply, window=w1),
        # 跨窗内容断言(审核反例结构):causal 绑定 **W1 动作(seq5)**,
        # 但 evidence/observedTarget 属于 W2 logRow——目的窗口 ≠ 动作
        # 窗口 → 必须独立成步,不得折入 W1 步(2038 复验)。
        ev(6, 60, "assertion", observed=t_row, window=w2,
           assertion={"type": "text_contains", "expected": "row-1",
                      "timeoutSeconds": 5.0},
           causal="CAUSE-0005"),
    ]
    windows = [
        {"title": "logo1", "processName": "EDRClient.exe", "handle": 100,
         "pid": 42, "origin": "already_open", "closed": False,
         "instanceId": "RI-0001"},
        {"title": "Log Center", "processName": "EDRClient.exe",
         "handle": 200, "pid": 42, "origin": "opened", "closed": True,
         "instanceId": "RI-0002", "openedBy": "CAUSE-0001"},
    ]
    snapshots = [
        {"snapshotId": "CS-0001", "capturedAtMs": 90, "reason": "step",
         "window": {"handle": 100, "processName": "EDRClient.exe",
                    "rootAutomationId": "EdrMainWindow", "title": "logo1",
                    "instanceId": "RI-0001"},
         "contentHash": base, "stateHash": "sha256:" + "b" * 64,
         "truncated": False, "complete": True, "controlCount": 1,
         "controls": [{"automationId": "EdrMainWindow",
                       "controlType": "Window"}]},
        {"snapshotId": "CS-0002", "capturedAtMs": 130, "reason": "step",
         "window": {"handle": 200, "processName": "EDRClient.exe",
                    "rootAutomationId": "LogCenterWindow",
                    "title": "Log Center", "instanceId": "RI-0002"},
         "contentHash": "sha256:" + "d" * 64,
         "stateHash": "sha256:" + "e" * 64,
         "truncated": False, "complete": True, "controlCount": 1,
         "controls": [{"automationId": "LogCenterWindow",
                       "controlType": "Window"}]}]
    # 跨窗内容断言:causal 绑定 **seq3(W2 内点击,W1 动作窗口之外)** —
    # 目的窗口 W2 在断言时存活(置于 closed 之前),语义上内容断言归属
    # 其目的窗口独立求值;causal 与动作共享(causal=CAUSE-0003)。
    return {
        "schema": "edr.desktop-recording/v2", "sessionId": "REC-" + "c" * 32,
        "name": "cross-window-isolation",
        "captureDiagnostics": {"outOfScopeEvents": 0,
                               "droppedPackets": 0,
                               "correlationErrorCount": 0,
                               "recorderUiEvents": 0,
                               "unidentifiedTargetEvents": 0},
        "windows": windows, "controlSnapshots": snapshots,
        "rejections": [], "events": events,
        "windowTimeline": {
            "schema": "edr.recorded-window-timeline/v1",
            "events": [
                {"id": "WE1", "order": 1, "monotonicMs": 1005,
                 "kind": "initial_observed", "source": "initial_inventory",
                 "instanceId": "RI-0001"},
                {"id": "WE2", "order": 2, "monotonicMs": 1020,
                 "kind": "created", "source": "native_show",
                 "ingressSequence": 2, "instanceId": "RI-0002",
                 "associatedActionCausalId": "CAUSE-0001",
                 "association": "temporal_forward"},
                {"id": "WE3", "order": 3, "monotonicMs": 1040,
                 "kind": "hidden", "source": "native_hide",
                 "ingressSequence": 4, "instanceId": "RI-0002"},
            ],
            "coverage": {"destroyEvents": "supported", "droppedEvents": 0},
        },
    }


def test_explicit_causal_cross_window_assertion_gets_own_step():
    """P1(2038 复验):显式因果绑定(W1 动作)的 W2 内容断言不得折入
    W1 步骤——必须**独立 assertion-only 步**存在、携带原 verifier
    (logRow/text_contains/expected)、目的窗口 W2;同窗组合折叠不受
    影响;golden ready(执行链对独立步真实求值)。"""
    raw = _cross_window_isolation_raw()
    result = compile_recording(RawRecording.from_dict(raw),
                               profile="windows_hisec")
    codes = [i.code for i in result.issues]
    assert result.golden.status == "ready", codes
    body = result.golden.to_dict().get("steps") or {}
    # 无任何 W1 动作步携带 logRow 目的 verifier(不塞来源窗口)。
    misplaced = [sid for sid, s in body.items()
                 if s.get("actionId") and any(
                     (v.get("match") or {}).get("automation_id") == "logRow"
                     for v in s.get("verifiers") or [])]
    assert not misplaced, misplaced
    # 独立 assertion-only 步存在:actionId 为 None、selector 窗口是
    # W2(^Log Center$)、verifier 原样携带(logRow + expected "row-1")。
    independent = [s for s in body.values()
                   if not s.get("actionId") and any(
                       (v.get("match") or {}).get("automation_id") == "logRow"
                       for v in s.get("verifiers") or [])]
    assert len(independent) == 1, body
    step = independent[0]
    assert ((step.get("selector") or {}).get("window") or {}).get(
        "titleRegex") == "^Log\\ Center$", step.get("selector")
    v = next(v for v in step["verifiers"]
             if (v.get("match") or {}).get("automation_id") == "logRow")
    assert v["type"] == "text_contains" and v["expected"] == "row-1", v
    # windowSequence:主链 A→B→A + 跨窗断言独立步归属其目的窗口 W2。
    assert [s["windowId"] for s in
            result.golden.window_sequence.to_dict()["steps"]] == \
        ["W1", "W2", "W1", "W2"]


def test_late_transition_with_old_causal_stays_structured_incomplete():
    """P1(2324):explicit_bound 仅限合法 assertion 事件——旧 causal 的
    **窗口转换**(非最近动作因果)不得送入 assertion 分支以空
    assertion 构造 verifier 抛异常;必须沿用转换门的结构化
    incomplete/拒绝诊断。"""
    from target.recording.models import RawRecording
    # 自包含 fixture(本模块 _cross_window_isolation_raw):不依赖被
    # .gitignore 的验收报告原件,干净环境可直接运行。
    raw = _cross_window_isolation_raw()
    first_causal = raw["events"][0]["causalId"]
    # seq4 closed 窗口转换改绑首动作(非最近动作因果)→ 迟到转换。
    raw["events"][3]["causalId"] = first_causal
    result = compile_recording(RawRecording.from_dict(raw),
                               profile="windows_hisec")
    codes = {i.code for i in result.issues}
    # 结构化 incomplete(0941 转换门语义),不是异常/空 verifier。
    assert result.golden.status == "incomplete", codes
    assert "compile_transition_unbound" in codes, codes


# ---- §5 单夹布局接线:write_compilation_artifacts 的 v3 轨迹落盘缝 ----

def test_v3_compilation_lands_trajectory_folder_trace_and_source_map():
    """§5 接线正例:v3 编译产物经 write_compilation_artifacts 落入单夹
    layouts——trajectories/TRJ-<id>/revisions/REV-0001/trace.v3.json 与
    source-map.json,内容与内存 golden/诊断逐键一致;平铺产物不回退
    (golden-trace.json 不再双写);manifest 指针仍指 REV-0001(发布是
    独立步骤,接线不冒充发布)。"""
    import tempfile
    from pathlib import Path
    from agent.recording import artifacts as artifacts_mod
    from agent.recording.models import GoldenTrace
    recording = _with_timeline(
        _recording(
            [_click(1, 100, "CAUSE-1", 100, instance_id="RI-0001")],
            windows=[_MAIN]), instances=("RI-0001",))
    result = compile_recording(recording, profile="windows_hisec")
    assert result.golden.schema == "edr.desktop-golden-trace/v3"
    with tempfile.TemporaryDirectory() as tmp:
        artifacts = artifacts_mod.write_compilation_artifacts(
            Path(tmp) / "out", recording, result)
        traj_root = Path(tmp) / "out" / "trajectories" / "TRJ-0001"
        rev = traj_root / "revisions" / "REV-0001"
        trace_path = rev / "trace.v3.json"
        map_path = rev / "source-map.json"
        assert trace_path.is_file(), "v3 主轨迹必须落入单夹布局"
        assert map_path.is_file(), "来源映射必须与主轨迹同修订落盘"
        on_disk = json.loads(trace_path.read_text(encoding="utf-8"))
        assert GoldenTrace.from_dict(on_disk).to_dict() == \
            result.golden.to_dict(), "落盘内容与内存 golden 逐键一致"
        source_map = json.loads(map_path.read_text(encoding="utf-8"))
        assert source_map["schema"] == "edr.source-map/v1"
        assert source_map["byStep"] == \
            result.diagnostics["sourceEvents"]["byStep"]
        manifest = json.loads(
            (traj_root / "manifest.json").read_text(encoding="utf-8"))
        assert manifest["currentRevision"] == "revisions/REV-0001", (
            "接线只写修订内容,manifest 发布属独立步骤")
        assert manifest["taskKey"], "manifest 必须携带任务标识"
        # 平铺 golden-trace.json 不再双写:主轨迹唯一(§2/§5),写两份
        # 就是两份可漂移的副本。
        assert not (artifacts.directory / "golden-trace.json").exists()


def test_v3_compilation_trajectory_lands_are_write_once_on_recompile():
    """同目录重复编译不得覆盖既有修订(§5:同名录制不覆盖、修订不可变):
    同 output_root 第二次 write_compilation_artifacts 分配新 TRJ id,
    第一次的修订内容逐字节不动。"""
    import tempfile
    from pathlib import Path
    from agent.recording import artifacts as artifacts_mod
    recording = _with_timeline(
        _recording(
            [_click(1, 100, "CAUSE-1", 100, instance_id="RI-0001")],
            windows=[_MAIN]), instances=("RI-0001",))
    result = compile_recording(recording, profile="windows_hisec")
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "out"
        first = artifacts_mod.write_compilation_artifacts(out, recording, result)
        first_trace = (first.directory.parent / "trajectories" / "TRJ-0001"
                       / "revisions" / "REV-0001" / "trace.v3.json")
        assert first_trace.is_file()
        before = first_trace.read_bytes()
        second = artifacts_mod.write_compilation_artifacts(out, recording, result)
        assert second.directory.parent / "trajectories" / "TRJ-0002"
        assert first_trace.read_bytes() == before, (
            "既有修订内容必须逐字节不动")


def test_v1_compilation_keeps_flat_layout_without_trajectory_folder():
    """兼容不回退(§6.2):v1 编译产物走既有平铺落盘,不建 TRJ 单夹、
    不产 source-map——v3 门只对 v3 schema 生效。"""
    import tempfile
    from pathlib import Path
    from agent.recording import artifacts as artifacts_mod
    recording = _recording(
        [_click(1, 100, "CAUSE-1", 100)])
    result = compile_recording(recording)
    assert result.golden.schema != "edr.desktop-golden-trace/v3"
    with tempfile.TemporaryDirectory() as tmp:
        artifacts = artifacts_mod.write_compilation_artifacts(
            Path(tmp) / "out", recording, result)
        assert (artifacts.directory / "golden-trace.json").is_file(), (
            "v1 平铺 golden-trace.json 保持既有行为")
        assert not (Path(tmp) / "out" / "trajectories").exists(), (
            "v1 产物不得落入单夹布局")


# ---- §5 单夹布局接线:collect_recording_artifacts 真实链(录制停止共用入口) ----

def _v3_collect_fixture():
    """单点击 v3 fixture(与 §5 接线用例同款),供收集链用例复用。"""
    return (
        _with_timeline(
            _recording(
                [_click(1, 100, "CAUSE-1", 100, instance_id="RI-0001")],
                windows=[_MAIN]), instances=("RI-0001",)),
    )


def _collect_agent_with_captures(png: bytes, capture_id: str, recording_dict: dict):
    """collect_recording_artifacts 的真实 agent 形状:stop 结果回放 +
    get_recording_capture 按 capture_id 返回 digest 一致的帧。
    断言 get_recording_capture 的 arguments 携带 sessionId(显式会话
    归属契约,与 test_capture_session_attribution 同语义)。"""
    import base64
    import hashlib
    seen_capture_args: list[dict] = []

    def _call(name, arguments=None, **kw):
        if name == "get_recording_capture":
            seen_capture_args.append(dict(arguments or {}))
            return {
                "ok": True,
                "image_b64": base64.b64encode(png).decode("ascii"),
                "sha256": "sha256:" + hashlib.sha256(png).hexdigest(),
            }
        return {"ok": True, "recording": recording_dict}

    return _call, seen_capture_args


def test_collect_recording_artifacts_lands_v3_trajectory_via_real_chain():
    """§5 收集链接线正例:录制停止链(record stop / ui_bridge /
    rerecord_loop 的共用入口 collect_recording_artifacts)对 v3 编译产物
    与 record compile 落盘行为一致——TRJ-0001/REV-0001 trace.v3.json +
    source-map.json 落盘、内容与内存 golden/诊断逐键一致、manifest 指针
    不动、平铺 golden-trace.json 不双写。走真实 _fetch_captures 链
    (captures 参数形状),取证帧随 artifacts 落 assets/observations/。"""
    import base64
    import hashlib
    import io
    import tempfile
    from pathlib import Path
    from agent.recording.collection import collect_recording_artifacts
    from agent.recording.models import GoldenTrace
    from types import SimpleNamespace

    from PIL import Image
    png_buf = io.BytesIO()
    Image.new("RGB", (60, 40), (128, 128, 128)).save(png_buf, format="PNG")
    png = png_buf.getvalue()
    capture_id = "CAP-" + "a" * 32

    recording = _with_timeline(
        _recording(
            [{**_click(1, 100, "CAUSE-1", 100, instance_id="RI-0001"),
              "evidence": {
                  **_click(1, 100, "CAUSE-1", 100, instance_id="RI-0001")["evidence"],
                  "beforeCapture": {
                      "id": capture_id,
                      "sha256": "sha256:" + hashlib.sha256(png).hexdigest(),
                      "width": 60, "height": 40, "origin": [0, 0],
                      "redacted": True, "redactions": []}}}],
            windows=[_MAIN]), instances=("RI-0001",))
    recording_dict = recording.to_dict()
    _call, seen_capture_args = _collect_agent_with_captures(
        png, capture_id, recording_dict)

    with tempfile.TemporaryDirectory() as tmp:
        artifacts, issues = collect_recording_artifacts(
            SimpleNamespace(call_tool=_call),
            {"ok": True, "sessionId": recording_dict["sessionId"],
             "recording": recording_dict},
            Path(tmp) / "out", "windows_hisec", session_id="REC-S2")
        assert issues == [], "帧 hash 一致,不得产生取证缺口"
        assert seen_capture_args, "真实收集链必须逐帧取证"
        assert seen_capture_args[0].get("session_id") == "REC-S2", (
            "get_recording_capture 必须携带显式 sessionId 归属")
        traj_root = Path(tmp) / "out" / "trajectories" / "TRJ-0001"
        rev = traj_root / "revisions" / "REV-0001"
        trace_path = rev / "trace.v3.json"
        map_path = rev / "source-map.json"
        assert trace_path.is_file(), "收集链 v3 主轨迹必须落入单夹布局"
        assert map_path.is_file(), "收集链来源映射必须与主轨迹同修订落盘"
        on_disk = GoldenTrace.from_dict(
            json.loads(trace_path.read_text(encoding="utf-8")))
        assert on_disk.to_dict() == artifacts.compilation.golden.to_dict(), (
            "收集链落盘 golden 与内存编译 golden 逐键一致")
        source_map = json.loads(map_path.read_text(encoding="utf-8"))
        assert source_map["schema"] == "edr.source-map/v1"
        assert source_map["byStep"] == \
            artifacts.compilation.diagnostics["sourceEvents"]["byStep"]
        manifest = json.loads(
            (traj_root / "manifest.json").read_text(encoding="utf-8"))
        assert manifest["currentRevision"] == "revisions/REV-0001", (
            "收集链不冒充发布:manifest 指针不动")
        # v3 平铺主轨迹不双写(§2/§5):收集链与 compile 面行为一致。
        assert not (artifacts.directory / "golden-trace.json").exists()
        # 取证帧随收集链落 assets/observations/(与 write_compilation_artifacts
        # 的 _persist_capture_observations 契约一致)。
        assert (artifacts.directory / "assets" / "observations"
                / f"{capture_id}.png").is_file()


def test_v3_collect_artifacts_golden_trace_field_points_to_skipped_flat_path():
    """v3 下 RecordingArtifacts.golden_trace 字段如实指向被跳过的平铺
    路径(轮次 21 核查钉子):下游生产消费方 grep 证实零读取方
    (cli/ui_bridge/rerecord_loop 只用 directory/compilation/issues;
    replay/golden 加载面用 CLI 位置参数与生成的 pytest 投影,不读
    dataclass 字段),故字段保持现状不预改 API——但该行为在此钉死,
    未来若有人接手该字段将在此处撞出语义差异。"""
    import tempfile
    from pathlib import Path
    from agent.recording import artifacts as artifacts_mod
    recording = _with_timeline(
        _recording(
            [_click(1, 100, "CAUSE-1", 100, instance_id="RI-0001")],
            windows=[_MAIN]), instances=("RI-0001",))
    result = compile_recording(recording, profile="windows_hisec")
    with tempfile.TemporaryDirectory() as tmp:
        artifacts = artifacts_mod.write_compilation_artifacts(
            Path(tmp) / "out", recording, result)
        assert artifacts.golden_trace == \
            artifacts.directory / "golden-trace.json", (
            "字段值保持既有构造语义(平铺名)")
        assert not artifacts.golden_trace.exists(), (
            "v3 下平铺文件不写:字段如实指向不存在的路径,零生产消费方"
            "(轮次 21 grep 证据),不预改 API")


def _visual_assets_fixture():
    """轮次 24 写侧共用夹具:真实编译链 + protected=False 的 click +
    digest 对齐帧 → _attach_visual_templates 晋升 visual 声明;另注入
    cleanup 表的 visual 声明(平铺夹已有其模板文件)。返回 (recording,
    result_with_cleanup, cleanup_template_bytes, captures)——captures
    是晋升链的取证帧(测试原样传给 write_compilation_artifacts)。"""
    import dataclasses
    import hashlib
    import io
    from PIL import Image
    from agent.recording.models import ReplaySelector

    png_buf = io.BytesIO()
    Image.new("RGB", (60, 40), (128, 128, 128)).save(png_buf, format="PNG")
    png = png_buf.getvalue()
    capture_id = "CAP-" + "a" * 32
    base = _click(1, 100, "CAUSE-1", 100, instance_id="RI-0001")
    base["observedTarget"] = {**base["observedTarget"], "protected": False}
    recording = _with_timeline(
        _recording([{**base, "evidence": {
            **base["evidence"],
            "beforeCapture": {"id": capture_id,
                              "sha256": "sha256:" + hashlib.sha256(png).hexdigest(),
                              "width": 60, "height": 40, "origin": [0, 0],
                              "redacted": True, "redactions": []}}}],
            windows=[_MAIN]), instances=("RI-0001",))
    result = compile_recording(recording, profile="windows_hisec")
    step1 = result.golden.steps[0]
    # cleanup 表也声明 visual(模板文件由夹具预置在平铺 assets/)。
    cleanup_bytes = b"cleanup-template-bytes-0"
    cleanup_visual = {
        "template": "assets/cleanup-element.png",
        "elementSha256": "sha256:" + hashlib.sha256(cleanup_bytes).hexdigest(),
        "redacted": True, "relativePoint": [0.5, 0.5]}
    cleanup_step = dataclasses.replace(
        step1, step_id="cleanup-0001",
        selector=ReplaySelector(window=step1.selector.window,
                                control=step1.selector.control,
                                visual=cleanup_visual))
    ws2 = dataclasses.replace(
        result.golden.window_sequence,
        cleanup=({"stepId": "cleanup-0001", "windowId": "W1",
                  "effects": ()},))
    golden2 = dataclasses.replace(
        result.golden, cleanup=(cleanup_step,), window_sequence=ws2)
    return (recording, dataclasses.replace(result, golden=golden2),
            cleanup_bytes, {capture_id: png})


def test_v3_compilation_lands_declared_visual_templates_in_trajectory_assets():
    """§5 assets/sha256 落盘第一片(轮次 24 写侧):编译链声明的 visual
    模板(golden selector.visual,steps+cleanup 两表)按 digest 复制进
    TRJ-<id>/assets/sha256/<hex>.png(§5"共享的真实脱敏资源"),落盘
    trace 的模板名改写为单夹相对路径 assets/sha256/<hex>.png(与 §5
    "整夹复制后仍可定位资源"一致,resolver 单根闭包无需第二套解析);
    内存 golden 不动(改写只作用于落盘字典);平铺夹模板原样保留
    (v1/取证消费面不回退)。"""
    import tempfile
    from pathlib import Path
    from agent.recording import artifacts as artifacts_mod
    from agent.recording.generate_pytest import safe_test_name
    from agent.recording.models import GoldenTrace

    recording, result, cleanup_bytes, captures = _visual_assets_fixture()
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "out"
        (out / safe_test_name(recording.name) / "assets").mkdir(parents=True)
        (out / safe_test_name(recording.name) / "assets" /
         "cleanup-element.png").write_bytes(cleanup_bytes)
        artifacts = artifacts_mod.write_compilation_artifacts(
            out, recording, result, captures=captures)
        traj_root = out / "trajectories" / "TRJ-0001"
        on_disk = json.loads(
            (traj_root / "revisions" / "REV-0001" / "trace.v3.json")
            .read_text(encoding="utf-8"))
        for table in ("steps", "cleanup"):
            entry = on_disk[table]["step-0001"] if table == "steps" \
                else on_disk[table][0]
            disk_visual = entry["selector"]["visual"]
            digest_hex = disk_visual["elementSha256"].split(":", 1)[1]
            landed = traj_root / "assets" / "sha256" / f"{digest_hex}.png"
            assert landed.is_file(), \
                f"{table} 声明的 visual 模板必须落入单夹 assets/sha256/"
            assert disk_visual["template"] == f"assets/sha256/{digest_hex}.png"
            context = disk_visual.get("contextTemplate")
            if context is not None:
                assert context == \
                    f"assets/sha256/{disk_visual['contextSha256'].split(':', 1)[1]}.png", \
                    "context 模板同规则落单夹(声明存在时)"
        # 落盘 golden 仍是合法 v3(改写只动模板名,过 from_dict 严格门)。
        reread = GoldenTrace.from_dict(on_disk)
        assert reread.steps[0].selector.visual["template"].startswith(
            "assets/sha256/")
        # 内存 golden 不动:写链返回的 compilation 对象仍持平铺名
        # (改写只作用于落盘字典,不污染内存 CompilationResult)。
        assert artifacts.compilation.golden.steps[0].selector.visual["template"] == \
            "assets/step-0001-element.png"
        assert artifacts.compilation.golden.cleanup[0].selector.visual["template"] == \
            "assets/cleanup-element.png"
        # 平铺夹模板原样保留(v1/取证消费面不回退)。
        flat_assets = artifacts.directory / "assets"
        assert (flat_assets / "step-0001-element.png").is_file()
        assert (flat_assets / "cleanup-element.png").is_file()


def test_v3_compilation_visual_assets_skipped_when_flat_source_missing():
    """digest 门负例:golden 声明的 visual 模板在平铺夹缺失(真实晋升链
    总在声明同时生成模板文件,缺失=手拼 golden/证据面损坏)时不落单夹、
    trace 模板名不改写——不把未验证的声明复制进共享资源面;缺口的
    fail-closed 留给读侧 integrity 门。按模板逐份判:同 trace 里平铺源
    在位的 step 模板正常落单夹,证明是逐份 digest 门而非整表跳过。"""
    import tempfile
    from pathlib import Path
    from agent.recording import artifacts as artifacts_mod

    recording, result, cleanup_bytes, captures = _visual_assets_fixture()
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "out"
        # 平铺夹不预置 cleanup-element.png:cleanup 声明存在、源缺失。
        artifacts = artifacts_mod.write_compilation_artifacts(
            out, recording, result, captures=captures)
        traj_root = out / "trajectories" / "TRJ-0001"
        on_disk = json.loads(
            (traj_root / "revisions" / "REV-0001" / "trace.v3.json")
            .read_text(encoding="utf-8"))
        disk_cleanup = on_disk["cleanup"][0]["selector"]["visual"]
        # 源缺失那份:模板名不改写、digest 不落单夹(不冒充已落)。
        assert disk_cleanup["template"] == "assets/cleanup-element.png", (
            "平铺源缺失时模板名不改写(不冒充已落单夹)")
        cleanup_hex = disk_cleanup["elementSha256"].split(":", 1)[1]
        assert not (traj_root / "assets" / "sha256" /
                   f"{cleanup_hex}.png").exists(), (
            "缺失模板不落单夹:共享资源面零伪造")
        # 对照:step 那份平铺源在位 → 正常落单夹 + 模板名改写(逐份 digest 门)。
        disk_step = on_disk["steps"]["step-0001"]["selector"]["visual"]
        step_hex = disk_step["elementSha256"].split(":", 1)[1]
        assert (traj_root / "assets" / "sha256" / f"{step_hex}.png").is_file(), (
            "源在位的 step 模板仍落单夹:门是逐份 digest,非整表跳过")
        assert disk_step["template"] == f"assets/sha256/{step_hex}.png"


def test_v3_compilation_visual_assets_refused_when_flat_source_tampered():
    """digest 门负例(篡改面):平铺源**存在**但字节与声明 elementSha256
    不符时不落单夹、模板名不改写——被篡改字节若以声明 digest 为名进入
    共享资源面,读侧 digest 门虽会再拦,但落盘时就不该复制未对齐的字节
    (fail-closed 在写侧,不留给读侧补救)。"""
    import tempfile
    from pathlib import Path
    from agent.recording import artifacts as artifacts_mod
    from agent.recording.generate_pytest import safe_test_name

    recording, result, cleanup_bytes, captures = _visual_assets_fixture()
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "out"
        # 平铺夹预置 cleanup-element.png,但内容≠声明 digest(篡改)。
        flat_assets = out / safe_test_name(recording.name) / "assets"
        flat_assets.mkdir(parents=True)
        (flat_assets / "cleanup-element.png").write_bytes(
            b"tampered-bytes-not-matching-declared-digest")
        artifacts = artifacts_mod.write_compilation_artifacts(
            out, recording, result, captures=captures)
        traj_root = out / "trajectories" / "TRJ-0001"
        on_disk = json.loads(
            (traj_root / "revisions" / "REV-0001" / "trace.v3.json")
            .read_text(encoding="utf-8"))
        disk_cleanup = on_disk["cleanup"][0]["selector"]["visual"]
        assert disk_cleanup["template"] == "assets/cleanup-element.png", (
            "digest 不符时模板名不改写(不冒充已对齐落单夹)")
        cleanup_hex = disk_cleanup["elementSha256"].split(":", 1)[1]
        assert not (traj_root / "assets" / "sha256" /
                   f"{cleanup_hex}.png").exists(), (
            "digest 不符的模板不落单夹:不复制未对齐字节")
        # 对照:step 源对齐 → 正常落单夹(门是逐份 digest,篡改不连累他份)。
        disk_step = on_disk["steps"]["step-0001"]["selector"]["visual"]
        step_hex = disk_step["elementSha256"].split(":", 1)[1]
        assert (traj_root / "assets" / "sha256" / f"{step_hex}.png").is_file()
        assert disk_step["template"] == f"assets/sha256/{step_hex}.png"


# ---- §5 单夹布局接线:sources/REC-<id>/ 原始采集落盘(轮次 25) ----

def test_v3_compilation_lands_raw_sources_in_trajectory_folder():
    """§5 sources 正例:v3 编译链落盘时把原始采集证据复制进
    TRJ-<id>/sources/REC-<sessionId>/——recording.json 原件 + 逐份
    digest 门后的取证帧,与平铺原件逐字节一致(单一事实源的两面,
    不漂移);平铺两面不回退(取证消费面/record 目录契约不变);
    sources 目录按 sessionId 命名(§5 REC-<id>),不猜不伪造。"""
    import hashlib
    import io
    import tempfile
    from pathlib import Path
    from agent.recording import artifacts as artifacts_mod
    from PIL import Image

    png_buf = io.BytesIO()
    Image.new("RGB", (60, 40), (128, 128, 128)).save(png_buf, format="PNG")
    png = png_buf.getvalue()
    capture_id = "CAP-" + "b" * 32
    recording = _with_timeline(
        _recording(
            [{**_click(1, 100, "CAUSE-1", 100, instance_id="RI-0001"),
              "evidence": {
                  **_click(1, 100, "CAUSE-1", 100, instance_id="RI-0001")["evidence"],
                  "beforeCapture": {
                      "id": capture_id,
                      "sha256": "sha256:" + hashlib.sha256(png).hexdigest(),
                      "width": 60, "height": 40, "origin": [0, 0],
                      "redacted": True, "redactions": []}}}],
            windows=[_MAIN]), instances=("RI-0001",))
    result = compile_recording(recording, profile="windows_hisec")
    assert result.golden.schema == "edr.desktop-golden-trace/v3"
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "out"
        artifacts = artifacts_mod.write_compilation_artifacts(
            out, recording, result, captures={capture_id: png})
        traj_root = out / "trajectories" / "TRJ-0001"
        # sources 目录按原始采集会话 id 命名(§5 REC-<id>)。
        sources_dir = traj_root / "sources" / recording.session_id
        flat_recording = artifacts.directory / "recording.json"
        src_recording = sources_dir / "recording.json"
        assert src_recording.is_file(), "原始采集 recording.json 原件必须落 sources"
        assert src_recording.read_bytes() == flat_recording.read_bytes(), (
            "sources 原件与平铺原件逐字节一致(不漂移的两面)")
        # 取证帧(digest 门后)随 sources 落盘,与平铺帧逐字节一致。
        src_capture = sources_dir / f"{capture_id}.png"
        flat_capture = artifacts.directory / "assets" / "observations" / f"{capture_id}.png"
        assert src_capture.is_file(), "digest 门后取证帧必须落 sources"
        assert src_capture.read_bytes() == flat_capture.read_bytes() == png
        # 平铺两面不回退:v1/取证消费面契约不变。
        assert flat_recording.is_file()
        assert flat_capture.is_file()


def test_v3_compilation_does_not_overwrite_existing_source_originals():
    """§5 sources 负例:既有 sources 原件不覆盖(§7:raw/审计原件不覆盖)。
    单元级最短反例——TrajectoryLayout.create 恒分配新 TRJ 夹,公开链上
    "同夹二写"不可达(负例若经公开链则通过是平凡的:写进了另一个夹);
    守卫的可达面是同夹追加修订/重放落盘(设计 §5 同 TRJ 追加修订、
    materialize 场景),故直调 _persist_raw_sources 钉守卫本体。"""
    import tempfile
    from pathlib import Path
    from agent.recording import artifacts as artifacts_mod
    recording = _with_timeline(
        _recording(
            [_click(1, 100, "CAUSE-1", 100, instance_id="RI-0001")],
            windows=[_MAIN]), instances=("RI-0001",))
    with tempfile.TemporaryDirectory() as tmp:
        traj_root = Path(tmp) / "TRJ-0001"
        flat = Path(tmp) / "flat"
        (flat / "assets" / "observations").mkdir(parents=True)
        (flat / "recording.json").write_bytes(b'{"new": "content"}')
        sources_dir = traj_root / "sources" / recording.session_id
        sources_dir.mkdir(parents=True)
        prior_recording = sources_dir / "recording.json"
        prior_recording.write_bytes(b'{"prior": "original"}')
        prior_capture = sources_dir / ("CAP-" + "b" * 32 + ".png")
        prior_capture.write_bytes(b"prior-frame")
        # 平铺侧同 id 帧字节不同(模拟重放落盘新帧)。
        (flat / "assets" / "observations" /
         ("CAP-" + "b" * 32 + ".png")).write_bytes(b"new-frame")
        artifacts_mod._persist_raw_sources(traj_root, flat, recording)
        assert prior_recording.read_bytes() == b'{"prior": "original"}', (
            "既有 sources 原件不覆盖(raw 不覆盖,§7)")
        assert prior_capture.read_bytes() == b"prior-frame", (
            "既有 sources 帧不覆盖(逐份 write-once)")


def test_v1_compilation_keeps_flat_sources_without_trajectory_sources():
    """v1 平铺链零 sources 写入(不回退):§5 sources 是 v3 单夹接线面,
    v1/v2 平铺产物与 trajectories/ 面零新增——不单边改 v1 约定。"""
    import tempfile
    from pathlib import Path
    from agent.recording import artifacts as artifacts_mod
    recording = _recording(
        [_click(1, 100, "CAUSE-1", 100, instance_id="RI-0001")],
        windows=[_MAIN])
    result = compile_recording(recording, profile="windows_hisec")
    assert result.golden.schema != "edr.desktop-golden-trace/v3"
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "out"
        artifacts = artifacts_mod.write_compilation_artifacts(out, recording, result)
        assert not (out / "trajectories").exists(), (
            "v1 平铺链不建 trajectories 面(既有约定)")
        assert (artifacts.directory / "recording.json").is_file()


def test_v3_compilation_refuses_path_escape_in_session_sources_name():
    """sources 目录名守卫(最短反例):sessionId 携带路径分隔符/逃逸形状
    时不得以其为 sources 目录名落盘(路径逃逸不进共享证据面),编译链
    其余落盘面照常(不因守卫崩溃)。"""
    import tempfile
    from pathlib import Path
    from agent.recording import artifacts as artifacts_mod
    from target.recording.models import RawRecording
    recording = _with_timeline(
        _recording(
            [_click(1, 100, "CAUSE-1", 100, instance_id="RI-0001")],
            windows=[_MAIN]), instances=("RI-0001",))
    hostile = RawRecording.from_dict({
        **recording.to_dict(), "sessionId": "REC-../../escape"})
    result = compile_recording(hostile, profile="windows_hisec")
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "out"
        artifacts = artifacts_mod.write_compilation_artifacts(out, hostile, result)
        traj_root = out / "trajectories" / "TRJ-0001"
        assert traj_root.is_dir(), "守卫只拒 sources 命名,不拒轨迹落盘"
        assert not (traj_root / "sources" / "REC-..").exists()
        assert not (out.parent / "escape").exists()
        assert (artifacts.directory / "recording.json").is_file()


# ---- §5 单夹布局接线:sources/REC-<id>/ 停止/收集回执落盘(轮次 26) ----

def test_v3_collection_lands_stop_receipt_in_trajectory_sources():
    """§5 sources 收尾正例:收集链(write_compilation_artifacts 尾,三调用方
    cli/ui_bridge/rerecord_loop 共用链同得)把停止/收集回执落
    TRJ-<id>/sources/REC-<sessionId>/stop-receipt.json——字段与链上事实
    逐键一致(goldenStatus/captureIssues 取链上实况,sessionId 按 sources
    命名守卫的同一会话),不含 target 原始 result(recording.json 原件
    已是事实源,回执不复制原始载荷)。平铺两面与 v3 轨迹落盘照常。"""
    import hashlib
    import io
    import tempfile
    from pathlib import Path
    from agent.recording import artifacts as artifacts_mod
    from PIL import Image

    png_buf = io.BytesIO()
    Image.new("RGB", (60, 40), (128, 128, 128)).save(png_buf, format="PNG")
    png = png_buf.getvalue()
    capture_id = "CAP-" + "c" * 32
    recording = _with_timeline(
        _recording(
            [{**_click(1, 100, "CAUSE-1", 100, instance_id="RI-0001"),
              "evidence": {
                  **_click(1, 100, "CAUSE-1", 100, instance_id="RI-0001")["evidence"],
                  "beforeCapture": {
                      "id": capture_id,
                      "sha256": "sha256:" + hashlib.sha256(png).hexdigest(),
                      "width": 60, "height": 40, "origin": [0, 0],
                      "redacted": True, "redactions": []}}}],
            windows=[_MAIN]), instances=("RI-0001",))
    result = compile_recording(recording, profile="windows_hisec")
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "out"
        artifacts = artifacts_mod.write_compilation_artifacts(
            out, recording, result, artifact_issues=[
                {"code": "recording_capture_fetch_failed", "sequence": 1,
                 "evidence": "beforeCapture", "captureId": capture_id}])
        traj_root = out / "trajectories" / "TRJ-0001"
        receipt_path = (traj_root / "sources" / recording.session_id
                        / "stop-receipt.json")
        assert receipt_path.is_file(), "收集链回执必须落 sources/REC-<session>/"
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        assert receipt["sessionId"] == recording.session_id
        assert receipt["goldenStatus"] == result.golden.status, (
            "回执 goldenStatus 与链上编译事实一致")
        assert receipt["captureIssues"] == [
            {"code": "recording_capture_fetch_failed", "sequence": 1,
             "evidence": "beforeCapture", "captureId": capture_id}], (
            "回执 captureIssues 与链上取证缺口一致")
        assert receipt["directory"] == str(artifacts.directory), (
            "回执 directory 指向平铺产物目录")
        assert "recording" not in receipt, (
            "回执不含 target 原始载荷(recording.json 原件已是事实源)")
        # 正例对照:轨迹与 sources 原件落盘照常(回执接线不扰动既有面)。
        assert (traj_root / "revisions" / "REV-0001" / "trace.v3.json").is_file()
        assert (traj_root / "sources" / recording.session_id
                / "recording.json").is_file()


def test_v3_collection_does_not_overwrite_existing_stop_receipt():
    """§5 sources 收尾负例(最短反例,直调回执落盘函数钉守卫本体——
    TrajectoryLayout.create 恒新夹,公开链上同夹二写不可达):既有
    stop-receipt.json 不覆盖(write-once,§7 审计回执不覆盖)。"""
    import tempfile
    from pathlib import Path
    from agent.recording import artifacts as artifacts_mod
    recording = _with_timeline(
        _recording(
            [_click(1, 100, "CAUSE-1", 100, instance_id="RI-0001")],
            windows=[_MAIN]), instances=("RI-0001",))
    with tempfile.TemporaryDirectory() as tmp:
        traj_root = Path(tmp) / "TRJ-0001"
        sources_dir = traj_root / "sources" / recording.session_id
        sources_dir.mkdir(parents=True)
        receipt_path = sources_dir / "stop-receipt.json"
        receipt_path.write_bytes(b'{"prior": "receipt"}')
        artifacts_mod._persist_stop_receipt(
            traj_root, Path(tmp) / "flat", recording,
            golden_status="ready", capture_issues=[])
        assert receipt_path.read_bytes() == b'{"prior": "receipt"}', (
            "既有回执不覆盖(write-once)")


def test_v3_collection_stop_receipt_refuses_path_escape_in_session_name():
    """回执目录名沿用 sources 会话守卫(最短反例):sessionId 携带逃逸
    形状时回执不以其为目录名落盘、不写出 TRJ 夹外;既有 sources 原件
    落盘面照常(守卫只拒命名,不拒轨迹)。"""
    import tempfile
    from pathlib import Path
    from agent.recording import artifacts as artifacts_mod
    from target.recording.models import RawRecording
    recording = _with_timeline(
        _recording(
            [_click(1, 100, "CAUSE-1", 100, instance_id="RI-0001")],
            windows=[_MAIN]), instances=("RI-0001",))
    hostile = RawRecording.from_dict({
        **recording.to_dict(), "sessionId": "REC-../../escape"})
    with tempfile.TemporaryDirectory() as tmp:
        traj_root = Path(tmp) / "TRJ-0001"
        artifacts_mod._persist_stop_receipt(
            traj_root, Path(tmp) / "flat", hostile,
            golden_status="ready", capture_issues=[])
        assert not (traj_root / "sources" / "REC-..").exists()
        assert not (Path(tmp) / "escape").exists()


def test_v1_compilation_keeps_flat_without_stop_receipt():
    """v1 平铺链零回执新增(不回退):stop-receipt 是 v3 单夹接线面,
    v1/v2 平铺产物与 trajectories/ 面零新增——不单边改 v1 约定。"""
    import tempfile
    from pathlib import Path
    from agent.recording import artifacts as artifacts_mod
    recording = _recording(
        [_click(1, 100, "CAUSE-1", 100, instance_id="RI-0001")],
        windows=[_MAIN])
    result = compile_recording(recording, profile="windows_hisec")
    assert result.golden.schema != "edr.desktop-golden-trace/v3"
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "out"
        artifacts = artifacts_mod.write_compilation_artifacts(
            out, recording, result, artifact_issues=[
                {"code": "recording_capture_fetch_failed", "sequence": 1,
                 "evidence": "beforeCapture", "captureId": "CAP-" + "c" * 32}])
        assert not (out / "trajectories").exists(), (
            "v1 平铺链不建 trajectories 面(既有约定)")
        assert not list((artifacts.directory).glob("**/stop-receipt.json")), (
            "v1 平铺链不落 stop-receipt.json")


def test_v3_stop_receipt_carries_persistence_fact_from_result_store():
    """轮次 27 正例(链级):收集链入参 result 带 resultStore(cli record stop
    的 target 直返、rerecord_loop 的 dict(stopped) 两通道都带)时,回执如实
    带 persistence 键且与链上事实逐键一致——持久化事实是 §5 sources 面
    "停止/持久化/收集回执" 的第三块,落盘不冒充不加工。"""
    import tempfile
    from pathlib import Path
    from agent.recording import artifacts as artifacts_mod
    from agent.recording.collection import collect_recording_artifacts
    recording = _with_timeline(
        _recording(
            [_click(1, 100, "CAUSE-1", 100, instance_id="RI-0001")],
            windows=[_MAIN]), instances=("RI-0001",))
    result = compile_recording(recording, profile="windows_hisec")

    class _Agent:
        def call_tool(self, name, arguments=None, timeout=None):
            raise AssertionError(f"无取证事件的链不应调用 agent: {name}")

    result_store_fact = {"ok": True, "freshSave": True, "collected": False,
                         "captures": 0}
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "out"
        artifacts, capture_issues = collect_recording_artifacts(
            _Agent(), {"ok": True, "sessionId": recording.session_id,
                       "recording": recording.to_dict(),
                       "resultStore": result_store_fact},
            out, "windows_hisec")
        receipt = json.loads(
            (out / "trajectories" / "TRJ-0001" / "sources"
             / recording.session_id / "stop-receipt.json").read_text(
                encoding="utf-8"))
        assert receipt["persistence"] == result_store_fact, (
            "回执 persistence 与链上 resultStore 事实逐键一致")
        assert capture_issues == []
        assert artifacts.directory.is_dir()


def test_v3_stop_receipt_persistence_reflects_store_failure_not_silence():
    """轮次 27 正例二:resultStore.ok=False(存储失败是事实不是噪音)时,
    回执 persistence 如实带失败形状——回执不能把存储失败洗成沉默。"""
    import tempfile
    from pathlib import Path
    from agent.recording import artifacts as artifacts_mod
    recording = _with_timeline(
        _recording(
            [_click(1, 100, "CAUSE-1", 100, instance_id="RI-0001")],
            windows=[_MAIN]), instances=("RI-0001",))
    result = compile_recording(recording, profile="windows_hisec")
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "out"
        artifacts_mod.write_compilation_artifacts(
            out, recording, result, artifact_issues=[],
            persistence={"ok": False, "error": "disk full"})
        receipt = json.loads(
            (out / "trajectories" / "TRJ-0001" / "sources"
             / recording.session_id / "stop-receipt.json").read_text(
                encoding="utf-8"))
        assert receipt["persistence"] == {"ok": False, "error": "disk full"}


def test_v3_stop_receipt_without_result_store_fact_has_no_persistence_key():
    """轮次 27 负例:链上无 resultStore 事实(ui_bridge 走 durable
    read-back,组装 dict 只有 ok/recording/sessionId,该通道事实不可达)时,
    回执**无 persistence 键**——缺键=事实不在链上,不写 null 占位冒充。"""
    import tempfile
    from pathlib import Path
    from agent.recording import artifacts as artifacts_mod
    from agent.recording.collection import collect_recording_artifacts
    recording = _with_timeline(
        _recording(
            [_click(1, 100, "CAUSE-1", 100, instance_id="RI-0001")],
            windows=[_MAIN]), instances=("RI-0001",))
    result = compile_recording(recording, profile="windows_hisec")

    class _Agent:
        def call_tool(self, name, arguments=None, timeout=None):
            raise AssertionError(f"无取证事件的链不应调用 agent: {name}")

    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "out"
        collect_recording_artifacts(
            _Agent(), {"ok": True, "sessionId": recording.session_id,
                       "recording": recording.to_dict()},
            out, "windows_hisec")
        receipt = json.loads(
            (out / "trajectories" / "TRJ-0001" / "sources"
             / recording.session_id / "stop-receipt.json").read_text(
                encoding="utf-8"))
        assert "persistence" not in receipt, (
            "无链上事实不得落 persistence 键(不伪造)")
        assert receipt["sessionId"] == recording.session_id


def test_v3_compilation_lands_revision_views_and_readme_in_trajectory_folder():
    """§5 收口正例(轮次 28):编译链落单夹时经 write_revision_views 投影
    `revisions/REV-0001/views/steps.md` 与顶层 `README.md`(§5 宣告面,
    轮次 3 写入口已建、生产链零调用——本片接线,不新造渲染面)。views 从
    盘上 trace.v3.json 渲染:模板名携带单夹改写真相(assets/sha256/<hex>.png,
    与 visual 落盘片一致),README 引用 manifest 与当前修订(夹相对路径,
    整夹复制后仍可读)。views 落盘后回执/原件/模板面照常(零扰动)。"""
    import tempfile
    from pathlib import Path
    from agent.recording import artifacts as artifacts_mod

    recording, result, _cleanup_bytes, captures = _visual_assets_fixture()
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "out"
        artifacts = artifacts_mod.write_compilation_artifacts(
            out, recording, result, captures=captures)
        traj_root = out / "trajectories" / "TRJ-0001"
        steps_md = traj_root / "revisions" / "REV-0001" / "views" / "steps.md"
        readme_md = traj_root / "README.md"
        assert steps_md.is_file(), "§5 宣告的 views/steps.md 必须经编译链落盘"
        assert readme_md.is_file(), "§5 宣告的顶层 README 必须经编译链落盘"
        text = steps_md.read_text(encoding="utf-8")
        for step_id in result.golden.to_dict()["steps"]:
            assert step_id in text, step_id
        # 渲染自盘上真相:visual 声明带单夹相对模板名(轮次 24 改写面),
        # 平铺名不进 views(整夹复制后仍可定位资源)。
        assert "assets/sha256/" in text
        # README 是发现入口的说明面:引用 manifest 与当前修订,夹相对。
        readme = readme_md.read_text(encoding="utf-8")
        assert "manifest.json" in readme
        assert "revisions/REV-0001" in readme
        # 接线零扰动:既有单夹面(主轨迹/回执/原件/资源)照常落盘。
        assert (traj_root / "revisions" / "REV-0001" / "trace.v3.json").is_file()
        assert (traj_root / "sources" / recording.session_id
                / "recording.json").is_file()
        assert (traj_root / "sources" / recording.session_id
                / "stop-receipt.json").is_file()
        assert artifacts.compilation is result or \
            artifacts.compilation.golden is not None


def test_v1_compilation_keeps_flat_layout_without_views_or_readme():
    """兼容不回退(§6.2,轮次 28):v1 平铺链零 views/README 新增——
    views 投影是 v3 单夹接线面,v1 产物不建 trajectories/ 面(既有约定
    由轮次 20 用例钉住,本用例钉 views/README 措辞面)。"""
    import tempfile
    from pathlib import Path
    from agent.recording import artifacts as artifacts_mod
    recording = _recording(
        [_click(1, 100, "CAUSE-1", 100, instance_id="RI-0001")],
        windows=[_MAIN])
    result = compile_recording(recording)
    assert result.golden.schema != "edr.desktop-golden-trace/v3"
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "out"
        artifacts_mod.write_compilation_artifacts(out, recording, result)
        traj_root = out / "trajectories"
        assert not traj_root.exists(), "v1 平铺链不建 trajectories 面"
        assert not list(Path(tmp).rglob("steps.md"))
        assert not list(Path(tmp).rglob("README.md"))


# ---- §5 exports/maa/ 投影落盘(D 阶段第二片,轮次 35):转换器已收 v3
# (轮次 34,schema 门 {v1,v3} 双认),编译链把 to_maa_nodes 产物同步落进
# 修订夹 exports/maa/——§5 宣告的是修订内投影,平铺 maa 节点表照旧
# (one fact, two faces)。转换器缺席/失败时如实不落、不留占位(README
# 模板已宣告"生成前该文件不存在,不预建占位")。

def test_v3_compilation_lands_maa_projection_in_revision_exports():
    """§5 exports/maa 正例:真实编译链 v3 golden 经 to_maa_nodes(轮次 34
    已收 v3)产出节点表,随编译落 `revisions/REV-0001/exports/maa/
    pipeline.json`(§5 宣告名),与平铺 `trace.json` 逐字节一致(one fact,
    two faces);provenance 声明来源 schema 为 v3;平铺 maa 节点表与
    compile-report 的 maaExport 投影照旧不回退。"""
    import tempfile
    from pathlib import Path
    from agent.recording import artifacts as artifacts_mod
    from agent.recording.maa_export import recorder_home
    if recorder_home() is None:
        pytest.skip("edr-cloud-recorder 不在本机:导出是环境附加产物")

    recording = _with_timeline(
        _recording(
            [_click(1, 100, "CAUSE-1", 100, instance_id="RI-0001")],
            windows=[_MAIN]), instances=("RI-0001",))
    result = compile_recording(recording, profile="windows_hisec")
    assert result.golden.schema == "edr.desktop-golden-trace/v3"
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "out"
        artifacts = artifacts_mod.write_compilation_artifacts(
            out, recording, result)
        rev = out / "trajectories" / "TRJ-0001" / "revisions" / "REV-0001"
        export_path = rev / "exports" / "maa" / "pipeline.json"
        flat_path = artifacts.directory / "trace.json"
        assert export_path.is_file(), \
            "§5 宣告的 exports/maa/pipeline.json 必须随编译链落盘"
        assert flat_path.is_file(), "平铺 maa 节点表照旧(one fact, two faces)"
        assert export_path.read_bytes() == flat_path.read_bytes(), \
            "修订内投影与平铺面必须逐字节一致(不双算不漂移)"
        nodes = json.loads(export_path.read_text(encoding="utf-8"))
        assert nodes["$meta"]["attach"]["provenance"]["sourceSchema"] == \
            "edr.desktop-golden-trace/v3", "投影声明真实来源 schema"
        report = json.loads(artifacts.compile_report.read_text())
        assert report["maaExport"]["written"] is True
        assert report["maaExport"]["path"] == "trace.json"


def test_v3_compilation_leaves_exports_empty_when_converter_absent():
    """转换器缺席负例:首次编译转换器在位(平铺 trace.json 与 TRJ-0001
    投影落盘),二次编译 to_maa_nodes 返回 (None, note) 时 exports/maa/
    如实为空——不落 pipeline.json、不留占位(§5"生成前该文件不存在"),
    也不以平铺夹残留的上一轮陈旧字节冒充本次投影;compile-report 的
    maaExport 如实记录 written=False;编译核心产物(主轨迹/source-map/
    views/README)零扰动(附加产物做不成不影响编译——maa_export 契约)。"""
    import tempfile
    from pathlib import Path
    from agent.recording import artifacts as artifacts_mod
    from agent.recording import maa_export as maa_export_mod

    recording = _with_timeline(
        _recording(
            [_click(1, 100, "CAUSE-1", 100, instance_id="RI-0001")],
            windows=[_MAIN]), instances=("RI-0001",))
    result = compile_recording(recording, profile="windows_hisec")
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "out"
        # 首次编译:转换器在位,平铺 trace.json 与 TRJ-0001 投影落盘。
        artifacts = artifacts_mod.write_compilation_artifacts(
            out, recording, result)
        original = maa_export_mod.to_maa_nodes
        maa_export_mod.to_maa_nodes = \
            lambda golden: (None, "找不到 edr-cloud-recorder(测试注入)")
        try:
            # 二次编译(转换器缺席,分配新 TRJ):平铺夹残留上一轮的
            # trace.json 是陈旧字节,不得以其冒充本次修订的投影落新 TRJ。
            absent = artifacts_mod.write_compilation_artifacts(
                out, recording, result)
        finally:
            maa_export_mod.to_maa_nodes = original
        rev = out / "trajectories" / "TRJ-0001" / "revisions" / "REV-0001"
        assert (rev / "exports" / "maa" / "pipeline.json").is_file(), \
            "首次编译(转换器在位)投影落盘——场景前提"
        assert (artifacts.directory / "trace.json").is_file(), \
            "场景前提:平铺夹确有上一轮的节点表"
        report = json.loads(absent.compile_report.read_text())
        assert report["maaExport"]["written"] is False, "缺席如实报告"
        rev2 = out / "trajectories" / "TRJ-0002" / "revisions" / "REV-0001"
        assert not (rev2 / "exports" / "maa" / "pipeline.json").exists(), \
            "转换器缺席时不落投影、不留占位——陈旧平铺字节不冒充本次投影"
        assert (rev2 / "trace.v3.json").is_file(), "核心产物零扰动"
        assert (rev2 / "views" / "steps.md").is_file()
        assert (rev2 / "source-map.json").is_file()


def test_v1_compilation_writes_flat_maa_nodes_without_exports_face():
    """v1 不回退:平铺 maa 节点表照旧(既有 phase0 约定),零 exports/maa
    面——exports 投影是 v3 单夹接线面,v1 不建 trajectories/(既有约定
    轮次 20 用例已钉,本用例钉 maa 平铺面在 v1 下照常 + 无修订夹)。
    夹具用真 v1 形状(无 windows 表):带 windows 表的最小编译产物是 v2,
    v2 是否收进转换器门是头部"需用户决策",不在本片范围。"""
    import tempfile
    from pathlib import Path
    from agent.recording import artifacts as artifacts_mod
    from agent.recording.maa_export import recorder_home
    if recorder_home() is None:
        pytest.skip("edr-cloud-recorder 不在本机:导出是环境附加产物")

    recording = _recording([_click(1, 100, "CAUSE-1", 100)])
    result = compile_recording(recording)
    assert result.golden.schema == "edr.desktop-golden-trace/v1"
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "out"
        artifacts = artifacts_mod.write_compilation_artifacts(
            out, recording, result)
        assert (artifacts.directory / "trace.json").is_file(), \
            "v1 平铺 maa 节点表保持既有行为"
        assert not (out / "trajectories").exists(), "v1 不建 trajectories 面"


# ---- §5 exports/maa/capabilities.json(D 阶段第三片,轮次 36):投影的
# 能力声明面——§5:77 宣告"runtime模式、支持/缺口、来源hash"。三项全部派生
# 自已落盘的 pipeline.json / trace.v3.json 字节:runtime模式与支持/缺口来自
# 投影自身的 scope 标记键存在性扫描(值无关——转换器只在非原生能力上写
# actionScope/verification.scope 键,词表只在转换器一处,零第二份定义);
# 来源hash 取两份落盘字节的 sha256。runtimeId 是实机运行时事实(§6),
# 缺键=未测量,不伪造。投影未落(转换器缺席/失败)时如实不落、不留占位;
# 修订不可变 → write-once;畸形投影如实拒绝不阻塞原生入口(§4:附加面)。

def test_v3_compilation_lands_maa_capabilities_in_revision_exports():
    """§5 capabilities 正例:真实编译链 v3 → 投影落盘后 capabilities.json
    随同落盘——schema 自述、runtime.mode==pure(夹具单 gui.click→Click 属
    maa 原生动作、无断言,探针实测无 scope 标记)、supported/gaps 与投影
    节点逐一对得上、sourceHashes 与两份落盘字节逐字节吻合(能力声明锚定
    它所描述的字节,不锚定内存对象)。"""
    import hashlib
    import tempfile
    from pathlib import Path
    from agent.recording import artifacts as artifacts_mod
    from agent.recording.maa_export import recorder_home
    if recorder_home() is None:
        pytest.skip("edr-cloud-recorder 不在本机:导出是环境附加产物")

    recording = _with_timeline(
        _recording(
            [_click(1, 100, "CAUSE-1", 100, instance_id="RI-0001")],
            windows=[_MAIN]), instances=("RI-0001",))
    result = compile_recording(recording, profile="windows_hisec")
    assert result.golden.schema == "edr.desktop-golden-trace/v3"
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "out"
        artifacts = artifacts_mod.write_compilation_artifacts(
            out, recording, result)
        rev = out / "trajectories" / "TRJ-0001" / "revisions" / "REV-0001"
        cap_path = rev / "exports" / "maa" / "capabilities.json"
        pipeline_path = rev / "exports" / "maa" / "pipeline.json"
        assert pipeline_path.is_file(), "场景前提:投影已落盘(轮次 35)"
        assert cap_path.is_file(), \
            "§5 宣告的 capabilities.json 必须随投影落盘"
        caps = json.loads(cap_path.read_text(encoding="utf-8"))
        assert caps["schema"] == "edr.maa-capabilities/v1"
        assert caps["runtime"]["mode"] == "pure", \
            "单 gui.click 夹具无 scope 标记 → 纯 MAA(§6 纯/桥接措辞)"
        assert caps["supported"] == ["step_0001"]
        assert caps["gaps"] == []
        assert caps["sourceHashes"]["pipeline"] == "sha256:" + hashlib.sha256(
            pipeline_path.read_bytes()).hexdigest(), \
            "来源hash 锚定落盘 pipeline.json 字节"
        assert caps["sourceHashes"]["trace"] == "sha256:" + hashlib.sha256(
            (rev / "trace.v3.json").read_bytes()).hexdigest(), \
            "来源hash 锚定落盘 trace.v3.json 字节"


def test_v3_maa_capabilities_declare_bridged_nodes_and_decline_without_facts():
    """单元级(直调 _persist_maa_capabilities,轮次 25/26 直调模式):
    (a) 桥接声明——带 scope 标记的节点进 gaps(kinds 标明动作/断言哪面
    需桥接),无标记节点进 supported,mode=bridged;(b) 投影缺席/畸形/
    非对象/零节点/主轨迹锚缺席 → 如实不落不留占位(空 pipeline 不得产出
    "pure" 空声明);(c) write-once——既有文件字节不同预置 → 逐字节不动
    (修订不可变,轮次 25/26 模式)。"""
    import tempfile
    from pathlib import Path
    from agent.recording import artifacts as artifacts_mod

    bridged_pipeline = {
        "$meta": {"attach": {"provenance": {"sourceSchema": "x"}}},
        "step_0001": {"attach": {"provenance": {"sourceStepId": "step-0001"}}},
        "step_0002": {"attach": {
            "provenance": {"sourceStepId": "step-0002",
                           "actionScope": "desktop-only"},
            "verification": {"scope": "desktop-only", "verifiers": []}}},
        "step_0003": {"attach": {
            "provenance": {"sourceStepId": "step-0003"},
            "verification": {"scope": "desktop-only"}}},
    }
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        rev = root / "revisions" / "REV-0001"
        maa_dir = rev / "exports" / "maa"
        maa_dir.mkdir(parents=True)
        (rev / "trace.v3.json").write_text("{}", encoding="utf-8")
        (maa_dir / "pipeline.json").write_text(
            json.dumps(bridged_pipeline), encoding="utf-8")
        artifacts_mod._persist_maa_capabilities(root)
        cap_path = maa_dir / "capabilities.json"
        assert cap_path.is_file(), "投影在位时能力声明落盘"
        caps = json.loads(cap_path.read_text(encoding="utf-8"))
        assert caps["schema"] == "edr.maa-capabilities/v1"
        assert caps["runtime"]["mode"] == "bridged", \
            "任一节点带 scope 标记 → 桥接(§6:明确标注依赖)"
        assert caps["supported"] == ["step_0001"]
        assert caps["gaps"] == [
            {"node": "step_0002", "kinds": ["action", "verification"],
             "sourceStepId": "step-0002"},
            {"node": "step_0003", "kinds": ["verification"],
             "sourceStepId": "step-0003"},
        ], "gaps 逐节点标明桥接面,kinds 与标记所在键对应"
        # write-once:字节不同预置 → 逐字节不动(修订不可变)。
        cap_path.write_text('{"schema": "preset"}', encoding="utf-8")
        artifacts_mod._persist_maa_capabilities(root)
        assert cap_path.read_text(encoding="utf-8") == '{"schema": "preset"}'

        # 投影缺席:无 pipeline.json → 不落不留占位。
        empty_root = root / "empty"
        empty_rev = empty_root / "revisions" / "REV-0001"
        (empty_rev / "exports" / "maa").mkdir(parents=True)
        (empty_rev / "trace.v3.json").write_text("{}", encoding="utf-8")
        artifacts_mod._persist_maa_capabilities(empty_root)
        assert not (empty_rev / "exports" / "maa"
                    / "capabilities.json").exists()

        # 畸形投影(非 JSON / 非对象):如实拒绝,不阻塞原生入口。
        empty_pipeline = empty_rev / "exports" / "maa" / "pipeline.json"
        empty_pipeline.write_text("not json", encoding="utf-8")
        artifacts_mod._persist_maa_capabilities(empty_root)
        assert not (empty_rev / "exports" / "maa"
                    / "capabilities.json").exists()
        empty_pipeline.write_text("[1, 2]", encoding="utf-8")
        artifacts_mod._persist_maa_capabilities(empty_root)
        assert not (empty_rev / "exports" / "maa"
                    / "capabilities.json").exists()

        # 零节点投影:不得产出无据的 "pure" 空声明。
        (empty_rev / "exports" / "maa" / "pipeline.json").write_text(
            json.dumps({"$meta": {}}), encoding="utf-8")
        artifacts_mod._persist_maa_capabilities(empty_root)
        assert not (empty_rev / "exports" / "maa"
                    / "capabilities.json").exists()

        # 主轨迹锚缺席:pipeline 在而 trace.v3.json 缺 → 来源hash 无法成立,
        # 不落(能力声明必须锚定它所描述的字节)。
        no_trace_root = root / "no-trace"
        no_trace_rev = no_trace_root / "revisions" / "REV-0001"
        (no_trace_rev / "exports" / "maa").mkdir(parents=True)
        (no_trace_rev / "exports" / "maa" / "pipeline.json").write_text(
            json.dumps(bridged_pipeline), encoding="utf-8")
        artifacts_mod._persist_maa_capabilities(no_trace_root)
        assert not (no_trace_rev / "exports" / "maa"
                    / "capabilities.json").exists()


def test_v3_compilation_leaves_capabilities_absent_when_projection_absent():
    """链级负例(轮次 35 负例镜像):首次编译投影与能力声明全落(场景前提),
    二次编译转换器缺席 → TRJ-0002 无 pipeline.json 也无 capabilities.json
    ——陈旧平铺字节不冒充本次投影,能力声明不以缺失的投影为据;核心产物
    零扰动。"""
    import tempfile
    from pathlib import Path
    from agent.recording import artifacts as artifacts_mod
    from agent.recording import maa_export as maa_export_mod

    recording = _with_timeline(
        _recording(
            [_click(1, 100, "CAUSE-1", 100, instance_id="RI-0001")],
            windows=[_MAIN]), instances=("RI-0001",))
    result = compile_recording(recording, profile="windows_hisec")
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "out"
        artifacts = artifacts_mod.write_compilation_artifacts(
            out, recording, result)
        original = maa_export_mod.to_maa_nodes
        maa_export_mod.to_maa_nodes = \
            lambda golden: (None, "找不到 edr-cloud-recorder(测试注入)")
        try:
            absent = artifacts_mod.write_compilation_artifacts(
                out, recording, result)
        finally:
            maa_export_mod.to_maa_nodes = original
        rev1 = out / "trajectories" / "TRJ-0001" / "revisions" / "REV-0001"
        assert (rev1 / "exports" / "maa" / "pipeline.json").is_file(), \
            "场景前提:首次编译投影落盘"
        assert (rev1 / "exports" / "maa" / "capabilities.json").is_file(), \
            "场景前提:首次编译能力声明落盘"
        report = json.loads(absent.compile_report.read_text())
        assert report["maaExport"]["written"] is False, "缺席如实报告"
        rev2 = out / "trajectories" / "TRJ-0002" / "revisions" / "REV-0001"
        assert not (rev2 / "exports" / "maa" / "pipeline.json").exists()
        assert not (rev2 / "exports" / "maa"
                    / "capabilities.json").exists(), \
            "投影未落时能力声明不落、不留占位——不冒充"
        assert (rev2 / "trace.v3.json").is_file(), "核心产物零扰动"
        assert (rev2 / "views" / "steps.md").is_file()
        assert (rev2 / "source-map.json").is_file()


# ---- §5 E 阶段第一片(轮次 38):整夹搬动验证——"搬动目录后仍可消费"的
# 端到端契约面(§8:121 E 行出口第三事实)。契约面各件已各有钉死用例
# (manifest folder-relative 守卫 / assets TRJ 根相对改写 / from_manifest
# 读侧守卫 / source-map/views/exports 全夹相对),本片把"整夹复制后逐面
# 可消费"合成一条真实编译链证据。converter 缺席环境(本机无
# edr-cloud-recorder 时)capabilities/pipeline 面按既有约定 skip,
# 其余面照常验证。

def test_v3_trajectory_folder_survives_full_folder_move_and_stays_consumable():
    """E 阶段整夹搬动验证(轮次 38):真实编译链 TRJ-<id> 夹 → shutil.
    copytree 整夹复制到另一根(模拟搬动,原始保持不动)→ 从新根逐面消费:
    manifest 发现入口解析、主轨迹过严格门重读、views/README 存在且夹相对、
    sources/assets/runs 骨架与原件随夹、exports 双文件 sha256 与搬动后
    字节吻合(capabilities 锚定它所描述的字节)、盘上无旧根绝对路径残留。
    发现真实缺口(路径绝对/锚错根)即本用例红。"""
    import hashlib
    import shutil
    import tempfile
    from pathlib import Path
    from agent.recording import artifacts as artifacts_mod
    from agent.recording.maa_export import recorder_home
    from agent.recording.models import GoldenTrace
    from agent.recording.trajectory_layout import from_manifest

    recording = _with_timeline(
        _recording(
            [_click(1, 100, "CAUSE-1", 100, instance_id="RI-0001")],
            windows=[_MAIN]), instances=("RI-0001",))
    result = compile_recording(recording, profile="windows_hisec")
    assert result.golden.schema == "edr.desktop-golden-trace/v3"
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "out"
        artifacts_mod.write_compilation_artifacts(out, recording, result)
        traj_root = out / "trajectories" / "TRJ-0001"
        assert traj_root.is_file() is False and traj_root.is_dir()

        # 整夹复制到另一根(§7"迁移先复制";真实搬动=复制+另行确认,
        # 本验证片只钉"复制后仍可消费",不删除原夹)。
        moved_root = Path(tmp) / "moved" / "trajectories" / "TRJ-0001"
        moved_root.parent.mkdir(parents=True)
        shutil.copytree(traj_root, moved_root)
        assert traj_root.is_dir(), "验证片只复制不删原(§7 删除另行确认)"

        # (1) 发现入口:manifest 是唯一入口,from_manifest 从新根解析当前
        # 修订主轨迹——零猜测零回退(指针 folder-relative 守卫既有用例钉死)。
        resolved = from_manifest(moved_root)
        assert resolved == moved_root / "revisions" / "REV-0001" / "trace.v3.json"

        # (2) 主轨迹:新根文件过 from_dict 严格门重读,与搬动前逐字节一致
        # (复制不加工)。
        original_bytes = (traj_root / "revisions" / "REV-0001"
                          / "trace.v3.json").read_bytes()
        moved_bytes = resolved.read_bytes()
        assert moved_bytes == original_bytes
        on_disk = json.loads(moved_bytes.decode("utf-8"))
        GoldenTrace.from_dict(on_disk)  # 严格门:形状失配在此红

        # (3) 来源映射:随夹落盘,与诊断逐键一致(source-map 是编译链
        # 无条件派生面,轮次 19/20)。
        source_map = json.loads(
            (moved_root / "revisions" / "REV-0001" / "source-map.json")
            .read_text(encoding="utf-8"))
        assert source_map["schema"] == "edr.source-map/v1"
        assert source_map["byStep"] == \
            result.diagnostics["sourceEvents"]["byStep"]

        # (4) views/README:存在且夹相对——整夹复制后读到的文字不含旧根
        # 绝对路径(§5"所有资源与报告使用本夹相对路径")。
        steps_md = (moved_root / "revisions" / "REV-0001" / "views"
                    / "steps.md").read_text(encoding="utf-8")
        readme_md = (moved_root / "README.md").read_text(encoding="utf-8")
        assert str(tmp) not in steps_md, \
            "steps.md 残留旧根绝对路径=搬动后报告指旧根(§5 夹相对违例)"
        assert str(tmp) not in readme_md
        assert "manifest.json" in readme_md
        assert "revisions/REV-0001" in readme_md
        for step_id in on_disk["steps"]:
            assert step_id in steps_md

        # (5) sources/assets/runs/recovery 骨架与原件随夹。
        assert (moved_root / "sources" / recording.session_id
                / "recording.json").is_file()
        assert (moved_root / "sources" / recording.session_id
                / "stop-receipt.json").is_file()
        for fixed in ("sources", "assets/sha256", "runs", "recovery"):
            assert (moved_root / fixed).is_dir(), fixed

        # (6) exports/maa 双文件:投影与能力声明随夹,sourceHashes 锚定的
        # 字节在新根仍吻合(能力声明描述的就是这两份字节);转换器缺席
        # 环境按既有约定 skip 本面(其余面照常)。
        if recorder_home() is None:
            pytest.skip("edr-cloud-recorder 不在本机:导出是环境附加产物")
        rev_moved = moved_root / "revisions" / "REV-0001"
        pipeline = rev_moved / "exports" / "maa" / "pipeline.json"
        capabilities = rev_moved / "exports" / "maa" / "capabilities.json"
        assert pipeline.is_file() and capabilities.is_file()
        assert pipeline.read_bytes() == \
            (traj_root / "revisions" / "REV-0001" / "exports" / "maa"
             / "pipeline.json").read_bytes(), "投影随夹逐字节一致"
        caps = json.loads(capabilities.read_text(encoding="utf-8"))
        assert caps["sourceHashes"]["trace"] == "sha256:" + hashlib.sha256(
            resolved.read_bytes()).hexdigest(), \
            "能力声明锚定的 trace 字节在搬动后仍吻合"
        assert caps["sourceHashes"]["pipeline"] == "sha256:" + hashlib.sha256(
            pipeline.read_bytes()).hexdigest(), \
            "能力声明锚定的 pipeline 字节在搬动后仍吻合"


def test_v3_compilation_skips_default_pytest_scaffold_repeat():
    """减容(§4.2 默认不再重复生成 pytest):v3 编译的 pytest 导出显式
    UNSUPPORTED,平铺 conftest.py/pytest.ini/test_<name>.py 三件脚手架
    只服务不存在的 pytest 导出——v3 门内不再默认写;v1 平面照旧
    (既有 phase0 断言不回退);核心产物与 TRJ 面零扰动。"""
    import tempfile
    from pathlib import Path
    from agent.recording import artifacts as artifacts_mod
    recording = _with_timeline(
        _recording(
            [_click(1, 100, "CAUSE-1", 100, instance_id="RI-0001")],
            windows=[_MAIN]), instances=("RI-0001",))
    result = compile_recording(recording, profile="windows_hisec")
    assert result.golden.schema == "edr.desktop-golden-trace/v3"
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "out"
        artifacts = artifacts_mod.write_compilation_artifacts(out, recording, result)
        flat_names = {p.name for p in artifacts.directory.iterdir()}
        for scaffold in ("conftest.py", "pytest.ini",
                         "test_" + artifacts.directory.name + ".py"):
            assert scaffold not in flat_names, (
                f"v3 编译不再默认写 pytest 脚手架 {scaffold}")
        # 核心产物与投影面零扰动。
        assert "recording.json" in flat_names
        assert "case.json" in flat_names
        assert "compile-report.json" in flat_names
        assert (out / "trajectories" / "TRJ-0001" / "revisions" / "REV-0001"
                / "trace.v3.json").is_file()
        report = json.loads(artifacts.compile_report.read_text())
        assert report.get("pytestExport", {}).get("supported") is False


def test_v1_compilation_keeps_pytest_scaffold_flat_files():
    """v1 平面不回退:conftest.py/pytest.ini/test_<name>.py 照旧落盘
    (phase0 既有断言的同面钉子,减容门只作用于 v3)。"""
    import tempfile
    from pathlib import Path
    from agent.recording import artifacts as artifacts_mod
    recording = _recording([_click(1, 100, "CAUSE-1", 100)])
    result = compile_recording(recording)
    assert result.golden.schema != "edr.desktop-golden-trace/v3"
    with tempfile.TemporaryDirectory() as tmp:
        artifacts = artifacts_mod.write_compilation_artifacts(
            Path(tmp) / "out", recording, result)
        flat_names = {p.name for p in artifacts.directory.iterdir()}
        assert "conftest.py" in flat_names
        assert "pytest.ini" in flat_names
        assert "test_" + artifacts.directory.name + ".py" in flat_names
