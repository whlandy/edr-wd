"""C04 (design 11 §6.2/§9): 同进程同 title 父/子菜单链的身份证据。

正例：时间线证明顺序重开（同 title 菜单实例的已证生命周期区间互不重叠，
旧实例 destroyed 先于新实例 created）时，同一 collapsed replay identity 的
先后动作不得再报 compile_window_identity_ambiguous——replay 按步重解析，
录制链上有生命周期证据可执行。

负例（必须保持拒绝）：同 collapsed identity、不同 handle、时间线证明两实例
区间重叠（确实共存）时照旧拒绝；v1 无时间线文档照旧拒绝。
"""

from __future__ import annotations

import dataclasses

import pytest

from agent.recording.compiler import compile_recording
from target.recording.models import RawRecording

pytestmark = pytest.mark.unit


def _recording(events, windows=None, snapshots=None):
    return RawRecording.from_dict({
        "schema": "edr.desktop-recording/v2", "name": "menu flow",
        "sessionId": "REC-C04", "events": list(events),
        "controlSnapshots": list(snapshots or []),
        "captureDiagnostics": {"droppedPackets": 0, "correlationErrorCount": 0,
                                "recorderUiEvents": 0,
                                "unidentifiedTargetEvents": 0,
                                "outOfScopeEvents": 0},
        "rejections": [], "windows": list(windows or []),
    })


def _menu_click(seq, ms, causal, handle, instance_id, text):
    return {
        "sequence": seq, "type": "pointer_click",
        "wallTime": "2026-11-06T00:00:00Z", "monotonicMs": ms,
        "causalId": causal,
        "scope": {"backend": "windows_pywinauto",
                  "processName": "EDRClient.exe", "target": "target",
                  "windowTitle": "^EDRClient$"},
        "input": {"button": "left"},
        "observedTarget": {"automationId": None,
                           "controlType": "MenuItem", "identifier": None,
                           "fingerprint": "sha256:menu", "text": text,
                           "rect": [0, 0, 10, 10], "ancestry": [],
                           "snapshotId": f"S{seq}", "targetId": f"T{seq:04d}",
                           "protected": None, "sameNameSiblings": None},
        "evidence": {"window": {"handle": handle, "title": "EDRClient",
                                 "processName": "EDRClient.exe",
                                 "instanceId": instance_id},
                     "foregroundPid": 42},
        "assertion": None,
    }


