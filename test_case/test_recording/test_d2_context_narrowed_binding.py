"""D2 续：父上下文约束下独立识别子菜单（用户实施口径锁死）。

证据语义（design 11 D2 续，实机 REC-c6b060c2 证明 Qt 族无 Win32 owner 位）：
- 关系证据 = 因果 SHOW（openedBy）+ 生命周期包含（区间事实，非邻近性）
  + 候选结构观测（menu-layout/v1）三者组合；任一缺失 fail closed。
- 生命周期包含只用于排除矛盾（子窗口必须生灭于父可见区间内），不单独
  证明归属——无关窗口也可同区间存在。
- 绑定侧：同名多候选 → 逐候选**在候选自身控件树内**应用声明的结构选择
  器；恰好一个候选内唯一解析 → 绑定该候选；零/多个 → 拒绝，不选
  "最新窗口"。
- 派发前重验证由既有 reverify 承担（可见性/实例/身份）；HIDE 后旧绑定
  LOST 不可续用，重开必须重新观察（§7.4 既有契约）。

锁定反例：
- 无关同形菜单：两个候选树内都能唯一解析 → 拒绝（不选最新）。
- 单一候选结构唯一 → 绑定命中者（正例）。
- 旧菜单已可见（打开动作前已存在，无因果 SHOW）→ 编译期不豁免。
- 父绑定失效（无 BOUND 父、无结构选择器）→ 照旧拒绝。
- 结构变化（选择器在两个候选内都解析不了）→ 拒绝。
- 无结构选择器的多候选 → 照旧拒绝（不猜）。
"""

from __future__ import annotations

import pytest

from agent.recording import window_binding as wb
from agent.recording.compiler import compile_recording
from test_case.test_recording.test_d2_owner_relation_binding import (
    _click, _compile, _recording, _window,
    _MENU_HANDLE, _SETTINGS_HANDLE, _SIBLING_HANDLE,
)

pytestmark = pytest.mark.unit


def test_positive_context_narrowed_step_compiles_ready_and_defers():
    """两轮语言切换（实机 REC-31aa1e9b 形状）：每轮"对话框+菜单"共享同一
    openedBy 因果、菜单生于对话框可见区间内、两轮对话框先后有序（无共存）
    → 同身份不同 handle 的两步菜单点击不再判 ambiguous。"""
    events = [_click(1, 1000, "RI-0003"), _click(2, 1100, "RI-0004"),
              _click(3, 2000, "RI-0005"), _click(4, 2100, "RI-0006")]
    windows = [
        _window("RI-0001", 13239958, origin="already_open"),
        # round 1: dialog RI-0003 + menu RI-0004，同 cause CAUSE-0001
        dict(_window("RI-0003", 6358946), openedBy="CAUSE-0001"),
        dict(_window("RI-0004", 4522006), openedBy="CAUSE-0001"),
        # round 2: dialog RI-0005 + menu RI-0006，同 cause CAUSE-0003
        dict(_window("RI-0005", 1836874), openedBy="CAUSE-0003"),
        dict(_window("RI-0006", 5571864), openedBy="CAUSE-0003"),
    ]
    rec = _recording(windows, events)
    tl = rec.window_timeline
    tl["events"] = [
        {"id": "WE1", "order": 0, "monotonicMs": 900,
         "instanceId": "RI-0003", "kind": "created", "source": "native_show"},
        {"id": "WE2", "order": 1, "monotonicMs": 1000,
         "instanceId": "RI-0004", "kind": "created", "source": "native_show"},
        {"id": "WE3", "order": 2, "monotonicMs": 1500,
         "instanceId": "RI-0004", "kind": "hidden", "source": "native_hide"},
        {"id": "WE4", "order": 3, "monotonicMs": 1520,
         "instanceId": "RI-0003", "kind": "destroyed",
         "source": "native_destroy"},
        {"id": "WE5", "order": 4, "monotonicMs": 1900,
         "instanceId": "RI-0005", "kind": "created", "source": "native_show"},
        {"id": "WE6", "order": 5, "monotonicMs": 2000,
         "instanceId": "RI-0006", "kind": "created", "source": "native_show"},
        {"id": "WE7", "order": 6, "monotonicMs": 2500,
         "instanceId": "RI-0006", "kind": "hidden", "source": "native_hide"},
        {"id": "WE8", "order": 7, "monotonicMs": 2520,
         "instanceId": "RI-0005", "kind": "destroyed",
         "source": "native_destroy"},
    ]
    result = compile_recording(rec)
    statuses = [(s.step_id, s.status, tuple(s.issues)) for s in result.case.steps]
    menu_steps = [st for st in statuses if st[0] in ("step-0002", "step-0004")]
    assert all(st == "ready" for _, st, _ in menu_steps), statuses


