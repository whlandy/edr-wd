"""6×同名 Details 全链路端到端回归：录制 → 编译 → 观测 → 回放。

把 11.26 实测的「6 行同名 Details DataItem、祖先链完全相同、仅 rect.y 不同」
场景，一次性贯穿生产入口走完整条链，逐段断言诚实行为，防止任何一段被静默
放宽/假绿后整体回放错点：

  1. 录制侧 synthesize_selector：无强 ID 控件生成 6 层 ancestry 兜底 selector；
  2. 编译期 compile_recording：按全树候选数判歧义 -> 必须 `incomplete` +
     `compile_weak_or_ambiguous_identity`（不虚假 ready，否则回放必歧义）；
  3. 观测生产端：Windows `_build_tree` + `normalize_observed_control` 逐字段
     提供与录制侧同序同字段的 ancestry（补齐信息不对称）；
  4. 定位器 HybridLocator：信息对称后 6 行仍祖先链相同 -> 明确
     `locator_ambiguous`(candidate=6)，不猜测、不静默取录制坐标；
  5. 回放 replay_golden_trace：阻止派发，报 `replay_target_ambiguous`，附
     可行动 remediation（不含任何坐标），操作方据此处置。

所有断言都走生产入口（synthesize_selector / compile_recording /
normalize_observed_control / HybridLocator / replay_golden_trace），不使用
测试专用的旁路 helper，因此任一段回归都会被本文件捕获。
"""

from __future__ import annotations

import importlib
import sys
import types

import pytest

from action_dispatcher import ActionReceipt
from agent.execution import AtomicExecutor, StepStatus
from agent.locator import HybridLocator, LocatorError, LocatorPolicy, LocatorRequest
from agent.recording.compiler import compile_recording
from agent.recording.models import GoldenTrace, RecordedStep, ReplaySelector
from agent.recording.replay import ReplayRuntime, replay_golden_trace
from agent.recording.selectors import synthesize_selector
from target.recording.models import (
    CaptureScope,
    ObservedTarget,
    RawCaptureEvent,
    RawRecording,
)
from target.windowing.observation import normalize_observed_control

# --------------------------------------------------------------------------- #
# 与 11.26 实测一致的 6 行 Details 祖先链（逐字节相同，仅 rect.y 不同）
# --------------------------------------------------------------------------- #
_ANCHOR_CHAIN = (
    {"controlType": "Table", "automationId": "table"},
    {"controlType": "Group", "automationId": (
        "EdrMainWindow.baseWidget.pageWidget."
        "scanPage.VirusScanWindow.scanDetailWidget.tablePage")},
    {"controlType": "Custom", "automationId": (
        "EdrMainWindow.baseWidget.pageWidget."
        "scanPage.VirusScanWindow.scanDetailWidget")},
    {"controlType": "Group", "automationId": (
        "EdrMainWindow.baseWidget.pageWidget.scanPage.VirusScanWindow")},
    {"controlType": "Group", "automationId": (
        "EdrMainWindow.baseWidget.pageWidget.scanPage")},
    {"controlType": "Custom", "automationId": (
        "EdrMainWindow.baseWidget.pageWidget")},
)

_WINDOW_TITLE = "logo1"
_IDENTITY = {"process_name": "EDRClient.exe", "title": _WINDOW_TITLE}


def _six_row_event():
    """录音击第 3 行 Details 的原始事件（weak identity，ancestry = 6 层）。"""
    target = ObservedTarget(
        "OBS-1", "T0001", "sha256:x",
        automation_id=None, control_type="DataItem", text="Details",
        ancestry=_ANCHOR_CHAIN,
    )
    return RawCaptureEvent(
        sequence=1, wall_time="2026-10-28T00:00:00Z", monotonic_ms=1,
        type="pointer_click",
        scope=CaptureScope("t", "windows_pywinauto", "EDRClient.exe",
                           "^logo1$"),
        input={"button": "left"}, observed_target=target,
    )


def _six_row_controls():
    """6 个同名 Details，祖先链逐字节一致，仅 rect.y 不同（11.26 实测形态）。"""
    return [{
        "automation_id": None, "control_type": "DataItem", "text": "Details",
        "ancestry": [dict(x) for x in _ANCHOR_CHAIN],
        "rect": [1060, 403 + i * 40, 1120, 443 + i * 40],
    } for i in range(6)]


def _snapshot(controls):
    return {"snapshotId": "S1", "capturedAtMs": 0, "reason": "probe",
            "durationMs": 0,
            "window": {"title": _WINDOW_TITLE, "processName": "EDRClient.exe"},
            "contentHash": "h", "stateHash": "h", "controlCount": len(controls),
            "complete": True, "controls": list(controls)}


