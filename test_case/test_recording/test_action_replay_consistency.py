"""batch-2 动作 录制→编译→回放 全链路一致性（P0 缺口补齐，纯新增）。

独立审计结论（2026-10-28 22:00 定时轮）：
- ``gui.select``（selection_change）在整套测试里**零覆盖**——既无编译也无回放。
- ``toggle_change`` 的编译 ``checked`` verifier 只被直接求值测试覆盖，
  从未走 录制→编译→回放 全链路。

本文件补这两条 P0 的**端到端回归**：每个用例都从 ``RawCaptureEvent`` 录制事件出发，
走生产入口 ``synthesize_selector`` → ``compile_recording`` → ``replay_golden_trace``，
断言编译产物（args / verifier）与回放派发、求值一致。纯新增文件，不改任何既有
业务/测试文件。

安全行为不变：这些强身份控件本身语义唯一，回放唯一解析；歧义/缺证据仍拒绝。
"""

from __future__ import annotations

import pytest

from action_dispatcher import ActionReceipt
from agent.execution import AtomicExecutor, CaseOutcome
from agent.recording.compiler import compile_recording
from agent.recording.models import GoldenTrace
from agent.recording.replay import ReplayRuntime, replay_golden_trace
from agent.recording.selectors import synthesize_selector
from target.recording.models import (
    CaptureScope,
    ObservedTarget,
    RawCaptureEvent,
    RawRecording,
)

pytestmark = pytest.mark.unit

_WINDOW_TITLE = "logo1"


def _snapshot(controls):
    return {"snapshotId": "S1", "capturedAtMs": 0, "reason": "probe",
            "durationMs": 0,
            "window": {"title": _WINDOW_TITLE, "processName": "EDRClient.exe"},
            "contentHash": "h", "stateHash": "h", "controlCount": len(controls),
            "complete": True, "controls": list(controls)}


def _scope():
    return CaptureScope("t", "windows_pywinauto", "EDRClient.exe", "^logo1$")


def _compile(event, controls):
    recording = RawRecording(
        name="t", session_id="s1", events=(event,),
        control_snapshots=(_snapshot(controls),),
    )
    result = compile_recording(recording)
    return recording, result


class _Observations:
    def __init__(self, controls):
        self.controls = controls
        self.counter = 0
        self.snapshots = {}

    def refresh(self):
        self.counter += 1
        snapshot_id = f"OBS-{self.counter}"
        items = []
        for i, source in enumerate(self.controls, start=1):
            item = dict(source)
            item["target_id"] = f"T{self.counter:02d}{i:02d}"
            item["process_name"] = source.get("process_name") or "EDRClient.exe"
            item["window_title"] = _WINDOW_TITLE
            items.append(item)
        self.snapshots[snapshot_id] = {
            "snapshot_id": snapshot_id, "controls": items,
            "windows": [{"process_name": "EDRClient.exe", "title": _WINDOW_TITLE}],
            "active_window": {
                "process_name": "EDRClient.exe", "title": _WINDOW_TITLE,
                "rect": [0, 0, 1280, 720],
            },
            "screenshot_scope": "window", "screenshot_redacted": True,
        }
        return snapshot_id

    def latest_snapshot_id(self):
        return f"OBS-{self.counter}" if self.counter else None

    def get_snapshot(self, snapshot_id):
        return self.snapshots[snapshot_id]


def _runtime(controls):
    observations = _Observations(controls)
    calls = []

    def dispatch(**kwargs):
        calls.append(kwargs)
        return ActionReceipt.from_ok(
            action_id=kwargs["action_id"], action_code=kwargs["action_code"],
            request_id=kwargs["request_id"], result={"ok": True},
        )

    executor = AtomicExecutor(dispatch=dispatch, observation_provider=observations)
    return ReplayRuntime(executor, "1.0.0", "sha256:catalog"), observations, calls


def _golden(*steps):
    return GoldenTrace(
        name="batch2", status="ready",
        source_recording={"schema": "edr.desktop-recording/v1", "sha256": "s"},
        catalog={"version": "1.0.0", "digest": "sha256:catalog"},
        environment={"backend": "windows_pywinauto", "target": "win-dev"},
        steps=tuple(steps),
    )


# --------------------------------------------------------------------------- #
# gui.select (selection_change)：录制→编译→回放 全链路
# --------------------------------------------------------------------------- #
_COMBOBOX_AUTOMATION_ID = "EdrMainWindow.operationTypeCombo"


def _select_event(item="全部"):
    target = ObservedTarget(
        "OBS-1", "T0001", "sha256:x", control_type="ComboBox",
        automation_id=_COMBOBOX_AUTOMATION_ID, text="", ancestry=(),
    )
    return RawCaptureEvent(
        sequence=1, wall_time="2026-10-28T00:00:00Z", monotonic_ms=1,
        type="selection_change", scope=_scope(),
        input={"value": item}, observed_target=target,
    )


def _combo_control():
    return {
        "automation_id": _COMBOBOX_AUTOMATION_ID, "control_type": "ComboBox",
        "text": "", "rect": [500, 300, 660, 336],
    }


