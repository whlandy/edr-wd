"""Regression tests for the compile-time weak-identity uniqueness fixes.

Covers three reviewer-confirmed defects:

* P1a — ``_weak_selector_candidate_count`` used to sum matches across ALL
  snapshots. A single unique control captured before/after a page-state change
  (two snapshots of the same window) was counted twice and falsely marked
  ``ambiguous``; the same selector tree in another window also inflated the
  count. The fix selects the single event-bound frame and counts within it.
* P1b — ``content_hash`` omitted ancestry, so a hierarchy-only change was
  deduplicated as "unchanged" and the compiler proved uniqueness against stale
  structure. The fix includes canonical ancestry in the identity digest.
* P2 — recursive ``_build_tree`` turned an ``element_info`` read failure into
  a ``{}`` ancestry placeholder propagated to descendants, inconsistent with
  the recording-side all-or-nothing chain. The fix sentinels the failure and
  clears the whole chain.
"""

from __future__ import annotations

import importlib
import sys
import types
from dataclasses import replace

from agent.recording.compiler import _weak_selector_candidate_count, compile_recording
from agent.recording.selectors import synthesize_selector
from target.recording.inventory import content_hash, normalize_controls
from target.recording.models import (
    CaptureScope,
    ObservedTarget,
    RawCaptureEvent,
    RawRecording,
)

_ANCHOR_CHAIN = (
    {"controlType": "Table", "automationId": "table"},
    {"controlType": "Group", "automationId": "base.tablePage"},
    {"controlType": "Custom", "automationId": "base.scanDetailWidget"},
    {"controlType": "Group", "automationId": "base.VirusScanWindow"},
    {"controlType": "Group", "automationId": "base.scanPage"},
    {"controlType": "Custom", "automationId": "base.pageWidget"},
)


def _click_target(same_name_siblings=1):
    return RawCaptureEvent(
        sequence=1, wall_time="2026-10-28T00:00:00Z", monotonic_ms=1,
        type="pointer_click",
        scope=CaptureScope("t", "windows_pywinauto", "EDRClient.exe", "^logo1$"),
        input={"button": "left"},
        observed_target=ObservedTarget(
            "OBS-1", "T0001", "sha256:x",
            automation_id=None, control_type="DataItem", text="Details",
            ancestry=_ANCHOR_CHAIN, same_name_siblings=same_name_siblings,
        ),
    )


def _dat_item(rect_y=403):
    return {
        "automation_id": None, "control_type": "DataItem", "text": "Details",
        "ancestry": [dict(x) for x in _ANCHOR_CHAIN],
        "rect": [1060, rect_y, 1120, rect_y + 40],
    }


def _snapshot(controls, *, window=None, index=1, captured_at_ms=0):
    return {
        "snapshotId": f"S{index}", "capturedAtMs": captured_at_ms,
        "reason": "probe", "durationMs": 0,
        "window": dict(window or {"title": "logo1", "processName": "EDRClient.exe"}),
        "contentHash": "h", "stateHash": "h", "controlCount": len(controls),
        "complete": True, "controls": list(controls),
    }


def _compile(event, snapshots):
    return compile_recording(RawRecording(
        name="t", session_id="s1", events=(event,),
        control_snapshots=tuple(snapshots),
    )).case


# --------------------------------------------------------------------------- #
# P1a — single event-bound frame counting
# --------------------------------------------------------------------------- #

def test_same_unique_control_in_two_snapshots_is_not_ambiguous():
    """同窗口两个快照（页态切换前后）出现同一唯一控件：不得数成 2 个候选。

    修复前会在两个快照各数一次 -> candidate_count=2 -> 假 ambiguous。修复后
    只在与事件关联的单帧内计数 -> 1 -> ready。
    """
    event = _click_target()
    selector = synthesize_selector(event)
    snapshots = [
        _snapshot([_dat_item()], index=1, captured_at_ms=0),
        _snapshot([_dat_item()], index=2, captured_at_ms=2),
    ]
    assert _weak_selector_candidate_count(selector.control, snapshots, event) == 1
    case = _compile(event, snapshots)
    assert case.status == "ready"
    assert case.steps[0].issues == ()


def test_same_selector_in_other_window_not_counted():
    """同一 selector 结构出现在另一窗口：不得让本窗口的候选数被抬高。

    修复前会把另一窗口的同名树也计入 -> 假 ambiguous。修复后按事件窗口过滤，
    跨窗口的同名树被排除。
    """
    event = _click_target()
    selector = synthesize_selector(event)
    # Real inventory snapshots carry rootAutomationId (the event window derives
    # 'base' from the target's ancestry), so the matcher can tell windows apart.
    this_window = {"title": "logo1", "processName": "EDRClient.exe",
                   "rootAutomationId": "base"}
    other_window = {"title": "logo2", "processName": "EDRClient.exe",
                    "rootAutomationId": "other"}
    snapshots = [
        _snapshot([_dat_item()], index=1, window=this_window, captured_at_ms=0),   # 本窗口 1 个
        _snapshot([_dat_item()] * 6, index=2, window=other_window, captured_at_ms=0),  # 另一窗口 6 个
    ]
    assert _weak_selector_candidate_count(selector.control, snapshots, event) == 1
    case = _compile(event, snapshots)
    assert case.status == "ready"
    assert case.steps[0].issues == ()