# --------------------------------------------------------------------------- #
# 段 1+2：录制侧 selector 带 6 层 ancestry，编译期诚实 incomplete
# --------------------------------------------------------------------------- #
def test_recording_selector_and_compile_are_honest_for_six_rows():
    event = _six_row_event()
    selector = synthesize_selector(event)
    assert selector is not None
    control = selector.control
    # 无强 ID -> ancestry 兜底进入 selector
    assert control.get("automationId") in (None, "")
    assert len(control.get("ancestry") or ()) == 6

    # 编译期：全树 6 候选 -> 必须 incomplete，绝不承诺 ready
    case = compile_recording(RawRecording(
        name="t", session_id="s1", events=(event,),
        control_snapshots=(_snapshot(_six_row_controls()),),
    )).case
    step = case.steps[0]
    assert step.status == "incomplete", (
        "weak-identity selector matching 6 controls must not be ready")
    assert "compile_weak_or_ambiguous_identity" in step.issues


# --------------------------------------------------------------------------- #
# 段 3：观测生产端补回与录制侧同序同字段的 ancestry
# --------------------------------------------------------------------------- #
def _install_fake_pywinauto(monkeypatch):
    pkg = types.ModuleType("pywinauto")
    pkg.Application = object
    pkg.timings = types.ModuleType("pywinauto.timings")
    pkg.mouse = types.ModuleType("pywinauto.mouse")
    pkg.__path__ = []
    monkeypatch.setitem(sys.modules, "pywinauto", pkg)
    monkeypatch.setitem(sys.modules, "pywinauto.timings", pkg.timings)
    monkeypatch.setitem(sys.modules, "pywinauto.mouse", pkg.mouse)
    monkeypatch.delitem(sys.modules, "target.pywinauto_client", raising=False)
    return importlib.import_module("target.pywinauto_client")


class _EI:
    def __init__(self, control_type="", automation_id="", name=""):
        self.control_type = control_type
        self.automation_id = automation_id
        self.name = name
        self.is_password = False


class _Rect:
    left = 40
    top = 400

    def width(self):
        return 60

    def height(self):
        return 40


class _Node:
    def __init__(self, control_type="", automation_id="", name="",
                 parent=None, children=()):
        self.element_info = _EI(control_type, automation_id, name)
        self._parent = parent
        self._children = list(children)

    def parent(self):
        return self._parent

    def children(self):
        return self._children

    def window_text(self):
        return self.element_info.name or ""

    def control_id(self):
        return None

    def friendly_class_name(self):
        return self.element_info.control_type or ""

    def rectangle(self):
        return _Rect()

    def is_visible(self):
        return True

    def is_enabled(self):
        return True

    def automation_id(self):
        return self.element_info.automation_id

    def control_type(self):
        return self.element_info.control_type

    def get_toggle_state(self):
        return None

    def is_selected(self):
        return False


def _tree_with_six_rows():
    """与 11.26 扫描表同形状的 UIA 树：6 行 Details 挂在同一 Table 下。"""
    table = _Node("Table", "table", children=[])
    table_page = _Node("Group", _ANCHOR_CHAIN[1]["automationId"],
                       children=[table])
    detail_widget = _Node("Custom", _ANCHOR_CHAIN[2]["automationId"],
                          children=[table_page])
    scan_window = _Node("Group", _ANCHOR_CHAIN[3]["automationId"],
                        children=[detail_widget])
    scan_page = _Node("Group", _ANCHOR_CHAIN[4]["automationId"],
                      children=[scan_window])
    page_widget = _Node("Custom", _ANCHOR_CHAIN[5]["automationId"],
                        children=[scan_page])
    root = _Node("Window", "EdrMainWindow", children=[page_widget])
    for parent, child in ((root, page_widget), (page_widget, scan_page),
                          (scan_page, scan_window), (scan_window, detail_widget),
                          (detail_widget, table_page), (table_page, table)):
        child._parent = parent
    table._children = [
        _Node("DataItem", automation_id="", name="Details", parent=table)
        for _ in range(6)
    ]
    return root


def test_observation_production_emits_matching_ancestry_for_six_rows(monkeypatch):
    """Windows `_build_tree` + `normalize_observed_control` 补齐 ancestry，
    且 6 行 ancestry 逐字节一致（信息对称后仍然歧义，而不是 0 候选）。"""
    module = _install_fake_pywinauto(monkeypatch)
    gui = module.WindowsGUI()
    controls = gui._build_tree(_tree_with_six_rows(), depth=0, max_depth=10)
    rows = [c for c in controls if c["text"] == "Details"]
    assert len(rows) == 6
    observed = [normalize_observed_control(c, _IDENTITY, i)
                for i, c in enumerate(rows, start=1)]

    # 观测 ancestry 与录制侧 selector ancestry 逐字节一致
    chain = [dict(x) for x in _ANCHOR_CHAIN]
    assert all(o["control_type"] == "DataItem" for o in observed)
    assert all(o["text"] == "Details" for o in observed)
    assert all(o.get("automation_id") in (None, "") for o in observed)
    # 每行都带完整 6 层、且彼此逐字节一致（仅 rect.y 不同，夹具已保证）
    serialized = {str(o.get("ancestry")) for o in observed}
    assert len(serialized) == 1, "six rows must share byte-identical ancestry"
    assert list(observed[0]["ancestry"]) == chain


