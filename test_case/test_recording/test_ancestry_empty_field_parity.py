"""Ancestry 空字段处理 parity：录制侧与回放侧对「缺 automationId 的祖先层」必须一致。

任务验收项（recording-usability 第 1 批，ancestry 最小闭环）要求：
「ancestry 顺序、层数限制、字段命名和**空字段处理**必须与录制侧一致」。

录制侧（``target/recording/windows.py::_describe``）与回放侧
（``target/pywinauto_client.py::_build_ancestry``）的祖先行走都在组装每条链时
**丢弃空字段**（``value not in (None, "")``）：某祖先没有 automation_id 时，链
条目只保留 ``{"controlType": "Group", ...}``，不带空 ``automationId`` 键。两侧
逐字段一致后，``HybridLocator`` 的严格匹配（``hybrid.py`` 的
``if value not in (None, "")`` 跳过期望中的空字段）才能把这样一个「缺某一层
automationId」的 recorded selector 在回放观测上唯一解析。

既有 ``test_ancestry_closed_loop.py`` 的链每层都带 automation_id，没有真正走到
**空字段丢弃**分支。本回归专pin该分支：证明
1. 回放侧生产走法（dump_tree/_build_ancestry）对缺 automationId 的祖先层丢弃空
   字段，且仍按「最近父优先、camelCase」排序；
2. 一个带中间缺 automationId 祖先链的 recorded selector，经回放观测 normalize
   原样保留同一链，能被 ``HybridLocator`` 唯一解析（非 candidate_count=0）；
3. 空字段丢弃不是「宽松放宽」：若观测侧链被错误地保留空 automationId（与录制
   侧契约不一致的形状），且语义与 recorded 链相悖，locator 仍安全拒绝。

本文件只新增未跟踪测试（不触碰既有 WIP 业务文件），沿用
``test_ancestry_closed_loop.py`` 的最小 pywinauto shim。
"""

from __future__ import annotations

import importlib
import sys
import types

import pytest

from agent.locator import HybridLocator, LocatorError, LocatorPolicy, LocatorRequest
from target.windowing.observation import normalize_observed_control


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
    def __init__(self, control_type="", automation_id=None, name=""):
        self.control_type = control_type
        self.automation_id = automation_id
        self.name = name
        self.is_password = False


class _Rect:
    left = 50
    top = 50

    def width(self):
        return 30

    def height(self):
        return 14