def test_six_rows_in_event_frame_still_ambiguous():
    """6 个同名行确实在事件窗口的帧里：必须仍是歧义（诚实拒绝，不假唯一）。

    这是上一轮已钉住的安全行为；引入单帧选择后不得把它做没了。
    """
    event = _click_target()
    selector = synthesize_selector(event)
    snapshots = [_snapshot([_dat_item(y) for y in (403, 443, 483, 523, 563, 603)])]
    assert _weak_selector_candidate_count(selector.control, snapshots, event) == 6
    case = _compile(event, snapshots)
    assert case.status == "incomplete"
    assert "compile_weak_or_ambiguous_identity" in case.steps[0].issues


def test_no_snapshot_matches_event_window_is_unresolved():
    """没有与事件窗口绑定的帧：不得跨窗口求和，诚实 unresolved/不 ready。"""
    event = _click_target()
    selector = synthesize_selector(event)
    other_process = {"title": "logo1", "processName": "Other.exe"}
    snapshots = [_snapshot([_dat_item()], index=1, window=other_process)]
    assert _weak_selector_candidate_count(selector.control, snapshots, event) is None
    case = _compile(event, snapshots)
    assert case.status == "incomplete"
    assert "compile_weak_identity_unresolved" in case.steps[0].issues


# --------------------------------------------------------------------------- #
# P1b — ancestry part of content identity
# --------------------------------------------------------------------------- #

def test_ancestry_change_changes_content_hash():
    """仅祖先链变化（层级迁移）必须改变 content_hash，不得被去重成 'unchanged'。

    修复前 ancestry 不在身份字段里，层级变化但文本等字段不变的树会被判为
    'unchanged' 丢弃，编译器对着过期结构证明唯一性。
    """
    base = normalize_controls([{
        "automation_id": None, "control_type": "DataItem", "text": "Details",
        "ancestry": [dict(x) for x in _ANCHOR_CHAIN],
    }])
    moved = normalize_controls([{
        "automation_id": None, "control_type": "DataItem", "text": "Details",
        "ancestry": [
            {"controlType": "Table", "automationId": "otherTable"},
            *[dict(x) for x in _ANCHOR_CHAIN[1:]],
        ],
    }])
    assert content_hash(base) != content_hash(moved)


def test_same_identity_same_ancestry_same_hash():
    """身份与祖先链都相同（仅几何变化）时 content_hash 不变。"""
    a = normalize_controls([{
        "automation_id": "x.a", "control_type": "Button", "text": "确定",
        "ancestry": [{"controlType": "Group", "automationId": "p"}],
    }])
    b = normalize_controls([{
        "automation_id": "x.a", "control_type": "Button", "text": "确定",
        "ancestry": [{"controlType": "Group", "automationId": "p"}],
        "rectangle": {"x": 9, "y": 9, "w": 1, "h": 1},
    }])
    assert content_hash(a) == content_hash(b)


# --------------------------------------------------------------------------- #
# P2 — _build_tree invalid-ancestor sentinel
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
    top = 40

    def width(self):
        return 60

    def height(self):
        return 40


class _BrokenEI:
    @property
    def control_type(self):
        raise RuntimeError("broken")

    @property
    def automation_id(self):
        raise RuntimeError("broken")

    @property
    def name(self):
        raise RuntimeError("broken")


class _Node:
    def __init__(self, control_type="", automation_id="", name="",
                 parent=None, children=(), broken=False):
        self._ei = _BrokenEI() if broken else _EI(control_type, automation_id, name)
        self.element_info = self._ei
        self._parent = parent
        self._children = list(children)

    def parent(self):
        return self._parent

    def children(self):
        return self._children

    def window_text(self):
        return self._ei.name or ""

    def control_id(self):
        return None

    def friendly_class_name(self):
        return self._ei.control_type or ""

    def rectangle(self):
        return _Rect()

    def is_visible(self):
        return True

    def is_enabled(self):
        return True

    def automation_id(self):
        return self._ei.automation_id

    def control_type(self):
        return self._ei.control_type

    def get_toggle_state(self):
        return None

    def is_selected(self):
        return False


