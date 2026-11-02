"""多行同名弱身份目标：编译期诚实 incomplete + 录制端兄弟计数捕获回归。

目标（审核 P1#1/P1#2 修正后的契约）：

- 编译器对「无 automationId/identifier」的弱身份 selector，无法仅凭录制端
  直接兄弟计数证明全窗口唯一（UIA 虚拟化可能未加载全部行；两个不同父节点
  可能序列化为相同 ancestry），因此必须以**完整观测树**（control_snapshots）
  按回放定位器同规则统计 selector 候选数：
  - 全树候选 > 1  -> `compile_weak_or_ambiguous_identity` + `incomplete`；
  - 全树候选 == 1 -> 可证明唯一，`ready`；
  - 无可枚举全树  -> `compile_weak_identity_unresolved` + `incomplete`
    （不能假定 `ready`）。
- 强 ID（automationId/identifier）拥有身份，无论候选多少都 `ready`。
- **按最终合成的 selector 判定，而非命中测试的那一行**：`scroll_commit` 提升
  到稳定 Table container（强 automationId）后不再被弱身份门误拦截。
- 录制端 `_same_name_sibling_count` 仍是弱身份时的直接兄弟信号，但不再是
  编译器唯一性判定的权威。
"""

from __future__ import annotations

import importlib
import sys
import types

import pytest

from agent.recording.compiler import compile_recording
from target.recording.inventory import normalize_controls
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


def _click_target(**overrides):
    kw = {
        "automation_id": None,
        "identifier": None,
        "control_type": "DataItem",
        "text": "Details",
        "ancestry": _ANCHOR_CHAIN,
        "same_name_siblings": 1,
    }
    kw.update(overrides)
    target = ObservedTarget("OBS-1", "T0001", "sha256:x",
                            automation_id=kw["automation_id"],
                            control_type=kw["control_type"],
                            text=kw["text"], ancestry=kw["ancestry"],
                            same_name_siblings=kw["same_name_siblings"])
    return RawCaptureEvent(
        sequence=1, wall_time="2026-08-18T00:00:00Z", monotonic_ms=1,
        type="pointer_click", scope=CaptureScope("t", "windows_pywinauto",
                                                 "EDRClient.exe", "^logo1$"),
        input={"button": "left"}, observed_target=target,
    )


def _compile(target, snapshots=()):
    return compile_recording(RawRecording(name="t", session_id="s1",
                                          events=(target,),
                                          control_snapshots=tuple(snapshots))).case


def _snapshot(*controls):
    return {"snapshotId": "S1", "capturedAtMs": 0, "reason": "probe",
            "durationMs": 0, "window": {"title": "logo1", "processName": "EDRClient.exe"},
            "contentHash": "h", "stateHash": "h", "controlCount": len(controls),
            "complete": True, "controls": list(controls)}


def _dat_item():
    return {"automation_id": None, "control_type": "DataItem", "text": "Details",
            "ancestry": list(_ANCHOR_CHAIN)}


# --------------------------------------------------------------------------- #
# 编译期诚实 incomplete
# --------------------------------------------------------------------------- #

def test_same_name_rows_weak_identity_compile_incomplete():
    """6 个同链同名 Details：无稳定 disambiguator，必须 honest `incomplete`，
    绝不承诺 `ready`（否则回放必然 `replay_target_ambiguous`）。"""
    case = _compile(_click_target(), snapshots=[_snapshot(*([_dat_item()] * 6))])
    assert case.status == "incomplete"
    assert case.steps[0].status == "incomplete"
    assert "compile_weak_or_ambiguous_identity" in case.steps[0].issues


def test_unique_weak_identity_in_full_tree_stays_ready():
    """全观测树中仅 1 个匹配候选的弱身份目标：可证明唯一，保持 `ready`。"""
    case = _compile(_click_target(same_name_siblings=1),
                    snapshots=[_snapshot(_dat_item())])
    assert case.status == "ready"
    assert case.steps[0].issues == ()


def test_two_parents_identical_ancestry_each_sibling_one_is_ambiguous():
    """审核 P1#2：两个不同父节点、相同序列化 ancestry、各自兄弟=1，
    但全树有 2 个匹配候选 -> 必须 `incomplete`（不能只信直接兄弟计数）。"""
    case = _compile(_click_target(same_name_siblings=1),
                    snapshots=[_snapshot(_dat_item(), dict(_dat_item()))])
    assert case.status == "incomplete"
    assert case.steps[0].status == "incomplete"
    assert "compile_weak_or_ambiguous_identity" in case.steps[0].issues


def test_no_control_tree_weak_identity_is_unresolved_not_ready():
    """审核 P1#2：无法对完整观测树枚举时，弱身份目标应 `incomplete`
    （compile_weak_identity_unresolved），而不是假定 `ready`。"""
    case = _compile(_click_target(same_name_siblings=None))
    assert case.status == "incomplete"
    assert case.steps[0].status == "incomplete"
    assert "compile_weak_identity_unresolved" in case.steps[0].issues


