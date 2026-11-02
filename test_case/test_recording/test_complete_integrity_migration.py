"""完整迁移回归：正向完整性（complete）与 null 严格性。

独立审查结论的 P1/P2 落地测试：

1. [P1] 正向完整性：
   - 编译期：弱身份由单候选进入 `ready` 必须以 `frame.complete is True` 为前提；
     字段缺失 / None / False（截断、children 枚举异常、深度/数量上限）一律默认不完整。
   - 生产端：`pywinauto_client` 只在遍历明确完整时输出 `complete=True`；children()
     枚举异常、深度上限、子控件数量上限都标记不完整。
   - inventory：树缺失 `complete` 或非 True -> 快照 `complete=False`（默认不完整）。
2. [P2] 模型与 schema 严格一致：`truncated` / `complete` 字段 *存在时* 必须是真
   boolean，`None` 必须被模型拒绝（与 JSON Schema `"type":"boolean"` 对齐）。

所有断言走生产入口（compile_recording / pywinauto_client 树 / ControlInventory /
RawRecording 模型），不依赖测试专用旁路。只做纯增量，不改既有用例语义。
"""

from __future__ import annotations

import importlib
import sys
import types

import pytest

from target.recording.inventory import ControlInventory
from target.recording.models import (
    CaptureScope,
    ObservedTarget,
    RawCaptureEvent,
    RawRecording,
    RecordingModelError,
)
from agent.recording.compiler import compile_recording


# --------------------------------------------------------------------------- #
# 编译期正向完整性门（P1 · compiler）
# --------------------------------------------------------------------------- #
_WINDOW = {"title": "logo1", "processName": "EDRClient.exe"}
_ANCESTRY = (
    {"controlType": "Table", "automationId": "table"},
    {"controlType": "Group", "automationId": "base.tablePage"},
    {"controlType": "Custom", "automationId": "base.pageWidget"},
)


def _weak_target(**overrides):
    kw = {
        "automation_id": None,
        "identifier": None,
        "control_type": "DataItem",
        "text": "Details",
        "ancestry": _ANCESTRY,
        "same_name_siblings": 1,
    }
    kw.update(overrides)
    target = ObservedTarget("OBS-1", "T0001", "sha256:x",
                            automation_id=kw["automation_id"],
                            control_type=kw["control_type"],
                            text=kw["text"], ancestry=kw["ancestry"],
                            same_name_siblings=kw["same_name_siblings"])
    return RawCaptureEvent(
        sequence=1, wall_time="2026-10-28T00:00:00Z", monotonic_ms=1,
        type="pointer_click", scope=CaptureScope("t", "windows_pywinauto",
                                                 "EDRClient.exe", "^logo1$"),
        input={"button": "left"}, observed_target=target,
    )

# The recording-side snapshot node shape mirrors inventory.normalize_controls.
def _node():
    return {"automation_id": None, "control_type": "DataItem", "text": "Details",
            "ancestry": [dict(x) for x in _ANCESTRY]}


def _snapshot(*controls, complete=None):
    snap = {"snapshotId": "S1", "capturedAtMs": 0, "reason": "probe",
            "durationMs": 0, "window": dict(_WINDOW),
            "contentHash": "h", "stateHash": "h",
            "controlCount": len(controls), "controls": list(controls)}
    if complete is not None:
        snap["complete"] = complete
    return snap


def _compile(target, snapshots):
    return compile_recording(RawRecording(
        name="t", session_id="s1", events=(target,),
        control_snapshots=tuple(snapshots))).case


def test_missing_complete_weak_identity_is_unresolved_not_ready():
    """字段缺失 -> 默认不完整：唯一弱身份单候选也不能 `ready`。"""
    case = _compile(_weak_target(), [_snapshot(_node())])
    assert case.status == "incomplete"
    assert "compile_weak_identity_unresolved" in case.steps[0].issues


def test_complete_false_weak_identity_is_unresolved_not_ready():
    """complete=False（截断/异常/数量上限）-> 不能证明唯一，不得 ready。"""
    case = _compile(_weak_target(), [_snapshot(_node(), complete=False)])
    assert case.status == "incomplete"
    assert "compile_weak_identity_unresolved" in case.steps[0].issues
    assert "compile_weak_or_ambiguous_identity" not in case.steps[0].issues


def test_complete_true_unique_weak_identity_is_ready():
    """complete=True + 全树唯一 -> 可证明唯一，`ready`（对照）。"""
    case = _compile(_weak_target(), [_snapshot(_node(), complete=True)])
    assert case.status == "ready"
    assert case.steps[0].issues == ()


