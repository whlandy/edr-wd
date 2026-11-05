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
