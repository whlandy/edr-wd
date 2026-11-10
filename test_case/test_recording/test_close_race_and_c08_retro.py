"""审核意见落实锁定（C08 回溯边界 + 关闭窗口竞态；REC-e619605f/e6435b7c）。

实机证据链（result-report/20261109-0230-rerecord-replay/04-stop-d2-*.json）：
- C08 时序：ttk 停止按钮命令在释放时触发，down/up 包先于 mark 到达 →
  前向分类误计 recorder_ui；mark 落下后回溯重分类（retro 标记防重复）。
- 关闭窗口竞态：hook 时 WindowFromPoint 命中 Log Center（native stamp），
  处理时 UIA hit-test 因窗口已销毁应答桌面（Explorer pid 3456）→ 点击被
  误拒 target_outside_scope，关闭按钮 step 丢失。

锁定：
1. 回溯只洗标记窗口内的条目，且保留原分类追加纠正记录（retro 标记），
   重复 mark 不重复迁移计数（幂等）。
2. hook-time stamp 与处理时 foreground 同窗、而 hit-test 应答异主时，
   stamp 否决 hit-test：点击按 stamp 窗口身份入档（observed=None →
   compile_selector_ambiguous 结构化 gap），绝不静默丢弃。
3. 反例：hit-test 异主但 stamp 指向不同窗口（真正的跨应用点击）→ 照旧
   拒绝；无 native stamp 的 packet 不受否决路径影响。
"""

from __future__ import annotations

import pytest

from target.recording.models import CaptureScope
from target.recording.source import HookPacket
from target.recording.windows import WindowsUIACorrelator

pytestmark = pytest.mark.unit

SCOPE = CaptureScope(backend="windows_pywinauto",
                     process_name="EDRClient.exe",
                     window_title="^EDRClient$", target="target")


class _ClosingDialogResolver:
    """down 时 Log Center 在点下（native stamp），处理时 UIA 应答桌面。"""

    def __init__(self) -> None:
        self.desktop_owner = 3456

    def windows(self):
        return [{"title": "logo1", "pid": 1984,
                 "processName": "EDRClient.exe", "handle": 13239958}]

    def process_in_scope(self, name, pid=None):
        return True

    def process_pid_belongs_to_root(self, pid, root):
        return True

    def application_process_names(self, process_name):
        return ["EDRClient.exe"]

    def foreground(self):
        # 处理时窗口已关：前台是其后画面（logo1 仍在）
        return {"windowTitle": "logo1", "processName": "EDRClient.exe",
                "pid": 1984, "handle": 13239958}

    def window_from_native(self, native):
        if not isinstance(native, dict):
            return None
        return {**native, "processName": "EDRClient.exe"}

    def element_at(self, x, y, pid=None):
        # 处理时 hit-test：Log Center 已销毁 → 桌面（Explorer 属主）
        return {"controlType": "Pane", "text": None, "automationId": "",
                "rect": [0, 0, 1440, 900], "protected": False,
                "ownerPid": self.desktop_owner}

    def control_tree(self, handle, max_depth=12):
        return self.trees.get(handle) if hasattr(self, 'trees') else None


def _close_click_packets():
    # native stamp：hook 时 WindowFromPoint 命中 Log Center (1115424)
    native_window = {"handle": 1115424, "pid": 1984,
                     "windowTitle": "Log Center"}
    down = HookPacket(kind="pointer_down", monotonic_ms=1000,
                      wall_time="2026-11-09T00:00:00.000Z",
                      screen_point=(1248, 167), button="left",
                      native={"window": native_window})
    up = HookPacket(kind="pointer_up", monotonic_ms=1080,
                    wall_time="2026-11-09T00:00:00.000Z",
                    screen_point=(1248, 167), button="left",
                    native={"window": native_window})
    return down, up


def _correlate_close_click(resolver):
    correlator = WindowsUIACorrelator(resolver)
    correlator.seed_scope(SCOPE)
    # The dialog's SHOW was processed before the click (RI declared, handle
    # enters the derived scope) — as in the real capture.
    show = HookPacket(kind="window_transition", monotonic_ms=900,
                      wall_time="2026-11-09T00:00:00.000Z",
                      screen_point=None, button=None,
                      native={"kind": "opened", "nativeKind": "shown",
                              "processName": "EDRClient.exe",
                              "title": "Log Center", "pid": 1984,
                              "handle": 1115424})
    correlator.correlate(show, SCOPE, 0)
    down, up = _close_click_packets()
    # Reality (REC-e619605f): hook-queue latency means BOTH click packets
    # are processed AFTER the destroy transition — the window is already
    # gone when each click's hit test runs. The destroy is processed
    # first; the click packets carry the hook-time stamp of the dead
    # window.
    destroy = HookPacket(kind="window_transition", monotonic_ms=1040,
                         wall_time="2026-11-09T00:00:00.000Z",
                         screen_point=None, button=None,
                         native={"kind": "closed", "nativeKind": "destroyed",
                                 "processName": "EDRClient.exe",
                                 "title": "Log Center", "pid": 1984,
                                 "handle": 1115424})
    correlator.correlate(destroy, SCOPE, 0)
    correlator.correlate(down, SCOPE, 1)
    event = correlator.correlate(up, SCOPE, 2)
    return correlator, event