def test_negative_opened_before_parent_not_narrowable():
    """旧菜单已可见：子窗口首事实不晚于父 → 生命周期包含不成立 → 照旧
    ambiguous（打开动作前已存在的同名菜单不是本次动作的子菜单）。"""
    windows = [
        _window("RI-0001", 13239958, origin="already_open"),
        _window("RI-0004", _SETTINGS_HANDLE, origin="already_open"),
        dict(_window("RI-0005", _MENU_HANDLE), openedBy="CAUSE-0001"),
    ]
    events = [_click(1, 1000, "RI-0004"), _click(2, 1100, "RI-0005")]
    rec = _recording(windows, events)
    rec.window_timeline["events"] = [
        {"id": "WE1", "order": 0, "monotonicMs": 800,
         "instanceId": "RI-0005", "kind": "initial_observed",
         "source": "initial_inventory"},
        {"id": "WE2", "order": 1, "monotonicMs": 900,
         "instanceId": "RI-0004", "kind": "initial_observed",
         "source": "initial_inventory"},
    ]
    result = compile_recording(rec)
    codes = {i.code for i in result.issues}
    assert "compile_window_identity_ambiguous" in codes


def test_negative_no_causal_show_not_narrowable():
    """manifest 无 openedBy（无因果 SHOW）→ 组合证据缺一 → 照旧 ambiguous
    （生命周期包含只排除矛盾，不证明归属）。"""
    windows = [
        _window("RI-0001", 13239958, origin="already_open"),
        _window("RI-0004", _SETTINGS_HANDLE, origin="already_open"),
        _window("RI-0005", _MENU_HANDLE),
    ]
    events = [_click(1, 1000, "RI-0004"), _click(2, 1100, "RI-0005")]
    rec = _recording(windows, events)
    rec.window_timeline["events"] = [
        {"id": "WE1", "order": 0, "monotonicMs": 900,
         "instanceId": "RI-0004", "kind": "initial_observed",
         "source": "initial_inventory"},
        {"id": "WE2", "order": 1, "monotonicMs": 1050,
         "instanceId": "RI-0005", "kind": "created",
         "source": "native_show"},
    ]
    result = compile_recording(rec)
    codes = {i.code for i in result.issues}
    assert "compile_window_identity_ambiguous" in codes


# ---- binding-side structure narrowing --------------------------------------


class _TreeBackend:
    """同名双候选窗口，各自树内可按 automationId 解析。"""

    def __init__(self, trees: dict[int, list[dict]]):
        self._trees = trees
        self.exe_path = "/opt/EDRClient/EDRClient.exe"
        self.dump_calls: list[int] = []

    def exe_path_for_pid(self, pid):
        return self.exe_path

    def refresh_inventory(self):
        from types import MappingProxyType
        items = []
        for handle in self._trees:
            items.append(MappingProxyType({
                "handle": handle, "native_id": str(handle), "pid": 1984,
                "process_name": "EDRClient.exe", "title": "EDRClient",
                "root_control_id": "", "lost": False, "generation": 2,
                "owner_handle": None}))
        return items

    def dump_tree_for_window(self, handle, max_depth=12):
        self.dump_calls.append(int(handle))
        return {"ok": True, "controls": self._trees.get(int(handle), [])}

    def focus_existing_window(self, **kwargs):
        return {"ok": True}


def _protocol(backend):
    """构造最小 target WindowBindingFacade（租约/跟踪桩；U3 已覆盖全链）。"""
    from target.windowing.binding import WindowBindingFacade
    facade = WindowBindingFacade.__new__(WindowBindingFacade)
    facade._session = _Session(backend)
    facade._policy = None
    facade._lease_token = "tok"
    facade._declared = {}
    facade._golden_windows = {}
    facade._allowed_processes = frozenset({"edrclient"})
    facade._installed_paths = ()
    facade._effect_baselines = {}
    facade._tracker_fault = None
    facade._tracker = None
    facade._bindings = {}
    facade._token_to_window = {}
    facade._revision = 0
    facade._generation = 2
    facade._coverage = "full"
    facade._supported = True
    facade._run_id = None
    import threading
    facade._lock = threading.RLock()
    return facade


class _Session:
    def __init__(self, backend):
        self.backend = backend

    def refresh(self):
        return self.backend.refresh_inventory()


_MENU_CONTROL = {
    "controlType": "MenuItem",
    "name": "简体中文",
    "ancestry": ({"controlType": "Menu"},
                 {"controlType": "MenuItem", "name": "Language"},
                 {"controlType": "Window", "name": "EDRClient"},
                 {"controlType": "Pane", "name": "Desktop 1"}),
    "structural": {"kind": "menu-layout/v1", "index": 0,
                   "layout": [[0.0, 0.0, 1.0, 0.5], [0.0, 0.5, 1.0, 1.0]]},
}