def test_selection_change_compiles_item_argument_and_is_ready():
    """selection_change 录制 → 编译出带 ``item`` 参数的 ready 步骤，不歧义不拒绝。"""
    event = _select_event("威胁防护")
    selector = synthesize_selector(event)
    assert selector is not None
    # 强身份（automationId）→ selector 带 automationId，不依赖 ancestry
    assert selector.control["automationId"] == _COMBOBOX_AUTOMATION_ID

    _, result = _compile(event, [_combo_control()])
    step = result.case.steps[0]
    assert step.action_id == "gui.select"
    assert step.args == {"item": "威胁防护"}
    assert step.status == "ready", step.issues


def test_selection_change_replays_with_recorded_item_and_identity():
    """编译后的 gui.select 步骤经回放派发：携带录制的 item + 重解析的控件身份，
    派发成功（不静默，不歧义）。"""
    event = _select_event("威胁防护")
    _, result = _compile(event, [_combo_control()])
    step = result.case.steps[0]

    runtime, _, calls = _runtime([{
        "process_name": "EDRClient.exe", "window_title": _WINDOW_TITLE,
        "automation_id": _COMBOBOX_AUTOMATION_ID, "control_type": "ComboBox",
        "text": "", "rect": [500, 650, 660, 686],   # 布局已下移，靠语义非坐标
    }])
    outcome = replay_golden_trace(runtime, _golden(step))

    assert outcome.case_result.outcome is CaseOutcome.PASSED
    assert len(calls) == 1
    assert calls[0]["action_id"] == "gui.select"
    assert calls[0]["args"]["item"] == "威胁防护"
    # 派发指向重解析的当前控件（新鲜观测里的 target_ref），绝不用录制坐标。
    target_ref = calls[0]["target_ref"]
    assert target_ref is not None
    assert target_ref["snapshot_id"].startswith("OBS-")
    assert "x" not in calls[0]["args"] and "y" not in calls[0]["args"]


def test_selection_change_missing_target_blocks_without_dispatch():
    """选择动作目标在回放观测缺失 → 安全拒绝、不派发（batch-2 一致性 + 安全）。"""
    event = _select_event("威胁防护")
    _, result = _compile(event, [_combo_control()])
    step = result.case.steps[0]

    runtime, _, calls = _runtime([])   # 观测无任何匹配控件
    outcome = replay_golden_trace(runtime, _golden(step))

    assert calls == []
    assert outcome.task_success is False


# --------------------------------------------------------------------------- #
# toggle_change（check）：录制→编译→回放→断言 全链路
# --------------------------------------------------------------------------- #
_TOGGLE_AUTOMATION_ID = "EdrMainWindow.realTimeProtectSwitch"


def _toggle_event(checked=True):
    target = ObservedTarget(
        "OBS-1", "T0001", "sha256:x", control_type="CheckBox",
        automation_id=_TOGGLE_AUTOMATION_ID, text="实时防护",
        ancestry=(),
    )
    return RawCaptureEvent(
        sequence=1, wall_time="2026-10-28T00:00:00Z", monotonic_ms=1,
        type="toggle_change", scope=_scope(),
        input={"value": checked}, observed_target=target,
    )


def _toggle_control(checked=True):
    return {
        "automation_id": _TOGGLE_AUTOMATION_ID, "control_type": "CheckBox",
        "text": "实时防护", "checked": checked, "rect": [500, 100, 560, 136],
    }


def test_toggle_change_compiles_checked_verifier_into_ready_step():
    """toggle_change 录制 → 编译步骤带 ``checked`` verifier，强身份 ready。"""
    event = _toggle_event(checked=True)
    selector = synthesize_selector(event)
    assert selector is not None
    assert selector.control["automationId"] == _TOGGLE_AUTOMATION_ID

    _, result = _compile(event, [_toggle_control(True)])
    step = result.case.steps[0]
    assert step.action_id == "gui.click"
    assert {"type": "checked", "expected": True} in step.verifiers
    assert step.status == "ready", step.issues


def test_toggle_change_replay_asserts_checked_state():
    """编译后的 toggle 步骤经回放：解析目标 + 求值 checked verifier 通过
    （录制勾选 → 回放观测同样勾选 → control_checked_equals 通过）。"""
    event = _toggle_event(checked=True)
    _, result = _compile(event, [_toggle_control(True)])
    step = result.case.steps[0]

    runtime, _, calls = _runtime([_toggle_control(True)])
    outcome = replay_golden_trace(runtime, _golden(step))

    assert outcome.case_result.outcome is CaseOutcome.PASSED
    assert outcome.evaluation.assertion_pass_rate == 1.0
    # toggle 的语义是"勾选动作 + 状态断言"：断言通过即可（派发与否由实现决定）
    assert any(sr.status.name == "PASSED"
               for sr in outcome.case_result.step_results)


def test_toggle_change_replay_fails_when_observed_state_contradicts():
    """录制勾选 → 回放观测未勾选 → checked verifier 求值失败（诚实失败，不假装通过）。"""
    event = _toggle_event(checked=True)
    _, result = _compile(event, [_toggle_control(True)])
    step = result.case.steps[0]

    # 回放观测里开关是未勾选，与录制的 expected=True 冲突
    runtime, _, calls = _runtime([_toggle_control(False)])
    outcome = replay_golden_trace(runtime, _golden(step))

    assert outcome.task_success is False
    assert outcome.evaluation.assertion_pass_rate == 0.0