def test_unknown_sibling_count_but_unique_in_tree_is_ready():
    """兄弟计数未知（None）不再决定成败：若全树证明唯一，仍 `ready`。"""
    case = _compile(_click_target(same_name_siblings=None),
                    snapshots=[_snapshot(_dat_item())])
    assert case.status == "ready"
    assert case.steps[0].issues == ()


def test_strong_id_many_siblings_stays_ready():
    """强 ID（automationId）拥有身份：兄弟再多也 `ready`，不因同名拦截。"""
    case = _compile(_click_target(automation_id="btnApply", same_name_siblings=6))
    assert case.status == "ready"


def test_scroll_commit_elevated_to_strong_table_is_not_ambiguous():
    """审核 P1#1：scroll_commit 把弱身份行提升为稳定 Table selector
    （automationId 强身份）后，不得再被 compile_weak_or_ambiguous_identity
    误拦截。"""
    target = _click_target(same_name_siblings=6).observed_target
    scroll_ev = RawCaptureEvent(
        sequence=1, wall_time="2026-08-18T00:00:00Z", monotonic_ms=1,
        type="scroll_commit", scope=CaptureScope("t", "windows_pywinauto",
                                                 "EDRClient.exe", "^logo1$"),
        input={"wheelDelta": -120, "screenPoint": [100, 100]},
        observed_target=target,
    )
    case = _compile(scroll_ev, snapshots=[_snapshot(*([_dat_item()] * 6))])
    # selector is the strong-ID Table container; only the verifier-required
    # scroll contract may keep it incomplete, never the weak-identity gate.
    assert "compile_weak_or_ambiguous_identity" not in case.steps[0].issues
    assert case.steps[0].selector.control["automationId"] == "table"


# --------------------------------------------------------------------------- #
# 稀疏树低估守卫：录制端直接兄弟计数 vs 快照树候选数
# --------------------------------------------------------------------------- #
def test_click_direct_siblings_but_sparse_tree_is_not_ready():
    """录制端实时观察到 6 个同名兄弟（same_name_siblings=6），但后台去抖快照
    树只捕获到 1 个候选（虚拟化表格未加载全部行）。此前该情形被稀疏树判为
    `ready`，回放时全量观测出现 6 个候选 -> ambiguous。守卫必须拒绝 `ready`，
    避免 11.26 实测的「编译 ready、回放歧义」在编译期重现。"""
    case = _compile(_click_target(same_name_siblings=6),
                    snapshots=[_snapshot(_dat_item())])
    assert case.status == "incomplete"
    assert case.steps[0].status == "incomplete"
    assert "compile_weak_identity_tree_undercount" in case.steps[0].issues


def test_sparse_tree_single_sibling_stays_ready():
    """快照树与直接兄弟计数一致（均 1）时，不触发低估守卫，仍可证明唯一 ready。"""
    case = _compile(_click_target(same_name_siblings=1),
                    snapshots=[_snapshot(_dat_item())])
    assert case.status == "ready"
    assert case.steps[0].issues == ()


def test_unknown_sibling_count_and_sparse_tree_unique_stays_ready():
    """直接兄弟计数未知（None）且快照树唯一：无矛盾信号，不足以下拒绝结论，仍 ready。"""
    case = _compile(_click_target(same_name_siblings=None),
                    snapshots=[_snapshot(_dat_item())])
    assert case.status == "ready"
    assert case.steps[0].issues == ()


def test_failing_mutation_removes_guard():
    """变异守卫：撤掉 same_name_siblings>1 与树候选数的核对后，该情形会退回
    `ready`（假唯一），证明本回归确实 pin 住守卫，非假绿。这里直接构造出
    `compile_weak_identity_tree_undercount` 必须出现才通过。"""
    case = _compile(_click_target(same_name_siblings=3),
                    snapshots=[_snapshot(dict(_dat_item()))])
    assert case.status == "incomplete"
    assert "compile_weak_identity_tree_undercount" in case.steps[0].issues


# --------------------------------------------------------------------------- #
# 录制端兄弟计数捕获
# --------------------------------------------------------------------------- #
class _EI:
    def __init__(self, control_type="", name=""):
        self.control_type = control_type
        self.automation_id = None
        self.name = name
        self.is_password = False


class _Wrapper:
    def __init__(self, ctype, name, parent=None, children=()):
        self.element_info = _EI(ctype, name)
        self._parent = parent
        self._children = list(children)

    def parent(self):
        return self._parent

    def children(self):
        return self._children


def _install_windows_module(monkeypatch):
    monkeypatch.delitem(sys.modules, "target.recording.windows", raising=False)
    return importlib.import_module("target.recording.windows")


