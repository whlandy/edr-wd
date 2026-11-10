"""D2-0335 探针 B (design 11 §4.1/§9, live-REC-ccd6612): coverage 门语义。

实机原件实测：coverage.droppedEvents=32 全部来自 WinEvent hook 的
"无 SHOW 期 DESTROY"（桌面其他应用窗口销毁，0002 口径的环境噪音明细），
capture 队列零丢包（droppedPackets=0）。编译器 coverage 门只看
droppedEvents 总数即拒绝整份录制（recording_window_coverage_incomplete）——
0002 口径写入 unattributedDestroys 明细正是为了让消费者区分环境噪音与
目标窗口流失，门却从未读过它。

本探针锁定：
- 正例：droppedEvents>0 但 unattributedDestroys == droppedEvents
  （hook 侧无归属 DESTROY，无队列丢包）→ 时间线连续性仍可证，不因
  环境噪音拒绝。
- 负例 1：droppedEvents > unattributedDestroys（队列真丢包或 hook
  其他流失）→ 照旧拒绝。
- 负例 2：无 unattributedDestroys 明细的旧文档 droppedEvents>0 →
  照旧拒绝（不伪造"全部是噪音"）。
"""

from __future__ import annotations

import pytest

from agent.recording.compiler import compile_recording
from target.recording.models import RawRecording

pytestmark = pytest.mark.unit


def _recording(coverage):
    return RawRecording.from_dict({
        "schema": "edr.desktop-recording/v2", "name": "noise flow",
        "sessionId": "REC-D2B", "events": [_click(1)],
        "controlSnapshots": [_snapshot()],
        "captureDiagnostics": {"droppedPackets": 0, "correlationErrorCount": 0,
                                "recorderUiEvents": 0,
                                "unidentifiedTargetEvents": 0,
                                "outOfScopeEvents": 0},
        "rejections": [],
        "windows": [{"instanceId": "RI-0001", "title": "logo1",
                     "processName": "EDRClient.exe", "origin": "already_open",
                     "closed": False, "handle": 100, "pid": 42,
                     "openedBy": None}],
        "windowTimeline": {
            "schema": "edr.recorded-window-timeline/v1",
            "events": [{"id": "WE1", "order": 1, "monotonicMs": 100,
                        "kind": "initial_observed",
                        "source": "initial_inventory",
                        "instanceId": "RI-0001"}],
            "coverage": coverage,
        },
    })


def _click(seq):
    return {
        "sequence": seq, "type": "pointer_click",
        "wallTime": "2026-11-06T00:00:00Z", "monotonicMs": 150,
        "causalId": "CAUSE-x",
        "scope": {"backend": "windows_pywinauto",
                  "processName": "EDRClient.exe", "target": "target",
                  "windowTitle": "^EDRClient$"},
        "input": {"button": "left"},
        "observedTarget": {"automationId": "btnOk",
                           "controlType": "Button", "identifier": None,
                           "fingerprint": "sha256:btn", "text": "OK",
                           "rect": [0, 0, 10, 10], "ancestry": [],
                           "snapshotId": f"S{seq}", "targetId": f"T{seq:04d}",
                           "protected": None, "sameNameSiblings": None},
        "evidence": {"window": {"handle": 100, "title": "logo1",
                                 "processName": "EDRClient.exe",
                                 "rootAutomationId": "EdrMainWindow",
                                 "instanceId": "RI-0001"},
                     "foregroundPid": 42},
        "assertion": None,
    }


def _snapshot():
    return {"snapshotId": "CS-0001", "capturedAtMs": 120,
            "reason": "step", "durationMs": 5,
            "window": {"handle": 100, "title": "logo1",
                       "processName": "EDRClient.exe",
                       "rootAutomationId": "EdrMainWindow",
                       "instanceId": "RI-0001"},
            "contentHash": "sha256:snap", "stateHash": "sha256:state",
            "truncated": False, "complete": True, "controlCount": 0,
            "controls": []}


def _codes(result):
    return {i.code for i in result.issues}


def test_probe_b_unattributed_destroys_alone_do_not_break_continuity():
    """32 个环境噪音 DESTROY、零队列丢包：时间线仍可证。"""
    result = compile_recording(
        _recording({"destroyEvents": "supported", "droppedEvents": 32,
                    "unattributedDestroys": 32}),
        profile="windows_hisec",
    )
    assert "recording_window_coverage_incomplete" not in _codes(result)


def test_probe_b_real_packet_loss_still_refused():
    """droppedEvents 超过噪音明细（真丢包）：照旧拒绝。"""
    result = compile_recording(
        _recording({"destroyEvents": "supported", "droppedEvents": 32,
                    "unattributedDestroys": 30}),
        profile="windows_hisec",
    )
    assert "recording_window_coverage_incomplete" in _codes(result)


def test_probe_b_legacy_document_without_detail_still_refused():
    """旧文档无明细字段：droppedEvents>0 照旧拒绝（不伪造全噪音）。"""
    result = compile_recording(
        _recording({"destroyEvents": "supported", "droppedEvents": 32}),
        profile="windows_hisec",
    )
    assert "recording_window_coverage_incomplete" in _codes(result)
