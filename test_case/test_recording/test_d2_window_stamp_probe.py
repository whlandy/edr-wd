"""D2-0335 (design 11 §6.3, live-REC-ccd6612): evidence.window 跨窗口拼接。

实机原件实测（11.26, REC-ccd6612eb1fd4ba4845170c707565241）：
- seq8 点击主窗口 Trusted List 图标：pointer 解析窗口=主窗口(h13239958)，
  hit-test 控件链 root=TrustListWindow（该窗口 16ms 后才 created —— UIA
  from_point 命中了预创建窗口的控件链）。_with_window 把 OT root 写进
  window stamp，与 handle/title 拼出从未共存的组合
  {handle:主窗口, root:TrustListWindow, title:logo1}。
- 编译侧 window_for_event 信任该 stamp → selector 窗口身份漂移 →
  same-selector drift 组内 handle 天然互异 → 7/12 步恒
  compile_window_identity_ambiguous。

探针 A（采集侧，本文件）：evidence.window.rootAutomationId 必须与
handle 同源 —— 事件所在窗口的 root 来自**同 handle 的已收快照**
（inventory walk），不是 hit-test 控件链；无快照时省略（编译侧
snapshot-root promotion 从同实例快照恢复，天然同源）。
OT root 与同 handle 快照 root 不一致 → 快照 root 获胜（窗口证据以
窗口解析为准）；无任何快照 → 不写 root，title 兜底。

探针 B（编译侧，test_d2_window_stamp_gate_probe.py）：
coverage 门扣除 unattributedDestroys 明细后才是真丢包。
"""

from __future__ import annotations

import threading

import pytest

from target.recording.source import HookPacket
from target.recording.windows import WindowsUIACorrelator
from target.recording.session import CaptureScope

pytestmark = pytest.mark.unit

MAIN_HANDLE = 13239958
TRUST_HANDLE = 7669730
SCOPE = CaptureScope(backend="windows_pywinauto", process_name="EDRClient.exe",
                     window_title="^EDRClient$", target="target")


class _StampResolver:
    """seq8 形状：pointer 落在主窗口，UIA from_point 命中预创建
    Trusted List 的控件链（OT root=TrustListWindow）。"""

    def __init__(self) -> None:
        self.trees: dict[int, dict] = {}

    def windows(self):
        return [{"title": "logo1", "pid": 42,
                 "processName": "EDRClient.exe", "handle": MAIN_HANDLE}]

    def process_pid_belongs_to_root(self, pid, root):
        return True

    def process_in_scope(self, name, pid=None):
        return True

    def foreground(self):
        return {"windowTitle": "logo1", "processName": "EDRClient.exe",
                "pid": 42, "handle": MAIN_HANDLE}

    def window_from_native(self, native):
        if not isinstance(native, dict):
            return None
        return {**native, "processName": "EDRClient.exe"}

    def element_at(self, x, y, pid=None):
        # 预创建窗口控件链：root 前缀是 TrustListWindow
        return {"controlType": "Group", "text": None,
                "automationId": "TrustListWindow.mainWidget.table",
                "ownerPid": pid,
                "rect": [10, 10, 200, 200],
                "ancestry": [{"control_type": "Window",
                              "automation_id": "TrustListWindow",
                              "name": "Trusted List"}]}

    def control_tree(self, handle, max_depth=12):
        return self.trees.get(handle)


class _StampInventory:
    """可控快照：预设同 handle 的 root 供 _with_window 对照。"""

    def __init__(self, roots_by_handle: dict[int, str]) -> None:
        self._roots = roots_by_handle
        self.instance_authority = None
        self._snapshots = [
            {"snapshotId": f"CS-{i:04d}", "capturedAtMs": 1,
             "reason": "seed", "window": {
                 "handle": h, "title": "logo1",
                 "processName": "EDRClient.exe",
                 "rootAutomationId": r}}
            for i, (h, r) in enumerate(sorted(roots_by_handle.items()), 1)
        ]

    def request(self, **kwargs):
        pass

    def snapshots(self):
        return tuple(self._snapshots)


