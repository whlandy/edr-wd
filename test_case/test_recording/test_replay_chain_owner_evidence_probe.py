"""fix-20261109-0340（用户会话实机 REC-996dda21 两缺陷）。

实机原件：result-report/20261109-0230-rerecord-replay/
（04-stop-recording-c06.json + live-c06/ 编译产物）。

B2（step-0006 归因矛盾）：弹窗（Quarantine & Blocking Area，RI-0005）
关闭瞬间点击 logo1 内容——evidence.window 戳 {title: QBA, handle: H,
instanceId: RI-0005}（stamp 时该实例快照未到，无 root），而
observed_target 控件链根是 EdrMainWindow（logo1）。编译侧
window_for_event 曾用 `or window_root_identity(observed_target)` 把
hit-test 链根回填进 selector window → 拼出从未共存的组合
{root: EdrMainWindow, titleRegex: QBA}（采集侧探针 d2-window-stamp 已
钉死"绝不回退 hit-test 控件链"，编译侧这个 `or` 违反了它）→
_frame_window_matches/_window_for 与 RI-0005 自身快照根
（QuarantineForbiddenAreaWindow）矛盾 → compile_window_source_unresolved，
整份 windowSequence 拒发。

修复：window_for_event 的 root 只取同源 stamp 字段；无 root 时
title-only，编译侧 snapshot-root promotion（compiler _window_for 已有）
从同实例快照恢复。锁定：
- 正例 A（实机形状）：QBA stamp 无 root + logo1 控件链 → selector 窗口
  无 rootAutomationId，title 保留。
- 正例 B（正常路径不误伤）：stamp 自带 root（同 handle 快照已到）→
  selector 窗口 root 照常携带，conjunctive titleRegex 不变。
- 反例 C（旧行为锁死）：`or` 回退不得复活——OT ancestry 的
  EdrMainWindow 永不出现在仅 stamp 无 root 事件的 selector 窗口里。

B1（target_outside_scope 拒绝无归属证据）：rejection 记录缺
ownerPid/ownerProcess，两笔 logo1 区点击被拒后无法区分"兄弟进程覆盖窗"
与"UIA 瞬态"（REC-996dda21 rejections[0..1]）。修复：_note_rejection 在
hit-test 元素带 ownerPid 时写入 rejection 条目（纯证据，行为不变）。
锁定：
- 正例 D：ownerPid=43（兄弟进程）的拒绝条目含 ownerPid=43。
- 反例 E：无 ownerPid 时条目不含该键（不伪造）。
"""

from __future__ import annotations

import pytest

from agent.recording.selectors import window_for_event
from target.recording.models import (
    CaptureScope,
    ObservedTarget,
    RawCaptureEvent,
)
from target.recording.source import HookPacket
from target.recording.windows import WindowsUIACorrelator, WindowsUIAResolver

pytestmark = pytest.mark.unit

QBA_HANDLE = 16320674
SCOPE = CaptureScope(backend="windows_pywinauto",
                     process_name="EDRClient.exe",
                     window_title="^EDRClient$", target="target")


def _closing_dialog_click_event() -> RawCaptureEvent:
    """REC-996dda21 seq9 形状：stamp 无 root + hit-test 链根 logo1。"""
    return RawCaptureEvent(
        sequence=9, wall_time="2026-11-08T19:26:02.491Z",
        monotonic_ms=1374562109, type="pointer_click", scope=SCOPE,
        input={"button": "left", "clickCount": 1,
               "screenPoint": [1128, 107]},
        observed_target=ObservedTarget(
            snapshot_id="OBS-b8ad7221", target_id="T0001",
            fingerprint="sha256:94e4a3f9",
            control_type="Group",
            automation_id="EdrMainWindow.baseWidget.pageWidget.mainPage"
                          ".protectTimeWidget",
            identifier=None, text="", rect=(322, 91, 1242, 209),
            ancestry=({"controlType": "Group",
                       "automationId": "EdrMainWindow.baseWidget.pageWidget"
                                       ".mainPage"},
                      {"controlType": "Window",
                       "automationId": "EdrMainWindow", "name": "logo1"},
                      {"controlType": "Pane", "name": "Desktop 1"}),
            protected=False, same_name_siblings=None),
        evidence={"window": {"title": "Quarantine & Blocking Area",
                             "processName": "EDRClient.exe",
                             "handle": QBA_HANDLE,
                             "instanceId": "RI-0005"}},
        causal_id="CAUSE-test",
    )


def test_positive_a_closing_dialog_stamp_without_root_keeps_title_only():
    """stamp 无 root 时 selector 窗口不得混入 hit-test 链根。"""
    window = window_for_event(_closing_dialog_click_event())
    assert window["processName"] == "EDRClient.exe"
    assert "rootAutomationId" not in window, (
        "hit-test 链根 EdrMainWindow 不得进入 QBA stamp 事件的 selector 窗口"
        "——两者从未共存（REC-996dda21 step-0006 形状）")
    assert window["titleRegex"] == "^Quarantine\\ \\&\\ Blocking\\ Area$", (
        "stamp title 保留：编译侧 promotion 从 RI-0005 自身快照恢复 root")


