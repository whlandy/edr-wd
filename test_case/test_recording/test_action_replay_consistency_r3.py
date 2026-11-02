"""batch-2 scroll/drag 带结果 verifier 的 ready→replay 一体回归（纯新增）。

接续 ``test_action_replay_consistency_r2.py`` 的下一个增量（其"剩余风险/下一批次"：

    batch-2 中 scroll/drag 带 verifier 的 ready→replay 一体回归
    （r2 只 pin 了无 verifier 的诚实 incomplete；verifier 绑定路径单侧已有，
    可再接"绑定→ready→回放→求值"合一闭环）。

r2 已经覆盖：scroll/drag **无结果 verifier** 必须诚实 incomplete
（``compile_scroll_verifier_required`` / ``compile_drag_verifier_required``）。
本文件补它们的**反向**：一旦绑定结果 verifier（随后的因果断言），scroll/drag
步骤必须能编译为 **ready**，并能走完整 录制→编译→回放→求值 闭环——派发动作
**并且** 求值绑定的结果断言。

安全行为不变：
- verifier 绑定路径仍与 r2 一致（``assertion`` 事件与前一动作共享 causal_id）；
- 回放求值诚实：观测与录制绑定状态相符 → PASS；相悖 → 诚实 FAIL
  （``assertion_pass_rate=0``），不静默跳过、不假装通过；
- 强身份控件唯一解析，不依赖录制坐标。

纯新增文件，不改任何既有业务/测试文件。
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
        name="batch2-r3", status="ready",
        source_recording={"schema": "edr.desktop-recording/v1", "sha256": "s"},
        catalog={"version": "1.0.0", "digest": "sha256:catalog"},
        environment={"backend": "windows_pywinauto", "target": "win-dev"},
        steps=tuple(steps),
    )


# --------------------------------------------------------------------------- #
# Replay observation provider + runtime (mirrors r2/P0 scaffolding)
# --------------------------------------------------------------------------- #
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


# --------------------------------------------------------------------------- #
# pointer.scroll + 绑定 checked verifier → ready → replay → 求值
# --------------------------------------------------------------------------- #
_SCROLL_TABLE = "EdrMainWindow.scanTable"
_CHECKBOX = "EdrMainWindow.realTimeProtectSwitch"


def _scroll_and_assert_checked(checked=True):
    """scroll_commit（落在 scanTable 上） + 与之前一动作同 causal 的 checked 断言。

    断言与 scroll 共享 causal_id → 编译阶段折叠为 scroll 步骤的"结果 verifier"，
    使滚动有可证明的结果（滚动后该开关确实处于 expected 状态）。
    """
    causal = "c-scroll"
    scroll = RawCaptureEvent(
        sequence=1, wall_time="2026-10-28T00:00:00Z", monotonic_ms=1,
        type="scroll_commit", scope=_SCOPE, causal_id=causal,
        input={"delta": -120, "screenPoint": [500, 300]},
        observed_target=ObservedTarget(
            "OBS-1", "T0001", "sha256:x", control_type="DataItem", text="Details",
            ancestry=({"controlType": "Table", "automationId": _SCROLL_TABLE},),
        ),
    )
    assertion = RawCaptureEvent(
        sequence=2, wall_time="2026-10-28T00:00:01Z", monotonic_ms=2,
        type="assertion", scope=_SCOPE, causal_id=causal, input={},
        observed_target=ObservedTarget(
            "OBS-2", "T0002", "sha256:c", control_type="CheckBox",
            automation_id=_CHECKBOX, text="实时防护",
        ),
        assertion={"type": "checked", "expected": checked, "timeoutSeconds": 5.0},
    )
    return scroll, assertion


def _scroll_table_control():
    return {
        "automation_id": _SCROLL_TABLE, "control_type": "Table",
        "text": "", "rect": [300, 200, 1000, 600],
    }


def _checkbox_control(checked):
    return {
        "automation_id": _CHECKBOX, "control_type": "CheckBox",
        "text": "实时防护", "checked": checked, "rect": [500, 100, 560, 136],
    }


def test_scroll_with_bound_result_verifier_compiles_ready():
    """scroll + 绑定结果 verifier（随后的 checked 断言同 causal）→ 编译为 READY，
    且步骤持有该 verifier——不再因 'compile_scroll_verifier_required' 而 incomplete。"""
    scroll, assertion = _scroll_and_assert_checked(True)
    selector = synthesize_selector(scroll)
    assert selector is not None
    # 滚动落到可滚动 Table 容器，selector 以容器强 ID 为身份
    assert selector.control["automationId"] == _SCROLL_TABLE

    _, result = _compile(
        scroll, assertion,
        controls=[_scroll_table_control(), _checkbox_control(True)],
    )
    assert len(result.case.steps) == 1
    step = result.case.steps[0]
    assert step.action_id == "pointer.scroll"
    assert step.args == {"clicks": -1}
    # 绑定的 checked 结果断言成为该 scroll 步骤的 verifier，缺它滚动只是 incomplete
    bound = [v for v in step.verifiers if v.get("type") == "checked"]
    assert bound, step.verifiers
    assert bound[0]["expected"] is True
    # 强容器 + 绑定结果断言 → 诚实 ready
    assert step.status == "ready", step.issues


def test_scroll_with_bound_verifier_replays_and_passes_when_state_matches():
    """scroll(ready) 经回放：派发 pointer.scroll（新鲜观测的目标），并求值绑定的
    checked 结果断言通过（录制 expected=True = 回放观测 checked=True）。"""
    scroll, assertion = _scroll_and_assert_checked(True)
    _, result = _compile(
        scroll, assertion,
        controls=[_scroll_table_control(), _checkbox_control(True)],
    )
    step = result.case.steps[0]
    assert step.status == "ready", step.issues

    runtime, _, calls = _runtime([
        # 布局整体下移，滚动目标与开关都靠语义身份（非录制坐标）解析
        _scroll_table_control(),
        _checkbox_control(True),
    ])
    outcome = replay_golden_trace(runtime, _golden(step))

    assert outcome.case_result.outcome is CaseOutcome.PASSED
    assert outcome.evaluation.assertion_pass_rate == 1.0
    # scroll 被派发，且指向新鲜观测的 ScrollBar/容器目标
    scrolled = [c for c in calls if c["action_id"] == "pointer.scroll"]
    assert scrolled, calls
    assert scrolled[0]["args"]["clicks"] == -1
    assert scrolled[0]["target_ref"]["snapshot_id"].startswith("OBS-")


def test_scroll_with_bound_verifier_replay_fails_when_observed_state_contradicts():
    """回放观测中绑定的结果断言与录制相悖（录制 expected=True 但观测 checked=False）
    → 诚实失败，assertion_pass_rate=0，不假装通过。"""
    scroll, assertion = _scroll_and_assert_checked(True)
    _, result = _compile(
        scroll, assertion,
        controls=[_scroll_table_control(), _checkbox_control(True)],
    )
    step = result.case.steps[0]
    assert step.status == "ready", step.issues

    runtime, _, calls = _runtime([
        _scroll_table_control(),
        _checkbox_control(False),   # 回放观测与监听结果相悖
    ])
    outcome = replay_golden_trace(runtime, _golden(step))

    assert outcome.task_success is False
    assert outcome.evaluation.assertion_pass_rate == 0.0


# --------------------------------------------------------------------------- #
# pointer.drag + 绑定结果 verifier → ready → replay → 求值
# --------------------------------------------------------------------------- #
_DRAG_SOURCE = "EdrMainWindow.item1"
_DRAG_TARGET_PANE = "EdrMainWindow.targetPane"
_DRAG_CHECK = "EdrMainWindow.dragDoneFlag"


def _drag_and_assert_checked(checked=True):
    """drag_commit（源强 ID ListItem → 目标 Pane）+ 同 causal 的 checked 结果断言。

    拖动必须有"结果"才有资格 ready；绑定的 checked 断言证明拖动落地后该开关
    确实进入 expected 勾选态。用 ``checked``（而非 ``visible``）作结果断言，因为
    ``visible`` 求值为 ``control_exists``——语义是"控件存在"，不读取可见性状态，
    无法构造"状态相悖"反例；``checked`` 读取勾选状态，能诚实验证结果一致性。
    """
    causal = "c-drag"
    drag = RawCaptureEvent(
        sequence=1, wall_time="2026-10-28T00:00:00Z", monotonic_ms=1,
        type="drag_commit", scope=_SCOPE, causal_id=causal,
        input={
            "screenPoint": [130, 140],
            "endPoint": [550, 400],
            "endTarget": {"automationId": _DRAG_TARGET_PANE,
                          "controlType": "Pane", "rect": [500, 200, 900, 600]},
        },
        observed_target=ObservedTarget(
            "OBS-1", "T0001", "sha256:x", control_type="ListItem",
            automation_id=_DRAG_SOURCE, text="", rect=[100, 100, 700, 200],
            ancestry=(),
        ),
    )
    assertion = RawCaptureEvent(
        sequence=2, wall_time="2026-10-28T00:00:01Z", monotonic_ms=2,
        type="assertion", scope=_SCOPE, causal_id=causal, input={},
        observed_target=ObservedTarget(
            "OBS-2", "T0002", "sha256:v", control_type="CheckBox",
            automation_id=_DRAG_CHECK, text="拖动完成",
        ),
        assertion={"type": "checked", "expected": checked, "timeoutSeconds": 5.0},
    )
    return drag, assertion


def _drag_controls(checked=True):
    return [
        {"automation_id": _DRAG_SOURCE, "control_type": "ListItem",
         "text": "item-1", "rect": [100, 80, 700, 240]},
        {"automation_id": _DRAG_TARGET_PANE, "control_type": "Pane",
         "text": "", "rect": [500, 200, 900, 660]},
        {"automation_id": _DRAG_CHECK, "control_type": "CheckBox",
         "text": "拖动完成", "checked": checked, "rect": [520, 160, 580, 196]},
    ]


def test_drag_with_bound_result_verifier_compiles_ready():
    """drag + 绑定结果 verifier → 编译为 READY（不再
    'compile_drag_verifier_required'），且持有 checked 结果断言。"""
    drag, assertion = _drag_and_assert_checked(True)
    selector = synthesize_selector(drag)
    assert selector is not None

    _, result = _compile(drag, assertion, controls=_drag_controls(True))
    assert len(result.case.steps) == 1
    step = result.case.steps[0]
    assert step.action_id == "pointer.drag"
    # 录制坐标只作证据，不进可回放 args
    assert "screenPoint" not in step.args and "endPoint" not in step.args
    bound = [v for v in step.verifiers if v.get("type") == "checked"]
    assert bound, step.verifiers
    assert bound[0]["expected"] is True
    assert step.status == "ready", step.issues


def test_drag_with_bound_verifier_replays_when_state_matches():
    """drag(ready) 经回放：派发 pointer.drag（两侧锚点来自当前观测），并求值
    checked 结果断言通过（expected=True = 观测 checked=True）。"""
    drag, assertion = _drag_and_assert_checked(True)
    _, result = _compile(drag, assertion, controls=_drag_controls(True))
    step = result.case.steps[0]
    assert step.status == "ready", step.issues

    runtime, _, calls = _runtime(_drag_controls(True))
    outcome = replay_golden_trace(runtime, _golden(step))

    assert outcome.case_result.outcome is CaseOutcome.PASSED
    assert outcome.evaluation.assertion_pass_rate == 1.0
    dragged = [c for c in calls if c["action_id"] == "pointer.drag"]
    assert dragged, calls
    # 起点/终点都以相对锚点重算（当前观测 rect），绝不用录制屏幕点
    assert "x1" in dragged[0]["args"] and "x2" in dragged[0]["args"]


def test_drag_with_bound_verifier_replay_fails_when_observed_state_contradicts():
    """回放观测的 checked 结果断言与录制相悖（expected=True 但观测 checked=False）
    → 诚实失败（assertion_pass_rate=0），不静默跳过。"""
    drag, assertion = _drag_and_assert_checked(True)
    _, result = _compile(drag, assertion, controls=_drag_controls(True))
    step = result.case.steps[0]
    assert step.status == "ready", step.issues

    runtime, _, calls = _runtime(_drag_controls(checked=False))
    outcome = replay_golden_trace(runtime, _golden(step))

    assert outcome.task_success is False
    assert outcome.evaluation.assertion_pass_rate == 0.0