def test_complete_true_six_rows_still_ambiguous_incomplete():
    """complete=True 但 6 行同链同名 -> 明确歧义 incomplete，不承诺 ready。"""
    case = _compile(_weak_target(), [_snapshot(*([_node()] * 6), complete=True)])
    assert case.status == "incomplete"
    assert "compile_weak_or_ambiguous_identity" in case.steps[0].issues


def test_missing_complete_never_blocks_strong_identity_ready():
    """强身份（automationId）不受完整性门影响：字段缺失仍 ready。"""
    strong = _weak_target(automation_id="strong.btn")
    case = _compile(strong, [_snapshot({**dict(_node()), "automation_id": "strong.btn"})])
    assert case.status == "ready"
    assert case.steps[0].issues == ()


# --------------------------------------------------------------------------- #
# 生产端正向完整性：pywinauto_client（P1 · producer）
# --------------------------------------------------------------------------- #
_WINDOWS_BY_HANDLE: dict = {}


def _register_window(handle: int, root) -> None:
    _WINDOWS_BY_HANDLE[int(handle)] = root


def _install_fake_pywinauto(monkeypatch):
    pkg = types.ModuleType("pywinauto")
    pkg.Application = object
    pkg.timings = types.ModuleType("pywinauto.timings")
    pkg.mouse = types.ModuleType("pywinauto.mouse")

    class _Desktop:
        def __init__(self, backend="uia"):
            self.backend = backend

        def window(self, handle=None):
            return _WINDOWS_BY_HANDLE.get(int(handle))

    pkg.Desktop = _Desktop
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
                 parent=None, children=(), children_raise=False,
                 item_count=None):
        self.element_info = _EI(control_type, automation_id, name)
        self._parent = parent
        self._children = list(children)
        self._children_raise = children_raise
        self._item_count = item_count

    def parent(self):
        return self._parent

    def children(self):
        if self._children_raise:
            raise RuntimeError("enumeration failed")
        return self._children

    def item_count(self):
        if self._item_count is None:
            raise AttributeError("no item count")
        return self._item_count

    def row_count(self):
        return self.item_count()

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


def _rack(children=()):
    root = _Node("Window", "EdrMainWindow", children=[])
    for child in children:
        child._parent = root
    root._children = list(children)
    return root


def test_full_walk_emits_complete_true(monkeypatch):
    module = _install_fake_pywinauto(monkeypatch)
    _register_window(12345, _rack(children=[_Node("Group", "base.pageWidget", children=[])]))
    tree = module.WindowsGUI().dump_tree_for_window(12345, max_depth=10)
    assert tree["complete"] is True
    assert tree["truncated"] is False


def test_children_exception_marks_tree_incomplete(monkeypatch):
    """children() 抛异常的子树 -> 整树 `complete=False`（不能当作完整）。"""
    module = _install_fake_pywinauto(monkeypatch)
    _register_window(12345, _rack(children=[_Node("Group", "base.pageWidget",
                                                  children_raise=True)]))
    tree = module.WindowsGUI().dump_tree_for_window(12345, max_depth=10)
    assert tree["complete"] is False
    assert tree["truncated"] is True


def test_depth_cap_marks_tree_incomplete(monkeypatch):
    module = _install_fake_pywinauto(monkeypatch)
    root = _Node("Window", "EdrMainWindow")
    child = _Node("Group", "g1", parent=root)
    grand = _Node("Group", "g2", parent=child)
    child._children = [grand]
    root._children = [child]
    _register_window(12345, root)
    # depth 0 caps at the root itself -> deeper nodes cannot be walked.
    tree = module.WindowsGUI().dump_tree_for_window(12345, max_depth=0)
    assert tree["complete"] is False
    assert tree["truncated"] is True


def test_child_count_cap_marks_tree_incomplete(monkeypatch):
    module = _install_fake_pywinauto(monkeypatch)
    # 201 children exceeds the 200 cap in _build_tree -> incomplete.
    _register_window(12345, _rack(
        children=[_Node("Button", f"b{i}") for i in range(201)]))
    tree = module.WindowsGUI().dump_tree_for_window(12345, max_depth=10)
    assert tree["complete"] is False
    assert tree["truncated"] is True


def test_single_child_under_cap_emits_complete_true(monkeypatch):
    module = _install_fake_pywinauto(monkeypatch)
    _register_window(12345, _rack(children=[_Node("Button", "b1")]))
    tree = module.WindowsGUI().dump_tree_for_window(12345, max_depth=10)
    assert tree["complete"] is True
    assert tree["truncated"] is False