class _Node:
    def __init__(self, control_type="", automation_id=None, name="",
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


def _tree_with_steinless_ancestor():
    """录制侧典型树：中间某祖先（pageWidget 的父即 root Window）有 automationId，
    但紧邻的一层 Group('scanPage') 没有 automation_id（真实 Qt 空 id 层）。

    root(Window, EdrMainWindow)
      -> pageWidget(Custom, ...pageWidget)
         -> scanPage(Group, automation_id="")      # 缺 automationId 的层
            -> scanWindow(Group, ...VirusScanWindow)
               -> detailWidget(Custom, ...scanDetailWidget)
                  -> tablePage(Group, ...tablePage)
                     -> table(Table, "table")
                        -> details(DataItem, "", "Details")
    """
    table = _Node("Table", "table", children=[])
    table_page = _Node("Group", "base.tablePage", children=[table])
    detail_widget = _Node("Custom", "base.scanDetailWidget", children=[table_page])
    scan_window = _Node("Group", "base.VirusScanWindow", children=[detail_widget])
    scan_page = _Node("Group", automation_id="", children=[scan_window])  # 空 id 层
    page_widget = _Node("Custom", "base.pageWidget", children=[scan_page])
    root = _Node("Window", "EdrMainWindow", children=[page_widget])
    for parent, child in ((root, page_widget), (page_widget, scan_page),
                          (scan_page, scan_window), (scan_window, detail_widget),
                          (detail_widget, table_page), (table_page, table)):
        child._parent = parent
    details = _Node("DataItem", "", "Details", parent=table)
    table._children = [details]
    return root


def _build_controls(monkeypatch):
    module = _install_fake_pywinauto(monkeypatch)
    gui = module.WindowsGUI()
    return gui, gui._build_tree(_tree_with_steinless_ancestor(), depth=0, max_depth=10)


def test_replay_walk_drops_empty_automation_id_but_keeps_order_and_case(monkeypatch):
    """回放侧 dump_tree/_build_ancestry 对缺 automationId 的中间层丢弃空字段：
    链该层只留 ``{controlType: Group}``（无空 automationId 键），且仍最近父优先、
    camelCase 字段名（与录制侧 `_describe` 的 `value not in (None, "")` 一致）。"""
    _, controls = _build_controls(monkeypatch)
    details = next(c for c in controls if c["text"] == "Details")
    chain = details["ancestry"]
    # 所有链条目都不含空字段值：空 automationId 必须在 walk 层被丢弃。
    for entry in chain:
        assert all(value not in (None, "") for value in entry.values())
        assert not any(key == "automationId" and not value for key, value in entry.items())
    # 缺 automationId 的层仍在链中，且只带 controlType（字段存在性被保留）。
    assert {"controlType": "Group"} in chain, f"empty-id layer must remain, got {chain}"
    # 最近父优先：table 在最前。
    assert chain[0] == {"controlType": "Table", "automationId": "table"}
    # camelCase 字段名（无 snake_case 残留）。
    for entry in chain:
        assert not any("_" in key for key in entry)


def test_recorded_selector_with_empty_id_ancestor_resolves_against_observation(monkeypatch):
    """录制侧合成带「中间缺 automationId 祖先」的 selector，回放观测原样保留同一链
    （正常化不改写），HybridLocator 唯一解析 —— 不再 replay_target_not_found。"""
    from agent.recording.selectors import synthesize_selector
    from target.recording.models import CaptureScope, ObservedTarget, RawCaptureEvent

    _, controls = _build_controls(monkeypatch)
    details = next(c for c in controls if c["text"] == "Details")
    observed_chain = details["ancestry"]  # 回放观测已丢弃空字段的链

    event = RawCaptureEvent(
        sequence=1, wall_time="2026-08-18T00:00:00Z", monotonic_ms=1,
        type="pointer_click",
        scope=CaptureScope("t", "windows_pywinauto", "EDRClient.exe", "^logo1$"),
        input={"button": "left"},
        observed_target=ObservedTarget(
            "OBS-1", "T0001", "sha256:x", "DataItem",
            text="Details", ancestry=tuple(observed_chain),
        ),
    )
    selector_chain = synthesize_selector(event).control["ancestry"]
    assert selector_chain == observed_chain

    identity = {"process_name": "EDRClient.exe", "title": "logo1"}
    normalized = [
        normalize_observed_control(source, identity, index)
        for index, source in enumerate(controls, start=1)
    ]
    bundle = {
        "observation_id": "OBS", "window_identity": {},
        "active_window": {"process_name": "EDRClient.exe", "title": "logo1"},
        "controls": normalized,
    }
    resolution = HybridLocator().resolve(
        LocatorRequest("gui.click", {"processName": "EDRClient.exe"},
                       {"controlType": "DataItem", "name": "Details",
                        "ancestry": selector_chain}),
        bundle, LocatorPolicy(mode="semantic_only"),
    )
    assert resolution.candidate is not None
    assert resolution.candidate.source["text"] == "Details"
    assert all(any(value not in (None, "") for value in entry.values())
               for entry in resolution.candidate.source["ancestry"])


def test_contract_mismatch_with_empty_values_is_still_safely_refused(monkeypatch):
    """空字段丢弃不是「把缺证据放宽」：若观测侧链被构造成与录制契约相悖（例如某层
    出现空 automationId 键、且语义额外引入一个不同父的匹配），locator 仍按严格匹配
    拒绝，绝不猜测。这里把观测链整体替换为一个含空值的不同链，必须 candidate 0。"""
    _, controls = _build_controls(monkeypatch)
    identity = {"process_name": "EDRClient.exe", "title": "logo1"}
    normalized = [
        normalize_observed_control(source, identity, index)
        for index, source in enumerate(controls, start=1)
    ]
    # 把 Details 的观测链改成一个空字段残留 + 不同父的链，模拟契约不一致。
    for item in normalized:
        if item["text"] == "Details":
            item["ancestry"] = [
                {"controlType": "Group", "automationId": ""},
                {"controlType": "Custom", "automationId": "other"},
            ]
    bundle = {
        "observation_id": "OBS", "window_identity": {},
        "active_window": {"process_name": "EDRClient.exe", "title": "logo1"},
        "controls": normalized,
    }
    expected_chain = [
        {"controlType": "Table", "automationId": "table"},
        {"controlType": "Group", "automationId": "base.tablePage"},
        {"controlType": "Custom", "automationId": "base.scanDetailWidget"},
        {"controlType": "Group", "automationId": "base.VirusScanWindow"},
        {"controlType": "Group"},                       # 空 id 层本身
        {"controlType": "Custom", "automationId": "base.pageWidget"},
    ]
    with pytest.raises(LocatorError) as e:
        HybridLocator().resolve(
            LocatorRequest("gui.click", {"processName": "EDRClient.exe"},
                           {"controlType": "DataItem", "name": "Details",
                            "ancestry": expected_chain}),
            bundle, LocatorPolicy(mode="semantic_only"),
        )
    assert e.value.code == "replay_target_not_found"
    assert e.value.candidate_count == 0