def test_positive_structure_narrow_binds_unique_candidate():
    """两个同名候选；结构选择器只在候选 B 树内唯一解析 → 绑定 B。"""
    backend = _TreeBackend({
        _SETTINGS_HANDLE: [{"target_id": "T1", "control_type": "Button", "text": "OK",
                            "automation_id": "btnOk"}],
        _MENU_HANDLE: [
                        {"target_id": "T2", "control_type": "MenuItem", "text": "简体中文",
                         "rect": [985, 154, 1138, 186],
                         "ancestry": [{"controlType": "Menu"},
                                      {"controlType": "MenuItem",
                                       "name": "Language"},
                                      {"controlType": "Window",
                                       "name": "EDRClient"},
                                      {"controlType": "Pane",
                                       "name": "Desktop 1"}]},
                        {"target_id": "T2b", "control_type": "MenuItem", "text": "English",
                         "rect": [985, 186, 1138, 218],
                         "ancestry": [{"controlType": "Menu"},
                                      {"controlType": "MenuItem",
                                       "name": "Language"},
                                      {"controlType": "Window",
                                       "name": "EDRClient"},
                                      {"controlType": "Pane",
                                       "name": "Desktop 1"}]}],
    })
    protocol = _protocol(backend)
    identity = {"processName": "EDRClient.exe", "titleRegex": "^EDRClient$"}
    item = protocol._structure_narrow(
        list(backend.refresh_inventory()), identity, _MENU_CONTROL)
    assert str(_field(item, 'native_id')) == str(_MENU_HANDLE)
    assert backend.dump_calls and set(backend.dump_calls) == {
        _SETTINGS_HANDLE, _MENU_HANDLE}


def _field(obj, *keys):
    for k in keys:
        try:
            return obj[k]
        except (TypeError, KeyError, IndexError):
            v = getattr(obj, k, None)
            if v is not None:
                return v
    return None


def test_negative_two_unique_candidates_refused():
    """无关同形菜单：两个候选树内都唯一解析 → 拒绝，不选最新。"""
    menu_control = dict(_MENU_CONTROL)
    backend = _TreeBackend({
        _SETTINGS_HANDLE: [
            {"target_id": "T3", "control_type": "MenuItem", "text": "简体中文",
             "rect": [985, 154, 1138, 186],
             "ancestry": [{"controlType": "Menu"},
                          {"controlType": "MenuItem", "name": "Language"},
                          {"controlType": "Window", "name": "EDRClient"},
                          {"controlType": "Pane", "name": "Desktop 1"}]},
            {"target_id": "T3b", "control_type": "MenuItem", "text": "English",
             "rect": [985, 186, 1138, 218],
             "ancestry": [{"controlType": "Menu"},
                          {"controlType": "MenuItem", "name": "Language"},
                          {"controlType": "Window", "name": "EDRClient"},
                          {"controlType": "Pane", "name": "Desktop 1"}]}],
        _MENU_HANDLE: [
                        {"target_id": "T2", "control_type": "MenuItem", "text": "简体中文",
                         "rect": [985, 154, 1138, 186],
                         "ancestry": [{"controlType": "Menu"},
                                      {"controlType": "MenuItem",
                                       "name": "Language"},
                                      {"controlType": "Window",
                                       "name": "EDRClient"},
                                      {"controlType": "Pane",
                                       "name": "Desktop 1"}]},
                        {"target_id": "T2b", "control_type": "MenuItem", "text": "English",
                         "rect": [985, 186, 1138, 218],
                         "ancestry": [{"controlType": "Menu"},
                                      {"controlType": "MenuItem",
                                       "name": "Language"},
                                      {"controlType": "Window",
                                       "name": "EDRClient"},
                                      {"controlType": "Pane",
                                       "name": "Desktop 1"}]}],
    })
    protocol = _protocol(backend)
    identity = {"processName": "EDRClient.exe", "titleRegex": "^EDRClient$"}
    with pytest.raises(Exception, match="narrowing resolved 2"):
        protocol._structure_narrow(
            list(backend.refresh_inventory()), identity, menu_control)


def test_negative_zero_candidates_resolving_refused():
    """结构变化：选择器在两个候选内都解析不了 → 拒绝。"""
    backend = _TreeBackend({
        _SETTINGS_HANDLE: [{"target_id": "T1", "control_type": "Button", "text": "OK",
                            "automation_id": "btnOk"}],
        _MENU_HANDLE: [{"target_id": "T5", "control_type": "Button", "text": "Cancel",
                        "automation_id": "btnCancel",
                        "ancestry": [{"controlType": "Menu"}]}],
    })
    protocol = _protocol(backend)
    identity = {"processName": "EDRClient.exe", "titleRegex": "^EDRClient$"}
    with pytest.raises(Exception, match="narrowing resolved 0"):
        protocol._structure_narrow(
            list(backend.refresh_inventory()), identity, _MENU_CONTROL)


def test_negative_no_selector_multi_candidate_refused():
    """无结构选择器的多候选 → 照旧 ambiguous（不猜）。"""
    backend = _TreeBackend({
        _SETTINGS_HANDLE: [], _MENU_HANDLE: []})
    protocol = _protocol(backend)
    identity = {"processName": "EDRClient.exe", "titleRegex": "^EDRClient$"}
    with pytest.raises(Exception, match="requires a declared control selector"):
        protocol._structure_narrow(
            list(backend.refresh_inventory()), identity, None)