def test_partially_materialized_virtualized_table_is_incomplete(monkeypatch):
    """P1: 正常返回但仅物化部分行的虚拟化容器 -> `complete=False`。

    `children()` 无异常、无上限，但容器是虚拟化类型且报告 10 行、只物化 1 行；
    producer 不得声称完整，否则弱身份会骗过编译门变成 `ready`。
    """
    module = _install_fake_pywinauto(monkeypatch)
    table = _Node("Table", "scanTable", item_count=10,
                  children=[_Node("DataItem", "", "Details")])
    root = _rack(children=[table])
    table._parent = root
    _register_window(12345, root)
    tree = module.WindowsGUI().dump_tree_for_window(12345, max_depth=10)
    assert tree["complete"] is False
    assert tree["truncated"] is True
    # 编译链：该树（complete=False）里的弱身份 Details 不得 ready。
    snap = _snapshot({**dict(_node()), "ancestry": list(_ANCESTRY)},
                     complete=False)
    case = _compile(_weak_target(), [snap])
    assert case.status == "incomplete"
    assert "compile_weak_identity_unresolved" in case.steps[0].issues


def test_unproven_virtualized_container_is_incomplete(monkeypatch):
    """虚拟化容器拿不到总行数（无法证明物化）-> 默认不完整（unprovable≠complete）。"""
    module = _install_fake_pywinauto(monkeypatch)
    table = _Node("Table", "scanTable", item_count=None,
                  children=[_Node("DataItem", "", "Details")])
    root = _rack(children=[table])
    table._parent = root
    _register_window(12345, root)
    tree = module.WindowsGUI().dump_tree_for_window(12345, max_depth=10)
    assert tree["complete"] is False
    assert tree["truncated"] is True


def _header_plus_rows_table(item_count, row_nodes):
    """A virtualized table whose children include a non-row header node.

    ``children()`` on a real UIA table can also report headers and scrollbars,
    not just rows, so ``len(children)`` is not a proof of materialization.
    """
    return _Node("Table", "scanTable", item_count=item_count,
                 children=[_Node("DataItem", "", "Header")] + list(row_nodes))


def test_header_makes_len_children_equivocate_materialization(monkeypatch):
    """P1 反例：children 含表头导致 len(children)==item_count 也非完整。

    3 总行、仅物化 2 行、children() 还带 1 个表头节点 → len(children)==3==item_count，
    但物化行只有 2，缺了 1 行。旧实现据此误判 complete=True（弱身份骗过编译门变
    `ready`）；虚拟化容器必须保守为 incomplete。
    """
    module = _install_fake_pywinauto(monkeypatch)
    table = _header_plus_rows_table(item_count=3, row_nodes=[
        _Node("DataItem", "", "row1"),
        _Node("DataItem", "", "row2"),
    ])
    root = _rack(children=[table])
    table._parent = root
    _register_window(12345, root)
    tree = module.WindowsGUI().dump_tree_for_window(12345, max_depth=10)
    assert tree["complete"] is False
    assert tree["truncated"] is True
    # 编译链：该树（complete=False）里的弱身份 Details 等待选，不得 ready。
    snap = _snapshot({**dict(_node()), "ancestry": list(_ANCESTRY)},
                     complete=False)
    case = _compile(_weak_target(), [snap])
    assert case.status == "incomplete"
    assert "compile_weak_identity_unresolved" in case.steps[0].issues


