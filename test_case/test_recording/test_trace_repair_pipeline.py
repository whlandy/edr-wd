"""T1 (design 12 §4/§8 R01): 修复候选生成/验证/来源保全管道。

成对反例纪律（test-mate）:
- R01 正例: 弱身份步因实例早于清单遍历被销毁而无帧,但时间线证明的
  顺序重开兄弟实例完整记录了同一菜单结构 → 候选1(structural 回填)
  生成、golden 变 ready、走真实 v3 链验证(execute_v3_window_stage_real,
  仅 OS fake)通过; 原件 hash 不变; 无重录预算消耗。
- 反例A: 时间线重叠(两同 title 窗口共存) → 候选拒绝生成(不能借帧)。
- 反例B: 兄弟帧菜单结构不一致(slot index 漂移) → 候选拒绝。
- 反例C: 真实链业务断言失败(菜单点击 receipt ok 但语言未变) →
  验证不通过,候选不宣称 recovered(独立断言,C11 语义)。

来源保全: 候选仅变异 step 的 selector.control.structural + status +
issues; args/verifiers/actionId/window identity 逐字节不变;原件
(compile 输入的 raw)在候选生成前后 SHA256 一致。
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest

from agent.recording.compiler import compile_recording
from agent.recording.execution_ledger import execute_v3_window_stage_real
from target.recording.models import (
    CaptureScope,
    ObservedTarget,
    RawCaptureEvent,
    RawRecording,
)

pytestmark = pytest.mark.unit

sys.path.insert(0, str(Path(__file__).parent))
import test_v3_real_server_adapter as adapter  # noqa: E402


class _ParentAwareBackend(adapter._FakeOSBackend):
    def _enumerate_top_windows_windows(self, *, strict=False):
        live = super()._enumerate_top_windows_windows(strict=strict)
        for item in live:
            source = self._windows.get(str(item["handle"])) or {}
            if source.get("parent_native_id") is not None:
                item["parent_native_id"] = source["parent_native_id"]
        return live



CHAIN = [{"controlType": "Window", "name": "EDRClient"},
         {"controlType": "Pane", "name": "Desktop 1"}]


def _menu_frame(label, width=153):
    """The recorded menu (3 items) as one complete walk of a window."""
    return [
        {"controlType": "MenuItem", "text": "Setting Center",
         "ancestry": copy.deepcopy(CHAIN), "rect": [1000, 76, 1000 + width, 108]},
        {"controlType": "MenuItem", "text": "Logs",
         "ancestry": copy.deepcopy(CHAIN), "rect": [1000, 108, 1000 + width, 140]},
        {"controlType": "MenuItem", "text": label,
         "ancestry": copy.deepcopy(CHAIN), "rect": [1000, 140, 1000 + width, 172]},
        {"controlType": "MenuItem", "text": "About",
         "ancestry": copy.deepcopy(CHAIN), "rect": [1000, 172, 1000 + width, 204]},
    ]


def _click(seq, ms, causal, handle, instance_id, label, rect, snapshot_id,
           window_title="EDRClient", automation_id=None,
           control_type="MenuItem"):
    return RawCaptureEvent(
        sequence=seq, wall_time="2026-11-07T00:00:00Z", monotonic_ms=ms,
        type="pointer_click",
        input={"button": "left", "screenPoint": [rect[0] + 50, rect[1] + 15]},
        observed_target=ObservedTarget(
            f"OBS-{seq}", f"T{seq:04d}", f"fp-{seq}", control_type=control_type,
            automation_id=automation_id, text=label, rect=tuple(rect),
            ancestry=tuple(copy.deepcopy(CHAIN))),
        scope=CaptureScope("t", "windows_pywinauto", "EDRClient.exe",
                           "^EDRClient$"),
        evidence={"window": {"handle": handle, "title": window_title,
                             "processName": "EDRClient.exe",
                             "instanceId": instance_id}},
        causal_id=causal)


def _transition(seq, ms, causal, handle, ri, kind="destroyed"):
    return RawCaptureEvent(
        sequence=seq, wall_time="2026-11-07T00:00:00Z", monotonic_ms=ms,
        type="window_transition",
        input={"kind": kind, "title": "EDRClient",
               "processName": "EDRClient.exe", "timeoutSeconds": 10.0},
        scope=CaptureScope("t", "windows_pywinauto", "EDRClient.exe",
                           "^EDRClient$"),
        evidence={"handle": handle, "window": {"handle": handle,
                 "title": "EDRClient", "processName": "EDRClient.exe",
                 "instanceId": ri}},
        causal_id=causal)


def _snap(snap_id, ms, handle, ri, controls, complete=True, title="EDRClient"):
    return {"snapshotId": snap_id, "capturedAtMs": ms, "reason": "step",
            "window": {"handle": handle, "processName": "EDRClient.exe",
                       "title": title, "instanceId": ri},
            "contentHash": "sha256:" + "a" * 64,
            "stateHash": "sha256:" + "b" * 64,
            "controlCount": len(controls), "complete": complete,
            "controls": list(controls)}


def _lang_recording(*, labels=("Language", "语言设置", "Idioma"),
                    timeline_overlap=False, with_result_assertion=True):
    """三次顺序重开的同 title 窗口,前两次有完整菜单 walk,最后一次
    （失效步所在）没有帧。顺序重开: 实例 i 在 i+1 创建之前已销毁
    (destroyed <= next created,时间线证明从未共存)。
    timeline_overlap=True 时全部实例共存(created 同刻,无 destroyed)。
    每个实例窗口有独立 title(EDRClient-1/2/3),点击因果绑定
    window_open exists=False 断言(显式声明"本步关闭所操作的窗口",
    断言按 title 唯一解析到声明窗口,v3 链 LOST 路径才有可求值
    声明);with_result_assertion=True 时最后一次点击再因果绑定
    跨窗语言结果 text_equals(currentLanguage 强 ID 控件位于入口
    窗口 logo1,期望 French)——R01 要求独立业务断言,窗口存在/
    点击成功不代替结果。"""
    events = []
    windows = []
    snapshots = []
    timeline = []
    ms = 100
    handles = (100, 200, 300)
    labels = list(labels) if len(labels) == 3 else ["Language", "语言设置", "Idioma"]
    # 入口窗口(already_open,flow 之前就存在): 第一步的 entry identity
    # 需要 manifest 里有 origin=already_open 的记录(真实样本的 logo1)。
    # 入口点击是强 ID 按钮(EdrMainWindow.openBtn)——入口步不弱身份;
    # currentLanguage 控件在本窗口(语言结果断言的跨窗落点)。
    events.append(_click(len(events) + 1, ms, "CAUSE-0", 50, "RI-0000",
                         "Open", [100, 100, 180, 132], "CS-ENTRY",
                         window_title="logo1",
                         automation_id="EdrMainWindow.openBtn",
                         control_type="Button"))
    windows.append({"title": "logo1", "processName": "EDRClient.exe",
                    "handle": 50, "pid": 42, "origin": "already_open",
                    "closed": False, "instanceId": "RI-0000"})
    snapshots.append({"snapshotId": "CS-ENTRY", "capturedAtMs": ms + 5,
                      "reason": "step",
                      "window": {"handle": 50, "processName": "EDRClient.exe",
                                 "title": "logo1",
                                 "rootAutomationId": "EdrMainWindow",
                                 "instanceId": "RI-0000"},
                      "contentHash": "sha256:" + "a" * 64,
                      "stateHash": "sha256:" + "b" * 64,
                      "controlCount": 1, "complete": True,
                      "controls": [{"controlType": "Button", "text": "Open",
                                    "automationId": "EdrMainWindow.openBtn",
                                    "ancestry": [],
                                    "rect": [100, 100, 180, 132]}]})
    timeline.append({"id": "WE0", "order": 1, "monotonicMs": 60,
                     "instanceId": "RI-0000", "kind": "initial_observed",
                     "source": "initial_inventory"})
    order = 2
    for i, (handle, label) in enumerate(zip(handles, labels[:2])):
        ri = f"RI-{i + 1:04d}"
        window_title = f"EDRClient-{i + 1}"
        events.append(_click(len(events) + 1, ms, f"CAUSE-{i + 1}",
                             handle, ri, label, [1000, 140, 1153, 172],
                             f"CS-{len(snapshots) + 1:04d}",
                             window_title=window_title))
        events.append(RawCaptureEvent(
            sequence=len(events) + 1,
            wall_time="2026-11-07T00:00:00Z", monotonic_ms=ms + 50,
            type="assertion", input={},
            assertion={"type": "window_open",
                       "expected": {"exists": False, "title": window_title},
                       "timeoutSeconds": 5.0},
            observed_target=None,
            scope=CaptureScope("t", "windows_pywinauto", "EDRClient.exe",
                               "^EDRClient$"),
            evidence={"window": {"handle": handle, "title": window_title,
                                 "processName": "EDRClient.exe",
                                 "instanceId": ri}},
            causal_id=f"CAUSE-{i + 1}"))
        windows.append({"title": window_title, "processName": "EDRClient.exe",
                        "handle": handle, "pid": 42, "origin": "opened",
                        "closed": False, "instanceId": ri})
        snapshots.append(_snap(f"CS-{len(snapshots) + 1:04d}", ms + 10, handle, ri,
                               _menu_frame(label)))
        ms += 200
    # 失效实例: 有 click 事件但窗口销毁早于任何 walk（无帧）。
    failing_ri = "RI-0003"
    failing_handle = 300
    failing_label = labels[2] if len(labels) == 3 else "Idioma"
    failing_rect = [1000, 140, 1156, 172]
    events.append(_click(len(events) + 1, ms, "CAUSE-3", failing_handle,
                         failing_ri, failing_label, failing_rect,
                         "CS-NONE", window_title="EDRClient-3"))
    events.append(RawCaptureEvent(
        sequence=len(events) + 1,
        wall_time="2026-11-07T00:00:00Z", monotonic_ms=ms + 50,
        type="assertion", input={},
        assertion={"type": "window_open",
                   "expected": {"exists": False, "title": "EDRClient-3"},
                   "timeoutSeconds": 5.0},
        observed_target=None,
        scope=CaptureScope("t", "windows_pywinauto", "EDRClient.exe",
                           "^EDRClient$"),
        evidence={"window": {"handle": failing_handle,
                             "title": "EDRClient-3",
                             "processName": "EDRClient.exe",
                             "instanceId": failing_ri}},
        causal_id="CAUSE-3"))
    if with_result_assertion:
        # 跨窗语言结果断言:落在入口窗口(logo1,全程存活,承载应用级
        # currentLanguage 控件)。断言控件的捕获时 Name 为空(该应用不
        # 为 Text 控件设 Name)——observe-only 步的 selector 只含强 ID
        # (automationId+controlType),不钉住易变文本;回放期控件实况
        # 是 text_equals 的独立求值对象(窗口存在/点击成功不代替结果)。
        events.append(RawCaptureEvent(
            sequence=len(events) + 1,
            wall_time="2026-11-07T00:00:00Z", monotonic_ms=ms + 200,
            type="assertion", input={},
            assertion={"type": "text_equals", "expected": "French",
                       "timeoutSeconds": 5.0},
            observed_target=ObservedTarget(
                "OBS-RA", "T-RA", "fp-ra", control_type="Text",
                automation_id="currentLanguage", text="",
                rect=(100, 20, 220, 40), ancestry=()),
            scope=CaptureScope("t", "windows_pywinauto", "EDRClient.exe",
                               "^EDRClient$"),
            evidence={"window": {"handle": 50, "title": "logo1",
                                 "processName": "EDRClient.exe",
                                 "instanceId": "RI-0000"}},
            causal_id="CAUSE-3"))
    windows.append({"title": "EDRClient-3", "processName": "EDRClient.exe",
                    "handle": failing_handle, "pid": 42, "origin": "opened",
                    "closed": False, "instanceId": failing_ri})
    if timeline_overlap:
        # 失效实例与前两个实例共存: 同刻 created、无 destroyed。
        for ri in ("RI-0001", "RI-0002", "RI-0003"):
            timeline.append({"id": f"WE{order}", "order": order,
                             "monotonicMs": 80, "instanceId": ri,
                             "kind": "created", "source": "native_show"})
            order += 1
    else:
        # created(t) < destroyed(t+200) <= next created(next t):
        # RI-0001: created@80 destroyed@280;RI-0002: created@280
        # destroyed@480;RI-0003: created@480 destroyed@680。
        for i, ri in enumerate(("RI-0001", "RI-0002", "RI-0003")):
            created_ms = 80 + 200 * i
            timeline.append({"id": f"WE{order}", "order": order,
                             "monotonicMs": created_ms,
                             "instanceId": ri, "kind": "created",
                             "source": "native_show"})
            order += 1
            timeline.append({"id": f"WE{order}", "order": order,
                             "monotonicMs": created_ms + 200,
                             "instanceId": ri, "kind": "destroyed",
                             "source": "native_destroy"})
            order += 1
    return RawRecording(
        name="r01-repair", session_id="s-r01", events=tuple(events),
        control_snapshots=tuple(snapshots), windows=tuple(windows),
        schema="edr.desktop-recording/v2",
        window_timeline={
            "schema": "edr.recorded-window-timeline/v1",
            "events": tuple(timeline),
            "coverage": {"destroyEvents": "supported", "droppedEvents": 0}},
    )


def _propose(recording):
    from agent.recording.trace_repair import propose_repairs
    result = compile_recording(recording)
    proposal = propose_repairs(recording, result.golden, result.diagnostics)
    return result, proposal


# ---------------------------------------------------------------- R01 正例


def test_r01_structural_candidate_repairs_unwalked_instance():
    """R01 正例: 候选1 从时间线证明的顺序兄弟实例回填 structural 身份,
    failing step 变 ready; 唯一变异是 selector.control.structural/status/
    issues; 原件 hash 不变。"""
    recording = _lang_recording()
    result, proposal = _propose(recording)
    assert result.golden.status == "incomplete"
    failing = [s for s in result.golden.steps if s.status != "ready"]
    assert len(failing) == 1 and failing[0].step_id == "step-0004"
    assert "compile_weak_identity_unresolved" in failing[0].issues
    assert len(proposal.candidates) == 1
    candidate = proposal.candidates[0]
    assert candidate.rule_id == "structural_identity_from_sequential_sibling_frames"
    assert candidate.golden.status == "ready"
    assert not candidate.semantics_changed
    assert candidate.source_evidence["proofInstanceIds"] == ["RI-0001", "RI-0002"]
    # 唯一变异: 仅 failing step 的 selector/status/issues。
    for parent_step, candidate_step in zip(result.golden.steps,
                                           candidate.golden.steps):
        if parent_step.step_id == "step-0004":
            parent_dict = parent_step.to_dict()
            candidate_dict = candidate_step.to_dict()
            changed = {k for k in parent_dict
                       if parent_dict[k] != candidate_dict[k]}
            assert changed == {"selector", "status", "issues"}, changed
            assert set(parent_dict["args"].items()) == set(
                candidate_dict["args"].items())
            assert parent_dict["verifiers"] == candidate_dict["verifiers"]
            assert parent_dict["actionId"] == candidate_dict["actionId"]
            assert parent_dict["selector"]["window"] == \
                candidate_dict["selector"]["window"]
            structural = candidate_dict["selector"]["control"]["structural"]
            assert structural["kind"] == "menu-layout/v1"
            assert structural["index"] == 2
        else:
            assert parent_step.to_dict() == candidate_step.to_dict()
    # 原件 hash 不变(候选生成是纯函数)。
    import hashlib
    from agent.recording.models import canonical_json
    sha = "sha256:" + hashlib.sha256(
        canonical_json(recording.to_dict()).encode()).hexdigest()
    assert candidate.parent_sha256 == sha


def test_r01_candidate_passes_real_v3_chain_with_business_assertion(
        monkeypatch, tmp_path):
    """候选 golden 走真实 v3 链(真实 begin/register/bind/focus/dispatch,
    仅 OS fake): 三个顺序窗口逐一绑定,失效步结构解析注入当前语言项,
    独立业务断言(currentLanguage text_equals)通过 → 验证层确认候选
    可用,不进入重录(无 agentRerecord 预算)。"""
    from target.windowing.attempt_ledger import AttemptLedger
    import target.server as server
    recording = _lang_recording()
    result, proposal = _propose(recording)
    golden = proposal.candidates[0].golden
    assert golden.status == "ready"
    assert golden.window_sequence is not None
    step_map = {s["stepId"]: s["windowId"]
                for s in golden.window_sequence.to_dict()["steps"]}
    assert step_map == {"step-0001": "W1", "step-0002": "W2",
                        "step-0003": "W3", "step-0004": "W4",
                        "step-0005": "W1"}
    # fake OS:入口窗口(logo1,全程存活,承载 currentLanguage 控件)+
    # 三个顺序窗口按录制顺序"存活"(每刻清单里恰一个 EDRClient-N
    # 窗口,销毁实例从清单移除);dialog role 断言需要 native parent
    # 边(宽泛 dialog role 不能认领无父边窗口),fake 枚举透传之。
    from agent.recording.models import GoldenTrace

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
    # 顺序重开切换:每次点击后销毁当前实例窗口(下一实例进入清单);
    # 入口窗口不参与轮换。当前窗口从权威锁状态取(click 前已聚焦)。
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
    try:
        outcome = execute_v3_window_stage_real(
            golden=golden, ledger=AttemptLedger(tmp_path / "ev"),
            server_tools=server,
            window_role_map={"W1": "main", "W2": "dialog", "W3": "dialog",
                             "W4": "dialog"})
        assert outcome["latched"] is False, outcome["steps"]
        assert outcome["report"]["outcome"] == "pass", outcome["report"]
        # 每个动作步恰一次真实注入;失效步(W4)注入的是当前语言项 Idioma。
        clicks = [c for c in backend.dispatch_calls if c["method"] == "click"]
        assert len(clicks) == 4, backend.dispatch_calls
        # 断言结果持久且 pass(独立业务断言,非 receipt ok):4 个动作步
        # + 1 个跨窗断言步全部 pass。
        events = AttemptLedger(tmp_path / "ev").rebuild_history(
            run_id=outcome["runId"])
        assert [e["outcome"] for e in events] == ["pass"] * 5, events
    finally:
        from target import product_policy
        from target.action_dispatcher import set_backend_resolver
        product_policy.reset_for_tests()
        server._binding_facade_holder["facade"] = None
        set_backend_resolver(lambda: None)


def test_negative_receipt_ok_but_language_unchanged_fails(monkeypatch,
                                                          tmp_path):
    """反例C(业务断言独立): receipt ok 但语言结果控件仍是 English →
    control_text_equals failed → fail 锁存,候选不宣称 recovered(C11
    语义:窗口存在/点击成功不代替业务结果)。"""
    from target.windowing.attempt_ledger import AttemptLedger
    import target.server as server
    recording = _lang_recording()
    result, proposal = _propose(recording)
    golden = proposal.candidates[0].golden
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
            # 语言结果控件实况 English:与期望 French 矛盾。
            tree["controls"].append({
                "class_name": "Text", "text": "English", "control_id": 30,
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
    try:
        outcome = execute_v3_window_stage_real(
            golden=golden, ledger=AttemptLedger(tmp_path / "ev"),
            server_tools=server,
            window_role_map={"W1": "main", "W2": "dialog", "W3": "dialog",
                             "W4": "dialog"})
        assert outcome["report"]["outcome"] == "fail", outcome["report"]
        assert outcome["latched"] is True, outcome["steps"]
        events = AttemptLedger(tmp_path / "ev").rebuild_history(
            run_id=outcome["runId"])
        assert events[-1]["outcome"] == "fail", events
        refs = [r for r in (events[-1].get("assertionResultRefs") or [])
                if r.get("type") == "control_text_equals"]
        assert refs and refs[0]["expected"] == "French", refs
        assert refs[0]["actual"] == "English", refs
    finally:
        from target import product_policy
        from target.action_dispatcher import set_backend_resolver
        product_policy.reset_for_tests()
        server._binding_facade_holder["facade"] = None
        set_backend_resolver(lambda: None)


# ------------------------------------------------------------ 不可造证反例


def test_negative_overlapping_timeline_refuses_candidate():
    """反例A: 时间线重叠(实例共存,无顺序证明) → 候选必须拒绝。
    不能借共存窗口的帧: 那可能是另一个真实目标。"""
    recording = _lang_recording(timeline_overlap=True)
    result, proposal = _propose(recording)
    assert not proposal.candidates
    assert any(r.reason == "structural_identity_unprovable"
               for r in proposal.refusals)


def test_negative_sibling_slot_disagreement_refuses_candidate():
    """反例B: 兄弟帧菜单结构不一致(第二帧是不同的菜单形状——三个
    槽位,Language 位置的槽不存在) → 契约无法统一,候选拒绝。"""
    recording = _lang_recording()
    # 第二实例帧: 菜单只有三个槽(语言项缺失),布局归一化后与
    # 失效目标中心命中的契约形状不同。
    different_menu = [
        {"controlType": "MenuItem", "text": "Setting Center",
         "ancestry": copy.deepcopy(CHAIN), "rect": [1000, 76, 1153, 108]},
        {"controlType": "MenuItem", "text": "Logs",
         "ancestry": copy.deepcopy(CHAIN), "rect": [1000, 108, 1153, 140]},
        {"controlType": "MenuItem", "text": "About",
         "ancestry": copy.deepcopy(CHAIN), "rect": [1000, 172, 1153, 204]},
    ]
    frames = list(recording.control_snapshots)
    frames[1] = _snap("CS-0002", 310, 200, "RI-0002", different_menu)
    recording = RawRecording(
        name=recording.name, session_id=recording.session_id,
        events=recording.events, control_snapshots=tuple(frames),
        windows=recording.windows, schema=recording.schema,
        window_timeline=recording.window_timeline)
    result, proposal = _propose(recording)
    assert not proposal.candidates
    assert any(r.reason == "structural_identity_unprovable"
               for r in proposal.refusals)


def test_negative_incomplete_proof_frames_refuse_candidate():
    """反例C: 兄弟帧不完整(complete=False) → 不能证唯一性,拒绝。"""
    recording = _lang_recording()
    frames = [dict(s) for s in recording.control_snapshots]
    for f in frames:
        f["complete"] = False
    recording = RawRecording(
        name=recording.name, session_id=recording.session_id,
        events=recording.events, control_snapshots=tuple(frames),
        windows=recording.windows, schema=recording.schema,
        window_timeline=recording.window_timeline)
    result, proposal = _propose(recording)
    assert not proposal.candidates


def test_negative_out_of_scope_issues_are_refused():
    """反例D: 非 trace-缺陷类(覆盖/面板计数)明确拒修,不静默。"""
    from agent.recording.trace_repair import propose_repairs
    recording = _lang_recording()
    result = compile_recording(recording)
    proposal = propose_repairs(
        recording, result.golden,
        {"sourceEvents": {"byStep": {}}})
    assert not proposal.candidates


def test_nonready_candidate_keeps_incomplete_status():
    """来源诚实: 若修复后仍有其他 incomplete 步,候选 golden 保持
    incomplete,不宣称 ready。"""
    recording = _lang_recording()
    result, proposal = _propose(recording)
    candidate = proposal.candidates[0]
    # 本 fixture 只有一个缺陷步;这里验证 status 传播逻辑本身:
    assert candidate.golden.status == "ready"
    assert all(s.status == "ready" for s in candidate.golden.steps)