def test_positive_closing_window_stamp_veto_keeps_the_click():
    """native stamp=Log Center 且 foreground 同窗，hit-test 异主（桌面）→
    stamp 否决 hit-test：点击入档（observed=None 的诚实 gap），不丢弃。"""
    resolver = _ClosingDialogResolver()
    correlator, event = _correlate_close_click(resolver)
    assert event is not None, (
        "关闭窗口竞态不得吞掉关闭按钮点击——hook 时窗口在点上（REC-e619605f "
        "实证 native stamp=Log Center），处理时 hit-test 应答桌面是竞态产物")
    assert event.observed_target is None, (
        "元素树已不可得：诚实记录无目标观测，编译期结构化 gap")


def test_negative_cross_app_click_still_rejected_without_stamp_match():
    """stamp 指向不同窗口（真正的跨应用点击）→ 照旧拒绝；C08/边界不放松。"""
    resolver = _ClosingDialogResolver()
    correlator = WindowsUIACorrelator(resolver)
    correlator.seed_scope(SCOPE)
    # stamp 指向一个非前景的别的窗口：foreground=logo1，stamp=别的窗口
    down = HookPacket(kind="pointer_down", monotonic_ms=1000,
                      wall_time="2026-11-09T00:00:00.000Z",
                      screen_point=(600, 400), button="left",
                      native={"window": {"handle": 999, "pid": 42,
                                         "windowTitle": "Other"}})
    up = HookPacket(kind="pointer_up", monotonic_ms=1080,
                    wall_time="2026-11-09T00:00:00.000Z",
                    screen_point=(600, 400), button="left",
                    native={"window": {"handle": 999, "pid": 42,
                                       "windowTitle": "Other"}})
    correlator.correlate(down, SCOPE, 1)
    event = correlator.correlate(up, SCOPE, 2)
    assert event is None, "异窗 stamp 不得触发否决路径"


# ---- C08 retro: relabel only panel-rect entries, idempotent ----------------


class _PanelRectResolver(_ClosingDialogResolver):
    """带 recorder UI rect 的桩：panel 内外各一个 recorder_ui 拒绝。"""

    def element_at(self, x, y, pid=None):
        return {"controlType": "Pane", "text": None, "automationId": "",
                "rect": [0, 0, 100, 100], "protected": False,
                "ownerPid": self.desktop_owner}


def test_c08_retro_relabels_and_is_idempotent():
    """回溯：窗口内 recorder_ui → recorder_control（保留原条目加 retro 标）；
    重复 mark 不重复迁移；panel 外的真遮挡不洗。"""
    resolver = _PanelRectResolver()
    correlator = WindowsUIACorrelator(resolver)
    correlator.set_recorder_ui_rect((1300, 600, 1440, 720))
    correlator.seed_scope(SCOPE)
    # 一次落在 panel 内的 down+up（真停止手势，先误计 ui）
    in_panel_down = HookPacket(kind="pointer_down", monotonic_ms=1000,
                               wall_time="2026-11-09T00:00:00.000Z",
                               screen_point=(1367, 661), button="left",
                               native={})
    in_panel_up = HookPacket(kind="pointer_up", monotonic_ms=1080,
                             wall_time="2026-11-09T00:00:00.000Z",
                             screen_point=(1367, 661), button="left",
                             native={})
    correlator.correlate(in_panel_down, SCOPE, 1)
    correlator.correlate(in_panel_up, SCOPE, 2)
    assert correlator.recorder_ui_events == 2
    assert correlator.recorder_control_events == 0
    # mark 落下（stop 回调）→ 回溯重分类
    correlator.mark_recorder_control()
    assert correlator.recorder_ui_events == 0, (
        "停止手势的 down/up 必须回溯改标")
    assert correlator.recorder_control_events == 2
    relabeled = [r for r in correlator.rejections
                 if r.get("reason") == "recorder_control"]
    assert all(r.get("retro") is True for r in relabeled), (
        "纠正必须追加标记，不覆盖历史条目")
    # 幂等：重复 mark 不再迁移
    correlator.mark_recorder_control()
    assert correlator.recorder_control_events == 2
    assert correlator.recorder_ui_events == 0