def test_concurrent_dumps_do_not_crosstalk_completeness(monkeypatch):
    """P1: `_tree_truncated`/complete 不再共享于实例 —— 两个并发 dump 各报各的。

    两棵不同的树同时走查：A 经 children() 异常截断，B 完整。B 的完整度不得被
    A 的截断污染（旧代码共享实例标志，B 会读到 A 的 True 而误报不完整）。

    同步编排保证确定性：A 的截断节点先置位 `a_raised` 再抛异常；B 的根 children()
    等到 `a_raised` 后再继续，从而 B 必然在读完整度之前 A 已提交截断标记。修复后
    每个 dump 用独立累计器，B 仍报完整；若完整性又共享到实例（旧缺陷变异），B 会
    读到 A 的截断而误报不完整，测试即失败。
    """
    import threading
    import time
    module = _install_fake_pywinauto(monkeypatch)
    gate = threading.Barrier(2)
    a_raised = threading.Event()

    class _SyncRoot(_Node):
        def children(self):
            try:
                gate.wait(timeout=5)
            except Exception:
                pass
            return super().children()

    class _RaisingNode(_Node):
        def children(self):
            a_raised.set()
            raise RuntimeError("enumeration failed")

    class _DelayedRoot(_Node):
        def children(self):
            try:
                gate.wait(timeout=5)
            except Exception:
                pass
            # Wait until A has raised (and therefore committed its truncation),
            # then a touch more so the write is visible to a shared flag.
            a_raised.wait(5)
            time.sleep(0.02)
            return super().children()

    bad = _RaisingNode("Group", "g")
    root_a = _SyncRoot("Window", "EdrMainWindowA", children=[bad])
    bad._parent = root_a
    root_b = _DelayedRoot("Window", "EdrMainWindowB",
                          children=[_Node("Button", "b1")])
    _register_window(11, root_a)
    _register_window(22, root_b)

    gui = module.WindowsGUI()
    results: dict = {}

    def dump(handle, key):
        results[key] = gui.dump_tree_for_window(handle, max_depth=5)

    ta = threading.Thread(target=dump, args=(11, "a"))
    tb = threading.Thread(target=dump, args=(22, "b"))
    ta.start()
    tb.start()
    ta.join(5)
    tb.join(5)
    assert not ta.is_alive() and not tb.is_alive(), "dumps must finish"

    assert results["a"]["complete"] is False   # A truncated -> incomplete
    assert results["a"]["truncated"] is True
    assert results["b"]["complete"] is True    # B full -> complete, no crosstalk
    assert results["b"]["truncated"] is False


# --------------------------------------------------------------------------- #
# inventory：快照传播正向完整性（P1 · propagation）
# --------------------------------------------------------------------------- #
class _Resolver:
    def __init__(self, tree):
        self.tree = tree

    def control_tree(self, handle, max_depth=12):
        return self.tree


def _capture_snapshot(tree):
    """Drive ControlInventory._capture synchronously (deterministic)."""
    resolver = _Resolver(tree)
    inv = ControlInventory(resolver, debounce_ms=0)
    request = {"handle": 7, "title": "logo1", "processName": "EDRClient.exe",
               "reason": "step", "dueMs": 0, "requestedAtMs": 0}
    inv._capture(request)
    return inv.snapshots[0] if inv.snapshots else None


def test_missing_complete_in_tree_defaults_snapshot_incomplete():
    """树缺失 `complete` 字段 -> 快照 `complete=False`（默认不完整）。"""
    snap = _capture_snapshot({"ok": True, "title": "logo1",
                              "controls": [{"automation_id": "btn.apply",
                                            "control_type": "Button",
                                            "text": "Apply"}]})
    assert snap is not None
    assert snap["complete"] is False
    assert snap["truncated"] is False


def test_complete_false_in_tree_propagates_to_snapshot():
    snap = _capture_snapshot({"ok": True, "title": "logo1",
                                  "controls": [{"automation_id": "btn.apply", "control_type": "Button", "text": "Apply"}],
                              "complete": False, "truncated": True})
    assert snap is not None
    assert snap["complete"] is False
    assert snap["truncated"] is True


def test_complete_true_in_tree_propagates_to_snapshot():
    snap = _capture_snapshot({"ok": True, "title": "logo1",
                                  "controls": [{"automation_id": "btn.apply", "control_type": "Button", "text": "Apply"}],
                              "complete": True, "truncated": False})
    assert snap is not None
    assert snap["complete"] is True
    assert snap["truncated"] is False


# --------------------------------------------------------------------------- #
# P2：模型与 schema 严格一致（null 拒绝）
# --------------------------------------------------------------------------- #
def _recording_with(**kwargs) -> RawRecording:
    return RawRecording("s", "n", (), {"droppedPackets": 0,
                                       "correlationErrorCount": 0}, **kwargs)


def test_model_rejects_null_complete_and_truncated():
    base = {"snapshotId": "CS-1", "capturedAtMs": 1, "window": {},
            "contentHash": "sha256:a", "controls": []}
    for bad_value in (None, "false", 0):
        with pytest.raises(RecordingModelError, match="boolean"):
            _recording_with(control_snapshots=({**base, "complete": bad_value},))
        with pytest.raises(RecordingModelError, match="boolean"):
            _recording_with(control_snapshots=({**base, "truncated": bad_value},))


def test_model_accepts_real_bool_complete_and_truncated():
    base = {"snapshotId": "CS-1", "capturedAtMs": 1, "window": {},
            "contentHash": "sha256:a", "controls": []}
    rec = _recording_with(control_snapshots=(
        {**base, "complete": True, "truncated": False},))
    assert rec.control_snapshots[0]["complete"] is True
    assert rec.control_snapshots[0]["truncated"] is False
