"""C09 成对探针:布局 drag 分型(WIP `_is_layout_drag` 实测核查)。

账本 C09:已识别 caption、同一绑定下,标题栏移动→打开语言菜单;对照
数据拖到标题栏。分类 WIP 待核查:当前 `_is_layout_drag` 只看 endTarget
的 automationId/controlType 特征(compiler.py)。

设计11 §7 的分型依据是"起点确属该窗口 caption/标题栏"+ 实例/owner 一致
+ 前后 bounds,而 raw drag_commit 事件只有 screenPoint(起点坐标)与
endTarget(终点身份),没有起点控件身份(startTarget)。§7 明确:
"从数据对象拖到标题栏"不是移动窗口。

本探针固定三类真实行为:
A. endTarget=titleWidget、observed_target=窗口标题控件 → 豁免(应过);
B. endTarget=数据组件 → 仍强制 verifier(应过);
C. observed_target=数据对象(ListItem)、endTarget=titleWidget
   ——"从数据对象拖到标题栏"与真移动窗口在当前事件结构下不可区分。
   只固定行为,由规格判断该行为是否为缺陷(不调整实现,留给报告)。
"""
from __future__ import annotations

import pytest

from target.recording.models import (CaptureScope, ObservedTarget,
                                     RawCaptureEvent, RawRecording)
from agent.recording.compiler import compile_recording

pytestmark = pytest.mark.unit

_SCOPE = CaptureScope("target", "windows_pywinauto", "EDRClient.exe", "^logo1$")


def _snapshot(controls):
    return {"snapshotId": "S1", "capturedAtMs": 0, "reason": "probe",
            "durationMs": 0,
            "window": {"title": "logo1", "processName": "EDRClient.exe"},
            "contentHash": "h", "stateHash": "h", "controlCount": len(controls),
            "complete": True, "controls": list(controls)}


def _drag_event(*, observed, end_target):
    return RawCaptureEvent(
        sequence=1, wall_time="2026-11-06T00:00:00Z", monotonic_ms=1,
        type="drag_commit", scope=_SCOPE,
        input={
            "screenPoint": [400, 150],
            "endPoint": [600, 300],
            "endTarget": end_target,
        },
        observed_target=observed,
    )


_TITLE_OBS = ObservedTarget(
    "OBS-1", "T0001", "sha256:x", control_type="TitleBar",
    automation_id="EdrMainWindow.titleWidget", text="EDRClient",
    rect=[100, 100, 700, 200], ancestry=(),
)

_DATA_OBS = ObservedTarget(
    "OBS-1", "T0001", "sha256:x", control_type="ListItem",
    automation_id="SourceItem", text="",
    rect=[100, 100, 700, 200], ancestry=(),
)

_TITLE_END = {"automationId": "EdrMainWindow.titleWidget.title",
              "controlType": "Text", "rect": [100, 100, 700, 130]}
_DATA_END = {"automationId": "TargetPane", "controlType": "Pane",
             "rect": [500, 200, 900, 600]}


def _compile(event):
    recording = RawRecording(
        name="t", session_id="s1", events=[event],
        control_snapshots=(_snapshot([]),),
    )
    return compile_recording(recording)


def _first_step(result):
    test_cases = getattr(result, "test_cases", None) or getattr(
        result, "recorded", None) or []
    if isinstance(result, tuple):
        _, result = result
    return result


def test_c09_a_titlebar_drag_layout_exemption():
    """正例:终点 titleWidget(移动窗口),不强制业务数据断言。"""
    result = _compile(_drag_event(observed=_TITLE_OBS, end_target=_TITLE_END))
    text = repr(result)
    assert "compile_drag_verifier_required" not in text, (
        f"标题栏拖拽应豁免业务 verifier,实际 {text[:400]}")


def test_c09_b_data_drag_keeps_verifier():
    """正例:终点数据组件,仍强制结果验证。"""
    result = _compile(_drag_event(observed=_DATA_OBS, end_target=_DATA_END))
    text = repr(result)
    assert "compile_drag_verifier_required" in text, (
        f"数据拖拽必须强制业务 verifier,实际 {text[:400]}")


def test_c09_c_data_start_titlebar_end_current_behavior():
    """数据对象起点 + 标题栏终点(§7 误分类面,已修)。

    设计 §7:"从数据对象拖到标题栏"不是移动窗口。本探针原固定旧行为
    (仅凭 endTarget 豁免)并注明"若实现后续按 §7 增加起点证据,本测试
    应随分型更新"。起点证据已在 raw 事件内(observed_target,press 时刻
    hit-test),_is_layout_drag 现要求双端标题栏印证、缺起点证据
    fail-closed;本用例随分型更新为断言强制 verifier(详细正负例见
    test_c09_start_evidence_probe.py)。"""
    result = _compile(_drag_event(observed=_DATA_OBS, end_target=_TITLE_END))
    text = repr(result)
    assert "compile_drag_verifier_required" in text, (
        f"数据起点+标题栏终点须强制 verifier(§7 起点印证),实际 {text[:400]}")