def test_sibling_count_tallies_same_type_and_text(monkeypatch):
    """_same_name_sibling_count 计数同父下同 (control_type, text) 的兄弟（含自身）。"""
    mod = _install_windows_module(monkeypatch)
    parent = _Wrapper("Table", "")
    rows = [_Wrapper("DataItem", "Details", parent=parent) for _ in range(6)]
    # 混入一个不同 text 的兄弟，不应计数
    rows.append(_Wrapper("DataItem", "Other", parent=parent))
    parent._children = rows
    n = mod._same_name_sibling_count(
        {"_nativeElement": rows[0], "controlType": "DataItem", "text": "Details"})
    assert n == 6


def test_sibling_count_unknown_without_native_element(monkeypatch):
    """无 `_nativeElement`（强身份或离线路径）返回 None，不臆造。"""
    mod = _install_windows_module(monkeypatch)
    assert mod._same_name_sibling_count(
        {"controlType": "DataItem", "text": "Details"}) is None


def test_observed_populates_same_name_siblings_only_for_weak_identity(monkeypatch):
    """_observed：弱身份控件携带兄弟计数；强身份控件不带（None）。"""
    mod = _install_windows_module(monkeypatch)
    parent = _Wrapper("Table", "")
    rows = [_Wrapper("DataItem", "Details", parent=parent) for _ in range(5)]
    parent._children = rows
    weak = mod.WindowsUIACorrelator._observed({
        "controlType": "DataItem", "automationId": None, "identifier": None,
        "text": "Details", "ancestry": [], "rect": [0, 0, 10, 10],
        "_nativeElement": rows[0]})
    assert weak.same_name_siblings == 5

    strong = mod.WindowsUIACorrelator._observed({
        "controlType": "Button", "automationId": "btnApply", "identifier": None,
        "text": "应用", "ancestry": [], "rect": [0, 0, 10, 10],
        "_nativeElement": rows[0]})
    assert strong.same_name_siblings is None


# --------------------------------------------------------------------------- #
# 生产路径回归：normalize_controls 保留 ancestry（诚实门按完整观测树判定）
#
# 既有测试用「手工构造、带 ancestry」的 snapshot 控件，未走生产 normalizer。
# 而生产录制树的 snapshot controls 由 inventory.normalize_controls 投影：
# 若该投影剥掉 ancestry，诚实门按「完整观测树 + 回放同规则」统计候选时，
# 每个弱身份 selector（必带 ancestry）都匹配到 0 个候选，把**唯一**的弱身份
# 行也误判成 compile_weak_identity_unresolved/incomplete（假过度拦截）。
# 这些回归用生产 normalizer 忠实地复现该路径。
# --------------------------------------------------------------------------- #
def _raw_tree_control(ancestry=_ANCHOR_CHAIN):
    """Windows _build_tree 输出的一个 snake_case 控件节点（含 ancestry）。"""
    return {
        "control_type": "DataItem", "text": "Details", "automation_id": None,
        "class_name": "", "control_id": None,
        "rectangle": {"x": 0, "y": 0, "w": 100, "h": 30},
        "is_visible": True, "is_enabled": True, "depth": 3,
        "is_password": False, "checked": None, "ancestry": list(ancestry),
    }


def _production_snapshot(nrows):
    """把原始树走 normalize_controls（生产投影），构造真实形状的 snapshot。"""
    raw = [dict(_raw_tree_control()) for _ in range(nrows)]
    return {"snapshotId": "S-PROD", "capturedAtMs": 0, "reason": "step",
            "durationMs": 0, "window": {"title": "logo1",
                                        "processName": "EDRClient.exe"},
            "contentHash": "h", "stateHash": "h", "controlCount": nrows,
            "complete": True, "controls": normalize_controls(raw)}


def test_normalize_controls_preserves_non_empty_ancestry():
    """生产投影必须保留祖先链（否则诚实门看不到它，弱身份必被误判 unresolved）。"""
    controls = normalize_controls([dict(_raw_tree_control())])
    assert controls and controls[0]["ancestry"] == [dict(x) for x in _ANCHOR_CHAIN]


def test_normalize_controls_drops_empty_ancestry():
    """空祖先链不产生信号：投影后不出现 ancestry 键（等价于无链）。"""
    controls = normalize_controls([dict(_raw_tree_control(ancestry=[]))])
    assert controls and "ancestry" not in controls[0]


def test_unique_weak_identity_via_production_normalizer_is_ready():
    """生产路径下唯一弱身份行必须 `ready`（此前因投影剥 ancestry 被误判 unresolved）。"""
    case = _compile(_click_target(), snapshots=[_production_snapshot(1)])
    assert case.status == "ready"
    assert case.steps[0].issues == ()


def test_ambiguous_rows_via_production_normalizer_honest_incomplete():
    """生产路径下 6 行同名弱身份必须 honest `compile_weak_or_ambiguous_identity`
    + incomplete（而不是误报 unresolved），绝不承诺 ready。"""
    case = _compile(_click_target(), snapshots=[_production_snapshot(6)])
    assert case.status == "incomplete"
    assert case.steps[0].status == "incomplete"
    assert "compile_weak_or_ambiguous_identity" in case.steps[0].issues
    assert "compile_weak_identity_unresolved" not in case.steps[0].issues