def test_build_tree_invalid_ancestor_clears_descendant_chain(monkeypatch):
    """递归建树时某祖先 element_info 读取失败：后代必须得到空链 []，而非 [{}]。

    修复前 ``_ancestor_entry`` 把异常吞成 ``{}``，后代链变成 ``[{}]``，与录制端
    “任一祖先读取失败即整链清空”的契约不一致。
    """
    module = _install_fake_pywinauto(monkeypatch)
    gui = module.WindowsGUI()
    # root -> broken(group) -> leaf(button)
    root = _Node("Pane", "root")
    broken = _Node("Group", "broken", broken=True, parent=root)
    leaf = _Node("Button", "leaf", parent=broken)
    broken._children = [leaf]
    root._children = [broken]

    controls = gui._build_tree(root, depth=0, max_depth=6)
    # The broken node loses its name/class/automation id (reading element_info
    # raises, caught as empty), so identify the leaf by its intact control_type.
    leafs = [c for c in controls if c.get("control_type") == "Button"]
    assert len(leafs) == 1
    assert leafs[0]["ancestry"] == [], leafs[0]["ancestry"]
    # no `{}` placeholder anywhere in any chain
    for c in controls:
        assert all(isinstance(e, dict) and e for e in c["ancestry"]), c


def test_multi_level_invalid_ancestry_stays_empty(monkeypatch):
    """broken 祖先的失效状态必须传播到**所有**后代，不止直接子节点。

    root -> broken -> child -> grandchild：child 链为空后，健康 child 不得
    为 grandchild 重建一条非空链（修复前 grandchild 会拿到
    [{'controlType':'Pane','automationId':'child'}]）。
    """
    module = _install_fake_pywinauto(monkeypatch)
    gui = module.WindowsGUI()
    root = _Node("Pane", "root")
    broken = _Node("Group", "broken", broken=True, parent=root)
    child = _Node("Group", "child", parent=broken)
    grandchild = _Node("Button", "grandchild", parent=child)
    broken._children = [child]
    child._children = [grandchild]
    root._children = [broken]

    controls = gui._build_tree(root, depth=0, max_depth=6)
    gc = [c for c in controls if c.get("control_type") == "Button"]
    assert len(gc) == 1
    assert gc[0]["ancestry"] == [], gc[0]["ancestry"]


def test_two_same_root_windows_disambiguated_by_handle():
    """同进程、同 root、同标题的两个窗口：必须按 handle 关联事件窗。

    事件窗 handle=101（1 个候选）与另一窗 handle=202（6 个候选）：选择
    101 帧 -> 唯一 -> ready；绝不因 202 帧被选中而误报歧义。上一轮测试
    用了不同 root 且事件不带 handle，未覆盖真实缺口。
    """
    event = _click_target()
    event = replace(event, evidence={"window": {"handle": 101}})
    selector = synthesize_selector(event)
    w101 = {"title": "logo1", "processName": "EDRClient.exe",
            "rootAutomationId": "base", "handle": 101}
    w202 = {"title": "logo1", "processName": "EDRClient.exe",
            "rootAutomationId": "base", "handle": 202}
    snapshots = [
        _snapshot([_dat_item()] * 6, index=1, window=w202, captured_at_ms=0),
        _snapshot([_dat_item()], index=2, window=w101, captured_at_ms=0),
    ]
    assert _weak_selector_candidate_count(selector.control, snapshots, event) == 1
    case = _compile(event, snapshots)
    assert case.status == "ready"
    assert case.steps[0].issues == ()


def _snapshot_truncated(controls, **kw):
    # A truncated tree is, by definition, not positively complete. The
    # producer contract is `complete=False` whenever any depth/child-count
    # cap or children-enumeration failure occurred.
    return {**_snapshot(controls, **kw), "truncated": True, "complete": False}


def test_truncated_weak_tree_does_not_prove_ready():
    """截断/虚拟化的局部树不得证明弱身份全局唯一。

    快照带 truncated=True 且只看到 1 个候选时，仍必须 incomplete（可能重复
    控件位于遍历之外），不能用局部树输出 ready。
    """
    event = _click_target(same_name_siblings=1)
    case = _compile(event, [_snapshot_truncated([_dat_item()])])
    assert case.status == "incomplete"
    assert "compile_weak_identity_unresolved" in case.steps[0].issues


def test_truncated_strong_id_still_ready():
    """强身份（有 automationId）控件不受截断影响：仍 ready。"""
    strong = _click_target()
    strong = replace(
        strong,
        observed_target=replace(
            strong.observed_target, automation_id="strong.btn"))
    case = _compile(strong, [_snapshot_truncated([{
        "automation_id": "strong.btn", "control_type": "Button",
        "text": "B", "ancestry": [],
    }])])
    assert case.status == "ready"
    assert case.steps[0].issues == ()