def test_positive_b_stamp_with_root_unchanged():
    """stamp 自带 root（同 handle 快照先于事件到达）→ 行为不变。"""

    event = _closing_dialog_click_event()
    event.evidence["window"]["rootAutomationId"] = (
        "QuarantineForbiddenAreaWindow")
    window = window_for_event(event)
    assert window["rootAutomationId"] == "QuarantineForbiddenAreaWindow"
    assert window["titleRegex"] == "^Quarantine\\ \\&\\ Blocking\\ Area$", (
        "同源 root+title conjunctive 绑定保持不变")


def test_negative_c_ancestry_root_fallback_is_gone():
    """`or window_root_identity(observed_target)` 回退被移除：控件链根
    （EdrMainWindow）在任何 stamp-无-root 事件里都不再出现。"""
    event = _closing_dialog_click_event()
    window = window_for_event(event)
    assert window.get("rootAutomationId") != "EdrMainWindow"


# ---- B1: rejection carries hit-test owner evidence ------------------------


class _OverlayResolver:
    """foreground=logo1(42)，hit-test 命中兄弟进程(43)拥有的控件。"""

    def __init__(self, element: dict) -> None:
        self._element = element

    def windows(self):
        return [{"title": "logo1", "pid": 42,
                 "processName": "EDRClient.exe", "handle": 13239958}]

    def process_in_scope(self, name, pid=None):
        return True

    def process_pid_belongs_to_root(self, pid, root):
        return True

    def application_process_names(self, process_name):
        return ["EDRClient.exe"]

    def foreground(self):
        return {"windowTitle": "logo1", "processName": "EDRClient.exe",
                "pid": 42, "handle": 13239958}

    def window_at(self, x, y):
        return self.foreground()

    def window_from_native(self, native):
        if not isinstance(native, dict):
            return None
        return {**native, "processName": "EDRClient.exe"}

    def element_at(self, x, y, pid=None):
        return dict(self._element)


def _click_packet(kind: str, ms: int) -> HookPacket:
    native = {"window": {"handle": 13239958, "pid": 42,
                         "windowTitle": "logo1"}}
    return HookPacket(kind=kind, monotonic_ms=ms,
                      wall_time="2026-11-08T19:26:02.000Z",
                      screen_point=(430, 494), button="left",
                      native=native)


def _correlate_overlay(element: dict):
    correlator = WindowsUIACorrelator(_OverlayResolver(element))
    correlator.seed_scope(SCOPE)
    correlator.correlate(_click_packet("pointer_down", 100), SCOPE, 1)
    event = correlator.correlate(_click_packet("pointer_up", 110), SCOPE, 2)
    return correlator, event


def test_positive_d_rejection_records_hit_test_owner_pid():
    """ownerPid=43 的兄弟进程控件被拒时，rejection 记录 ownerPid=43。"""
    correlator, event = _correlate_overlay(
        {"controlType": "Group", "text": None, "automationId": "overlay",
         "rect": [0, 0, 100, 100], "protected": False, "ownerPid": 43})
    assert event is None
    entries = [r for r in correlator.rejections
               if r.get("reason") == "target_outside_scope"]
    assert entries, "兄弟进程覆盖控件必须被拒并记录"
    assert entries[-1].get("ownerPid") == 43, (
        "拒绝条目必须携带 hit-test 元素自身 owner——区分兄弟进程覆盖窗与"
        "UIA 瞬态的唯一证据（REC-996dda21 两笔 logo1 区点击 owner 永失）")


def test_negative_e_no_owner_evidence_no_invented_key():
    """元素缺 ownerPid（生产 resolver fail-closed 路径）→ 条目不得伪造
    owner 键。生产 resolver 缺 ownerPid 视为归属证据失败（拒绝）。"""

    class _NoOwnerResolver(_OverlayResolver, WindowsUIAResolver):
        # MRO: stub 方法在前，production 类仅提供 isinstance 判定（fail-closed
        # ownerPid 分支）。反向继承会让 WindowsUIAResolver.windows 遮蔽桩的
        # windows()，seed 失败 → 走不到 target_outside_scope。

        def process_pid_belongs_to_root(self, pid, root):
            return True  # macOS 测试桩：归属根判定不因 psutil 缺进程而失败

        def process_belongs_to_root(self, process_name, root):
            return True

    correlator = WindowsUIACorrelator(_NoOwnerResolver(
        {"controlType": "Group", "text": None, "automationId": "mystery",
         "rect": [0, 0, 100, 100], "protected": False}))
    correlator.seed_scope(SCOPE)
    correlator.correlate(_click_packet("pointer_down", 100), SCOPE, 1)
    event = correlator.correlate(_click_packet("pointer_up", 110), SCOPE, 2)
    assert event is None
    entries = [r for r in correlator.rejections
               if r.get("reason") == "target_outside_scope"]
    assert entries
    assert "ownerPid" not in entries[-1], (
        "无归属证据时不得写入 ownerPid（不伪造证据）")