# --------------------------------------------------------------------------- #
# 段 4：HybridLocator 对信息对称后的 6 行返回明确歧义，不猜测
# --------------------------------------------------------------------------- #
def test_locator_returns_explicit_ambiguity_not_a_guess():
    normalized = [
        normalize_observed_control(c, _IDENTITY, i)
        for i, c in enumerate(_six_row_controls(), start=1)
    ]
    bundle = {
        "observation_id": "OBS", "window_identity": {},
        "active_window": {"process_name": "EDRClient.exe", "title": _WINDOW_TITLE},
        "controls": normalized,
    }
    request = LocatorRequest(
        "gui.click",
        {"processName": "EDRClient.exe"},
        {"controlType": "DataItem", "name": "Details",
         "ancestry": [dict(x) for x in _ANCHOR_CHAIN]},
    )
    with pytest.raises(LocatorError) as e:
        HybridLocator().resolve(request, bundle, LocatorPolicy(mode="semantic_only"))
    assert e.value.code == "locator_ambiguous"
    assert e.value.candidate_count == 6
    assert len(e.value.candidate_rects or []) == 6


# --------------------------------------------------------------------------- #
# 段 5：回放经生产入口阻止派发并给出可行动 remediation，绝不含坐标
# --------------------------------------------------------------------------- #
class _Observations:
    def __init__(self, controls):
        self.controls = controls
        self.counter = 0
        self.snapshots = {}

    def refresh(self):
        self.counter += 1
        snapshot_id = f"OBS-{self.counter}"
        controls = []
        for i, source in enumerate(self.controls, start=1):
            item = dict(source)
            item["target_id"] = f"T{self.counter:02d}{i:02d}"
            item["process_name"] = source.get("process_name") or "EDRClient.exe"
            item["window_title"] = _WINDOW_TITLE
            controls.append(item)
        self.snapshots[snapshot_id] = {
            "snapshot_id": snapshot_id,
            "controls": controls,
            "windows": [{"process_name": "EDRClient.exe", "title": _WINDOW_TITLE}],
            "active_window": {
                "process_name": "EDRClient.exe", "title": _WINDOW_TITLE,
                "rect": [0, 0, 1280, 720],
            },
            "screenshot_scope": "window",
            "screenshot_redacted": True,
        }
        return snapshot_id

    def latest_snapshot_id(self):
        return f"OBS-{self.counter}" if self.counter else None

    def get_snapshot(self, snapshot_id):
        return self.snapshots[snapshot_id]


def _golden(step):
    return GoldenTrace(
        name="six rows", status="ready",
        source_recording={"schema": "edr.desktop-recording/v1", "sha256": "s"},
        catalog={"version": "1.0.0", "digest": "sha256:catalog"},
        environment={"backend": "windows_pywinauto", "target": "win-dev"},
        steps=(step,),
    )


def _replay_obs_controls():
    """回放观测：6 行 Details（与录制同 Field 语义，ancestry 已补齐）。"""
    return [{
        "process_name": "EDRClient.exe", "window_title": _WINDOW_TITLE,
        "control_type": "DataItem", "text": "Details",
        "automation_id": None,
        "ancestry": [dict(x) for x in _ANCHOR_CHAIN],
        "rect": [1060, 403 + i * 40, 1120, 443 + i * 40],
    } for i in range(6)]


def test_replay_six_rows_ambiguous_never_dispatches(monkeypatch):
    selector = ReplaySelector(
        window={"processName": "EDRClient.exe", "titleRegex": "^logo1$"},
        control={"controlType": "DataItem", "name": "Details",
                 "ancestry": [dict(x) for x in _ANCHOR_CHAIN]},
    )
    step = RecordedStep("step-0001", "gui.click", {}, selector)
    observations = _Observations(_replay_obs_controls())
    calls = []

    def dispatch(**kwargs):
        calls.append(kwargs)
        return ActionReceipt.from_ok(
            action_id=kwargs["action_id"],
            action_code=kwargs["action_code"],
            request_id=kwargs["request_id"],
            result={"ok": True},
        )

    runtime = ReplayRuntime(AtomicExecutor(dispatch=dispatch,
                                           observation_provider=observations),
                            "1.0.0", "sha256:catalog")
    result = replay_golden_trace(runtime, _golden(step))

    assert calls == [], "six ambiguous rows must never dispatch a click"
    assert result.task_success is False
    step_result = result.case_result.step_results[0]
    assert step_result.status is StepStatus.BLOCKED
    assert step_result.error["code"] == "replay_target_ambiguous"
    details = step_result.error.get("details") or {}
    assert details.get("candidate_count") == 6
    assert len(details.get("candidate_rects") or []) == 6
    remediation = details.get("remediation")
    assert isinstance(remediation, str) and remediation
    # 可行动指引，绝不含裸坐标
    assert not any(banned in remediation
                   for banned in ("x=", "y=", "点击坐标", "使用录制坐标"))
