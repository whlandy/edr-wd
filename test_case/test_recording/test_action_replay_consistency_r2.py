"""batch-2 剩余动作 录制→编译→回放 一致性（纯新增，不改既有业务/测试文件）。

独立审计增量（2026-10-28 定时轮，接续 ``test_action_replay_consistency.py``）：
- ``gui.type_text``（text_commit）：既有覆盖只测控件定位/物化/派发单侧，缺"事件→编译→回放"
  一体回归。
- ``pointer.scroll`` / ``pointer.drag``：编译需显式结果 verifier 才 `ready`，缺"无 verifier
  必须诚实 incomplete"的回归 pin 住"诚实 ready/incomplete 结论"。
- ``window_transition``：绑定到其前一动作为 ``window_open`` verifier，缺该契约的回归。

安全行为不变：强身份控件语义唯一才回放；缺证据/缺 verifier/保护文本一律诚实拒绝，
不猜测、不静默用录制坐标。本文件只做代码级（offline）一致性回归。
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
_SCOPE = CaptureScope("t", "windows_pywinauto", "EDRClient.exe", "^logo1$")


def _snapshot(controls):
    return {"snapshotId": "S1", "capturedAtMs": 0, "reason": "probe",
            "durationMs": 0,
            "window": {"title": _WINDOW_TITLE, "processName": "EDRClient.exe"},
            "contentHash": "h", "stateHash": "h", "controlCount": len(controls),
            "complete": True, "controls": list(controls)}


def _compile(*events, controls=()):
    recording = RawRecording(
        name="t", session_id="s1", events=events,
        control_snapshots=(_snapshot(list(controls)),),
    )
    result = compile_recording(recording)
    return recording, result


def _golden(*steps):
    return GoldenTrace(
        name="batch2-r2", status="ready",
        source_recording={"schema": "edr.desktop-recording/v1", "sha256": "s"},
        catalog={"version": "1.0.0", "digest": "sha256:catalog"},
        environment={"backend": "windows_pywinauto", "target": "win-dev"},
        steps=tuple(steps),
    )


class _Observations:
    """回放观测提供者：控件 + 窗口列表，供 locator 与 window_open 求值。"""

    def __init__(self, controls, windows=None):
        self.controls = controls
        self.windows = windows or [{
            "process_name": "EDRClient.exe", "title": _WINDOW_TITLE,
        }]
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
            "windows": list(self.windows),
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


def _runtime(controls, windows=None):
    observations = _Observations(controls, windows)
    calls = []

    def dispatch(**kwargs):
        calls.append(kwargs)
        return ActionReceipt.from_ok(
            action_id=kwargs["action_id"], action_code=kwargs["action_code"],
            request_id=kwargs["request_id"], result={"ok": True},
        )

    executor = AtomicExecutor(dispatch=dispatch, observation_provider=observations)
    return ReplayRuntime(executor, "1.0.0", "sha256:catalog"), observations, calls


# --------------------------------------------------------------------------- #
# gui.type_text（text_commit）：录制→编译→回放 全链路
# --------------------------------------------------------------------------- #
_EDIT_AUTOMATION_ID = "EdrMainWindow.searchInput"


def _edit_target(text="malware.exe", protected=False):
    return ObservedTarget(
        "OBS-1", "T0001", "sha256:x", control_type="Edit",
        automation_id=_EDIT_AUTOMATION_ID, text="", ancestry=(),
        protected=protected,
    )


def _edit_control(text="malware.exe"):
    return {
        "automation_id": _EDIT_AUTOMATION_ID, "control_type": "Edit",
        "text": text, "rect": [500, 100, 700, 136],
    }


def _text_event(value="malware.exe", protected=False):
    return RawCaptureEvent(
        sequence=1, wall_time="2026-10-28T00:00:00Z", monotonic_ms=1,
        type="text_commit", scope=_SCOPE,
        input={"value": value}, observed_target=_edit_target(protected=protected),
    )


def test_text_commit_compiles_type_text_ready_argument():
    """text_commit（非保护 Edit + automationId）→ 编译出 gui.type_text 且带 text 参数，
    ready、不歧义、不拒绝。"""
    event = _text_event("malware.exe")
    selector = synthesize_selector(event)
    assert selector is not None
    assert selector.control["automationId"] == _EDIT_AUTOMATION_ID

    _, result = _compile(event, controls=[_edit_control()])
    step = result.case.steps[0]
    assert step.action_id == "gui.type_text"
    assert step.args == {"text": "malware.exe"}
    assert step.status == "ready", step.issues


def test_protected_text_plaintext_rejected_at_data_boundary():
    """数据边界纪律：text_commit 把明文放进 input，而目标被标记为保护
    （protected=True 或未知 None）→ 模型直接拒绝（recording_secret_plaintext），
    明文永远进不了编译/回放路径。"""
    from target.recording.models import RecordingModelError
    for protected in (True, None):
        with pytest.raises(RecordingModelError) as exc:
            _text_event("s3cr3t!", protected=protected)
        assert exc.value.code == "recording_secret_plaintext"


def test_unknown_sensitivity_without_plaintext_is_honest_incomplete():
    """目标敏感性未知（protected=None）且 input 未携带明文 → 编译为
    recording_secret_review_required 的 incomplete：不强填明文、不虚报 ready。"""
    # observed_target.protected=None 且 input 无 value 明文
    event = RawCaptureEvent(
        sequence=1, wall_time="2026-10-28T00:00:00Z", monotonic_ms=1,
        type="text_commit", scope=_SCOPE, input={},
        observed_target=_edit_target(protected=None),
    )
    _, result = _compile(event, controls=[_edit_control()])
    step = result.case.steps[0]
    assert step.status == "incomplete", step.issues
    assert "recording_secret_review_required" in step.issues
    assert "text" not in step.args


def test_type_text_replays_with_recorded_value_and_fresh_identity():
    """编译后的 gui.type_text 回放：派发携带录制的 text + 重解析的控件身份，
    不含录制坐标（纯语义，非像素兜底）。"""
    event = _text_event("malware.exe")
    _, result = _compile(event, controls=[_edit_control()])
    step = result.case.steps[0]

    runtime, _, calls = _runtime([{
        "process_name": "EDRClient.exe", "window_title": _WINDOW_TITLE,
        "automation_id": _EDIT_AUTOMATION_ID, "control_type": "Edit",
        "text": "", "rect": [500, 660, 700, 696],   # 布局下移，靠语义非坐标
    }])
    outcome = replay_golden_trace(runtime, _golden(step))

    assert outcome.case_result.outcome is CaseOutcome.PASSED
    assert len(calls) == 1
    assert calls[0]["action_id"] == "gui.type_text"
    assert calls[0]["args"]["text"] == "malware.exe"
    target_ref = calls[0]["target_ref"]
    assert target_ref is not None
    assert target_ref["snapshot_id"].startswith("OBS-")


def test_type_text_missing_target_blocks_without_dispatch():
    """type_text 目标在回放观测缺失 → 安全拒绝、不派发（不猜测坐标）。"""
    event = _text_event("malware.exe")
    _, result = _compile(event, controls=[_edit_control()])
    step = result.case.steps[0]

    runtime, _, calls = _runtime([])   # 观测无匹配控件
    outcome = replay_golden_trace(runtime, _golden(step))

    assert calls == []
    assert outcome.task_success is False


# --------------------------------------------------------------------------- #
# pointer.scroll / pointer.drag：无结果 verifier 必须诚实 incomplete
# --------------------------------------------------------------------------- #
_SCROLL_TABLE = "EdrMainWindow.scanTable"


def _scroll_target():
    return ObservedTarget(
        "OBS-1", "T0001", "sha256:x", control_type="DataItem", text="Details",
        ancestry=({"controlType": "Table", "automationId": _SCROLL_TABLE},),
    )


def _scroll_event(delta=-120):
    return RawCaptureEvent(
        sequence=1, wall_time="2026-10-28T00:00:00Z", monotonic_ms=1,
        type="scroll_commit", scope=_SCOPE,
        input={"delta": delta, "screenPoint": [500, 300]},
        observed_target=_scroll_target(),
    )


def test_scroll_without_result_verifier_is_honest_incomplete():
    """scroll_commit 无绑定结果 verifier → 编译为 incomplete
    （compile_scroll_verifier_required），绝不虚报 ready。"""
    event = _scroll_event(-120)
    selector = synthesize_selector(event)
    # 滚动落在可滚动 Table 容器上 → 稳定语义容器，非歧义拒绝
    assert selector is not None
    assert selector.control["automationId"] == _SCROLL_TABLE

    _, result = _compile(event, controls=[])
    step = result.case.steps[0]
    assert step.action_id == "pointer.scroll"
    assert step.args == {"clicks": -1}   # delta=-120 -> -1 notch
    assert step.status == "incomplete", step.issues
    assert "compile_scroll_verifier_required" in step.issues


def test_drag_without_result_verifier_is_honest_incomplete():
    """drag_commit 无绑定结果 verifier → 编译为 incomplete
    （compile_drag_verifier_required），grep 坐标只作证据、不作可回放参数。"""
    event = RawCaptureEvent(
        sequence=1, wall_time="2026-10-28T00:00:00Z", monotonic_ms=1,
        type="drag_commit", scope=_SCOPE,
        input={
            "screenPoint": [400, 150],   # 起点须在目标 rect 内，否则锚点不可推断
            "endPoint": [600, 300],
            "endTarget": {"automationId": "TargetPane", "controlType": "Pane",
                          "rect": [500, 200, 900, 600]},
        },
        observed_target=ObservedTarget(
            "OBS-1", "T0001", "sha256:x", control_type="ListItem",
            automation_id="SourceItem", text="", rect=[100, 100, 700, 200],
            ancestry=(),
        ),
    )
    selector = synthesize_selector(event)
    assert selector is not None

    _, result = _compile(event, controls=[])
    step = result.case.steps[0]
    assert step.action_id == "pointer.drag"
    # 录制坐标不进入可回放 args（只可能保留 duration）
    assert "screenPoint" not in step.args and "endPoint" not in step.args
    assert step.status == "incomplete", step.issues
    assert "compile_drag_verifier_required" in step.issues


# --------------------------------------------------------------------------- #
# window_transition：绑定为前一动作的 window_open verifier，并在回放求值
# --------------------------------------------------------------------------- #
_TRUST_BUTTON = "EdrMainWindow.trustButton"
_DETAILS_TITLE = "Malware Details"


def _click_trust_event(causal_id):
    return RawCaptureEvent(
        sequence=1, wall_time="2026-10-28T00:00:00Z", monotonic_ms=1,
        type="pointer_click", scope=_SCOPE, causal_id=causal_id,
        input={"button": "left"}, observed_target=ObservedTarget(
            "OBS-1", "T0001", "sha256:x", control_type="Button",
            automation_id=_TRUST_BUTTON, text="Trust", ancestry=(),
        ),
    )


def _transition_event(causal_id, title=_DETAILS_TITLE, kind="opened"):
    return RawCaptureEvent(
        sequence=2, wall_time="2026-10-28T00:00:01Z", monotonic_ms=2,
        type="window_transition", scope=_SCOPE, causal_id=causal_id,
        input={"kind": kind, "title": title,
               "processName": "EDRClient.exe"},
    )


def _trust_control():
    return {
        "automation_id": _TRUST_BUTTON, "control_type": "Button",
        "text": "Trust", "rect": [500, 200, 560, 236],
    }


def test_bound_window_transition_becomes_open_verifier_on_action():
    """与 click 同 causal 的 window_transition(opened) → click 步骤绑定
    window_open verifier（窗开断言由前一动做证明，而非独立空动作）。"""
    causal = "c-77"
    _, result = _compile(
        _click_trust_event(causal),
        _transition_event(causal),
        controls=[_trust_control()],
    )
    # 只有 1 个步骤：transition 绑定进 click，不产生独立 step
    assert len(result.case.steps) == 1
    step = result.case.steps[0]
    assert step.action_id == "gui.click"
    assert step.status == "ready", step.issues
    window_verifiers = [v for v in step.verifiers if v.get("type") == "window_open"]
    assert window_verifiers, step.verifiers
    assert window_verifiers[0]["expected"] == {
        "exists": True, "processName": "EDRClient.exe", "title": _DETAILS_TITLE,
    }


def test_bound_window_transition_open_replays_as_pass_when_window_present():
    """绑定 window_open verifier 的 click 步骤经回放：观测含目标窗口 →
    求值通过（PASSED），且 click 被派发。"""
    causal = "c-77"
    _, result = _compile(
        _click_trust_event(causal),
        _transition_event(causal),
        controls=[_trust_control()],
    )
    step = result.case.steps[0]

    runtime, _, calls = _runtime(
        controls=[{
            "process_name": "EDRClient.exe", "window_title": _WINDOW_TITLE,
            "automation_id": _TRUST_BUTTON, "control_type": "Button",
            "text": "Trust", "rect": [500, 200, 560, 236],
        }],
        windows=[
            {"process_name": "EDRClient.exe", "title": _WINDOW_TITLE},
            {"process_name": "EDRClient.exe", "title": _DETAILS_TITLE},
        ],
    )
    outcome = replay_golden_trace(runtime, _golden(step))

    assert outcome.case_result.outcome is CaseOutcome.PASSED
    assert outcome.evaluation.assertion_pass_rate == 1.0
    assert len(calls) == 1

def test_bound_window_transition_open_replay_fails_when_window_absent():
    """回放观测缺少录制时绑定断言的目标窗口 → window_open 求值诚实失败、
    不假装通过（不静默忽略窗口断言）。"""
    causal = "c-77"
    _, result = _compile(
        _click_trust_event(causal),
        _transition_event(causal),
        controls=[_trust_control()],
    )
    step = result.case.steps[0]

    # 回放观测只有主窗口，没有 "Malware Details"
    runtime, _, calls = _runtime(
        controls=[{
            "process_name": "EDRClient.exe", "window_title": _WINDOW_TITLE,
            "automation_id": _TRUST_BUTTON, "control_type": "Button",
            "text": "Trust", "rect": [500, 200, 560, 236],
        }],
        windows=[{"process_name": "EDRClient.exe", "title": _WINDOW_TITLE}],
    )
    outcome = replay_golden_trace(runtime, _golden(step))

    assert outcome.task_success is False