def _main_click(seq, ms, causal):
    """The main-window button click that opens each menu (live original
    seq=9/18/25/33 shape: strong-ID button on the already-open main window)."""
    return {
        "sequence": seq, "type": "pointer_click",
        "wallTime": "2026-11-06T00:00:00Z", "monotonicMs": ms,
        "causalId": causal,
        "scope": {"backend": "windows_pywinauto",
                  "processName": "EDRClient.exe", "target": "target",
                  "windowTitle": "^logo1$"},
        "input": {"button": "left"},
        "observedTarget": {"automationId": "btnOpenMenu",
                           "controlType": "Button", "identifier": None,
                           "fingerprint": "sha256:btn", "text": "Menu",
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


_MAIN = {"title": "logo1", "processName": "EDRClient.exe", "handle": 100,
         "pid": 42, "origin": "already_open", "closed": False,
         "instanceId": "RI-0001"}


def _menu_window(handle, instance_id):
    return {"title": "EDRClient", "processName": "EDRClient.exe",
            "handle": handle, "pid": 42, "origin": "opened",
            "closed": False, "instanceId": instance_id}


def _timeline(instances):
    """Sequential menu re-opens: each instance created then destroyed before
    the next SHOW. `initial_observed` for the main window keeps the coverage
    gate satisfied."""
    events = [{"id": "WE0", "order": 0, "monotonicMs": 10,
               "instanceId": "RI-0001", "kind": "initial_observed",
               "source": "initial_inventory"}]
    ms = 100
    for order, (ri, created, destroyed) in enumerate(instances, start=1):
        events.append({"id": f"WE{order}a", "order": order * 2 - 1,
                       "monotonicMs": created, "instanceId": ri,
                       "kind": "created", "source": "native_show"})
        events.append({"id": f"WE{order}b", "order": order * 2,
                       "monotonicMs": destroyed, "instanceId": ri,
                       "kind": "destroyed", "source": "native_destroy"})
    return {"schema": "edr.recorded-window-timeline/v1", "events": events,
            "coverage": {"destroyEvents": "supported", "droppedEvents": 0}}


def _snap(snap_id, ms, handle, items):
    """A complete menu frame: the MenuItem siblings under one same-title
    window, so the weak-identity gate has its own recorded evidence and the
    window-identity face of C04 is isolated."""
    return {"snapshotId": snap_id, "capturedAtMs": ms, "reason": "step",
            "window": {"handle": handle, "processName": "EDRClient.exe",
                       "title": "EDRClient"},
            "contentHash": "sha256:" + "a" * 64,
            "stateHash": "sha256:" + "b" * 64,
            "truncated": False, "complete": True,
            "controlCount": len(items),
            "controls": [{"controlType": "MenuItem", "text": t,
                          "rect": [0, i * 22, 100, (i + 1) * 22],
                          "ancestry": [{"controlType": "Menu",
                                        "name": "EDRClient"}]}
                         for i, t in enumerate(items)]}


def _sequential_reopen_recording(ri_handles):
    """Main-window click + one menu click per menu instance; each menu
    instance lives strictly before the next (timeline-provable sequential
    replacement, never coexisting)."""
    events = []
    snapshots = []
    instances = []
    for index, (ri, handle) in enumerate(ri_handles):
        created = 100 + index * 1000
        destroyed = created + 500
        open_ms = created + 10
        click_ms = created + 100
        events.append(_main_click(index * 2 + 1, open_ms, f"CAUSE-{index}o"))
        events.append(_menu_click(index * 2 + 2, click_ms, f"CAUSE-{index}m",
                                  handle, ri, f"item{index}"))
        snapshots.append(_snap(f"MENU-{index}", click_ms - 20, handle,
                               [f"item{index}"]))
        instances.append((ri, created, destroyed))
    raw = _recording(events, windows=[_MAIN, *(_menu_window(h, ri)
                                               for ri, h in ri_handles)],
                     snapshots=snapshots)
    return dataclasses.replace(raw, window_timeline=_timeline(instances))


def test_timeline_proven_sequential_reopens_of_same_title_menu_stay_ready():
    """§6.2 误报解除面：四次同 title 菜单点击（live-REC-647312 seq14/21/28
    原型），时间线证明旧实例 destroyed 先于新实例 created——窗口从不共存，
    replay 按步重解析可执行。不得报 compile_window_identity_ambiguous。"""
    handles = [("RI-0002", 9001), ("RI-0003", 9002),
               ("RI-0004", 9003), ("RI-0005", 9004)]
    result = compile_recording(_sequential_reopen_recording(handles),
                               profile="windows_hisec")
    flagged = [i.code for i in result.issues]
    assert "compile_window_identity_ambiguous" not in flagged, (
        "时间线已证明同 title 菜单实例顺序重开（区间互不重叠）:"
        "replay 每步重解析唯一存活窗口,不得误报共存歧义")
    assert result.golden.status == "ready"


def test_overlapping_same_title_instances_stay_refused():
    """共存面保持：两个同 title 实例区间重叠（确实同时可见）时,
    同 collapsed identity 的先后动作仍必须拒绝——时间线只证明共存,
    不证明哪一步点在哪个实例上。"""
    # RI-0002 created 100 destroyed 2000; RI-0003 created 500 destroyed 1500
    # (submenu inside parent menu's lifetime — both live, both clicked).
    events = [
        _main_click(1, 100, "CAUSE-0o"),
        _menu_click(2, 200, "CAUSE-1m", 9001, "RI-0002", "语言"),
        _main_click(3, 500, "CAUSE-1o"),
        _menu_click(4, 800, "CAUSE-2m", 9002, "RI-0003", "中文"),
        _main_click(5, 1500, "CAUSE-2o"),
        _menu_click(6, 1600, "CAUSE-3m", 9001, "RI-0002", "English"),
    ]
    snapshots = [_snap("MENU-0", 180, 9001, ["语言"]),
                 _snap("MENU-1", 780, 9002, ["中文"])]
    raw = _recording(events, windows=[_MAIN, _menu_window(9001, "RI-0002"),
                                       _menu_window(9002, "RI-0003")],
                     snapshots=snapshots)
    raw = dataclasses.replace(raw, window_timeline=_timeline([
        ("RI-0002", 100, 2000), ("RI-0003", 500, 1500)]))
    result = compile_recording(raw, profile="windows_hisec")
    flagged = [i.code for i in result.issues]
    assert "compile_window_identity_ambiguous" in flagged, (
        "同 title 实例共存（区间重叠）:collapsed identity 无法区分两个"
        "同时可见的窗口,必须保持拒绝")
    # 共存拒绝必须落在点击步骤上,不只是 golden 顶层。
    step_issues = [i for s in result.golden.steps
                   for i in s.issues]
    assert "compile_window_identity_ambiguous" in step_issues


def test_v1_recording_without_timeline_keeps_refusing():
    """v1 无时间线文档保持 09 号设计拒绝语义:无生命周期证据时,
    handle 变化 + collapsed identity 无法区分共存与替换,不解除。"""
    events = [
        _menu_click(1, 100, "CAUSE-1", 9001, "RI-0002", "语言"),
        _menu_click(2, 300, "CAUSE-2", 9002, "RI-0003", "中文"),
    ]
    raw = RawRecording.from_dict({
        "schema": "edr.desktop-recording/v1",
        "sessionId": "REC-C04-V1", "name": "menu flow",
        "captureDiagnostics": {"droppedPackets": 0,
                                "correlationErrorCount": 0},
        "events": list(events),
    })
    result = compile_recording(raw, profile="windows_hisec")
    flagged = [i.code for i in result.issues]
    assert "compile_window_identity_ambiguous" in flagged


def test_events_without_instance_id_cannot_borrow_timeline_bounds():
    """事件缺 instanceId（S1 未盖章）时不得借时间线区间放宽——区间属于
    具名实例,无 id 事件与任何实例都无法建立"先后"关系。"""
    events = [
        _menu_click(1, 200, "CAUSE-1", 9001, "RI-0002", "语言"),
        _menu_click(2, 800, "CAUSE-2", 9002, "RI-0003", "中文"),
    ]
    for e in events:
        del e["evidence"]["window"]["instanceId"]
    raw = _recording(events, windows=[_MAIN, _menu_window(9001, "RI-0002"),
                                       _menu_window(9002, "RI-0003")])
    raw = dataclasses.replace(raw, window_timeline=_timeline([
        ("RI-0002", 100, 500), ("RI-0003", 600, 1000)]))
    result = compile_recording(raw, profile="windows_hisec")
    flagged = [i.code for i in result.issues]
    assert "compile_window_identity_ambiguous" in flagged, (
        "无 instanceId 的事件不得借时间线区间证明顺序重开")