def _click_packets(ms):
    native_window = {"handle": MAIN_HANDLE, "pid": 42,
                     "windowTitle": "logo1"}
    down = HookPacket(
        kind="pointer_down", monotonic_ms=ms,
        wall_time="2026-11-06T00:00:00.000Z",
        screen_point=(60, 45), button="left",
        native={"window": native_window},
    )
    up = HookPacket(
        kind="pointer_up", monotonic_ms=ms + 10,
        wall_time="2026-11-06T00:00:00.000Z",
        screen_point=(60, 45), button="left",
        native={"window": native_window},
    )
    return down, up


def _window_stamp(correlator, resolver, inventory):
    down, up = _click_packets(400)
    correlator.correlate(down, SCOPE, 1)
    event = correlator.correlate(up, SCOPE, 2)
    assert event is not None and event.type == "pointer_click"
    window = event.evidence.get("window")
    assert isinstance(window, dict)
    return window


def test_probe_a_window_root_matches_input_window_snapshot_not_hit_test_chain():
    """OT root(TrustListWindow) ≠ 同 handle 快照 root(EdrMainWindow)：
    stamp 必须携带**输入窗口**的 root —— 绝不写 hit-test 控件链的。"""
    resolver = _StampResolver()
    inventory = _StampInventory({MAIN_HANDLE: "EdrMainWindow"})
    correlator = WindowsUIACorrelator(resolver, inventory=inventory)
    correlator.seed_scope(SCOPE)
    window = _window_stamp(correlator, resolver, inventory)
    assert window["handle"] == MAIN_HANDLE
    assert window["rootAutomationId"] == "EdrMainWindow", (
        "evidence.window.root 来自同 handle 快照（输入窗口），"
        "不得采用 hit-test 控件链的 TrustListWindow")


def test_probe_a_window_root_omitted_without_snapshot_evidence():
    """无同 handle 快照时：没有同源证据，不写 root（编译侧 promotion
    从后续快照恢复），title 仍在 —— 绝不回退到 hit-test 控件链。"""
    resolver = _StampResolver()
    inventory = _StampInventory({})  # 无任何快照
    correlator = WindowsUIACorrelator(resolver, inventory=inventory)
    correlator.seed_scope(SCOPE)
    window = _window_stamp(correlator, resolver, inventory)
    assert window["handle"] == MAIN_HANDLE
    assert "rootAutomationId" not in window, (
        "无同源快照证据时省略 root；OT 的 TrustListWindow 不得进入 stamp")
    assert window["title"] == "logo1"


def test_probe_a_window_root_snapshot_agreement_still_stamped():
    """OT root == 同 handle 快照 root（正常主窗口点击）：stamp 照常带
    root —— 一致性路径不被修复误伤。"""
    resolver = _StampResolver()

    class _AgreeResolver(_StampResolver):
        def element_at(self, x, y, pid=None):
            return {"controlType": "Button", "text": "Log Center",
                    "automationId":
                        "EdrMainWindow.baseWidget.pageWidget.mainPage"
                        ".contentWidget.mainWidget.operationWidget"
                        ".logCenterBtn",
                    "ownerPid": pid,
                    "rect": [10, 10, 100, 40],
                    "ancestry": [{"control_type": "Window",
                                  "automation_id": "EdrMainWindow",
                                  "name": "logo1"}]}

    resolver = _AgreeResolver()
    inventory = _StampInventory({MAIN_HANDLE: "EdrMainWindow"})
    correlator = WindowsUIACorrelator(resolver, inventory=inventory)
    correlator.seed_scope(SCOPE)
    window = _window_stamp(correlator, resolver, inventory)
    assert window["rootAutomationId"] == "EdrMainWindow"
