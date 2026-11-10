"""C09 成对探针:drag 分型起点证据(设计 §7"起点确属标题栏")。

实测基线(0046 锁定原件 live-REC-ec395765861244bdb27a3d976aa6e136):
drag_commit 事件的起点控件身份在事件内已有 —— 采集侧 press 时刻对起点
坐标 hit-test 存 _pending_press["observed"],随事件携带为 observed_target
(windows.py _drag_input)。raw 事件有起点身份,缺口纯在分类侧:
_is_layout_drag 只读 endTarget,"数据对象拖到标题栏"被误豁免。
(0911 探针注释"事件结构无 startTarget"与实测不符,已由本探针更正。)

设计 §7:豁免须"起点、所属实例、几何相互印证",不只看终点字符串。
最小修复口径:豁免 = endTarget 命中 titleWidget 特征 **且** 起点身份
(observed_target)同样是标题栏特征;起点缺失/非标题栏 → fail-closed
保留强制 verifier(不因 endTarget 字符串豁免)。

四用例:
A. 双端标题栏(真移动窗口)→ 豁免保持;
B. 数据起点(ListItem)+数据终点 → 强制保持;
C. 数据起点 + 标题栏终点(§7 误分类面)→ 先红(旧=豁免),修后强制;
D. 标题栏起点 + 数据终点 → 数据操作,强制(起点豁免不反向豁免数据终点)。
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


def test_c09_d_titlebar_both_ends_layout_exemption():
    """A:双端标题栏(真移动窗口)→ 豁免保持。"""
    result = _compile(_drag_event(observed=_TITLE_OBS, end_target=_TITLE_END))
    text = repr(result)
    assert "compile_drag_verifier_required" not in text, (
        f"双端标题栏拖拽应豁免业务 verifier,实际 {text[:400]}")


def test_c09_e_data_start_data_end_keeps_verifier():
    """B:数据起点+数据终点 → 强制保持。"""
    result = _compile(_drag_event(observed=_DATA_OBS, end_target=_DATA_END))
    text = repr(result)
    assert "compile_drag_verifier_required" in text, (
        f"数据拖拽必须强制业务 verifier,实际 {text[:400]}")


def test_c09_f_data_start_titlebar_end_requires_verifier():
    """C(§7 误分类面,先红):数据对象拖到标题栏不是移动窗口。

    旧实现只看 endTarget → 误豁免。修复后:起点身份非标题栏 →
    fail-closed 保留强制 verifier。"""
    result = _compile(_drag_event(observed=_DATA_OBS, end_target=_TITLE_END))
    text = repr(result)
    assert "compile_drag_verifier_required" in text, (
        f"数据起点+标题栏终点不得按 endTarget 字符串豁免,实际 {text[:400]}")


def test_c09_g_titlebar_start_data_end_requires_verifier():
    """D:标题栏起点+数据终点 → 数据操作,强制。"""
    result = _compile(_drag_event(observed=_TITLE_OBS, end_target=_DATA_END))
    text = repr(result)
    assert "compile_drag_verifier_required" in text, (
        f"数据终点必须强制 verifier,起点豁免不得反向传导,实际 {text[:400]}")


def test_c09_h_missing_start_evidence_requires_verifier():
    """E(缺起点证据 fail-closed):无 observed_target 的 drag
    不能凭 endTarget 标题栏特征豁免 —— 旧 0046 前的事件结构或
    部分采集路径可能缺起点观测;缺证据 = 不可证明 = 不豁免。"""
    event = RawCaptureEvent(
        sequence=1, wall_time="2026-11-06T00:00:00Z", monotonic_ms=1,
        type="drag_commit", scope=_SCOPE,
        input={
            "screenPoint": [400, 150],
            "endPoint": [600, 300],
            "endTarget": _TITLE_END,
        },
        observed_target=None,
    )
    result = _compile(event)
    text = repr(result)
    assert "compile_drag_verifier_required" in text, (
        f"缺起点证据不得豁免(fail-closed),实际 {text[:400]}")
