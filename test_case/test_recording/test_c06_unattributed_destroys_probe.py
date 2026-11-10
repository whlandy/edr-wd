"""C06 成对探针:无归属 DESTROY 分类(WIP 实测核查,不依赖实现)。

账本 C06:目标事件完整的前提下,加入明确外部窗口销毁;对照已知目标事件
丢失与真正未归属。分类/覆盖分别正确:噪音不污染目标,真正损失不隐藏。
原始计数、未知范围可追溯(设计11 §9 C06/§5.1)。

本文件只做实测探针,先固定真实行为再谈修复。
"""
from __future__ import annotations

import pytest

from target.recording.models import CaptureScope, RawRecording
from target.recording.source import HookPacket
from target.recording.windows import WindowsUIACorrelator

pytestmark = pytest.mark.unit

SCOPE = CaptureScope("target", "windows_pywinauto", "EDRClient.exe", "^logo1$")


class _AppResolver:
    def __init__(self, windows, app_names):
        self._windows = windows
        self._names = app_names

    def find_windows(self, *args, **kwargs):
        return list(self._windows)

    def process_pid_belongs_to_root(self, pid, root):
        return True

    def process_in_scope(self, name, pid=None):
        return True

    def foreground(self):
        return {"windowTitle": "logo1", "processName": "EDRClient.exe",
                "pid": 42, "handle": 13239958}


def _resolver():
    return _AppResolver(
        [{"title": "logo1", "pid": 42, "processName": "EDRClient.exe",
          "handle": 13239958}],
        ["EDRClient.exe"],
    )


def _probe_driver_no_show_destroy():
    """真实 WinEventDriver callback:无 SHOW 记录的外部 DESTROY(环境噪音)。"""
    import ast
    import time as _time
    from pathlib import Path
    from types import SimpleNamespace
    from target.recording import windows as W

    module = ast.parse(Path(W.__file__).read_text(encoding="utf-8"))
    cls = next(n for n in module.body if isinstance(n, ast.ClassDef)
               and n.name == "WindowsWinEventDriver")
    callback_node = next(n for n in ast.walk(cls)
                         if isinstance(n, ast.FunctionDef)
                         and n.name == "callback")
    driver = W.WindowsWinEventDriver("EDRClient.exe")
    packets = []
    driver._emit = packets.append
    env = {"self": driver,
           "user32": SimpleNamespace(GetAncestor=lambda h, f: 9999),
           "HookPacket": HookPacket, "time": _time,
           "_utc_now": lambda: "2026-11-06T00:00:00.000Z"}
    exec(compile(ast.Module(body=[callback_node], type_ignores=[]),
                 "<production-callback>", "exec"), env)
    # 外部进程(如 Explorer 托盘窗口)销毁,driver 从未见过 SHOW:handle=9999。
    env["callback"](None, driver.EVENT_OBJECT_DESTROY, 9999, 0, 0, None, None)
    return driver, packets


def test_c06_pair_a_no_show_destroy_counts():
    """探针A(负例原料):无身份 DESTROY 在 driver 侧的双计数事实。

    已知事实(读 diff):dropped_window_events +1 与
    unattributed_destroy_events +1 同时发生。此处只固定计数器事实,
    不断言语义正确性 —— 语义由 probe B/C 的 coverage 流转验证。"""
    driver, packets = _probe_driver_no_show_destroy()
    assert not packets, "无身份 DESTROY 不得伪造实例/包"
    assert driver.dropped_window_events == 1, (
        f"事实固定:dropped 计数,实际 {driver.dropped_window_events}")
    ua = getattr(driver, "unattributed_destroy_events", "MISSING")
    assert ua == 1, f"事实固定:unattributed 明细计数,实际 {ua}"


def test_c06_pair_b_real_chain_coverage_detail():
    """探针B(核心):真实 source→session 链,外部噪音 DESTROY 后,
    最终 coverage 的 droppedEvents / unattributedDestroys 语义。

    规格(设计11 §5.1):
    - 原始计数不删改;
    - 未归属 ≠ 自动目标丢失,明细须可判读;
    - 不能一层有字段、底层混算/恒空。
    本探针让真实行为说话,断言当前真实值,失败点即真实接线缺口。"""
    from target.recording.session import RecordingSession
    from target.recording.source import CompositeHookDriver, QueuedCaptureSource
    from target.recording.windows import (WindowsLowLevelHookDriver,
                                          WindowsWinEventDriver)

    driver, _ = _probe_driver_no_show_destroy()
    correlator = WindowsUIACorrelator(_resolver())
    source = QueuedCaptureSource(
        scope=SCOPE, sink=lambda event: True,
        driver=CompositeHookDriver(WindowsLowLevelHookDriver(), driver),
        correlator=correlator,
    )
    session = RecordingSession("REC-C06", "review", SCOPE, source=source)
    recording = session.recording()
    coverage = recording.window_timeline["coverage"]

    # 规格断言:噪音 DESTROY 不隐藏 —— 明细必须真实到达最终文档。
    # (若失败:一层有字段、底层恒 0,即 §5.1 禁止的接线缺口。)
    assert coverage.get("unattributedDestroys") == 1, (
        f"C06 缺口:driver 明细计数未流到最终 coverage,实际 {coverage}")
    # droppedEvents 保留旧含义(总流失含未归属),原始计数不删改。
    assert coverage["droppedEvents"] == 1, (
        f"droppedEvents 旧语义应保留(总流失),实际 {coverage}")


def test_c06_pair_c_distinguish_target_loss_from_noise():
    """探针C(正例对照):真正的目标事件丢失(有 SHOW 的窗口 DESTROY 后
    GetAncestor 失败产生的丢包)与外部噪音并存时,两者都可追溯。"""
    import ast
    import time as _time
    from pathlib import Path
    from types import SimpleNamespace
    from target.recording.session import RecordingSession
    from target.recording.source import CompositeHookDriver, QueuedCaptureSource
    from target.recording.windows import (WindowsLowLevelHookDriver,
                                          WindowsUIACorrelator,
                                          WindowsWinEventDriver)

    module = ast.parse(Path(
        __import__("target.recording.windows", fromlist=["W"]).__file__
        if False else __import__("target.recording", fromlist=["windows"]
                                 ).windows.__file__).read_text(encoding="utf-8"))
    cls = next(n for n in module.body if isinstance(n, ast.ClassDef)
               and n.name == "WindowsWinEventDriver")
    callback_node = next(n for n in ast.walk(cls)
                         if isinstance(n, ast.FunctionDef)
                         and n.name == "callback")

    driver = WindowsWinEventDriver("EDRClient.exe")
    driver._emit = lambda packet: None
    driver._titles[555] = ("dialog", "EDRClient.exe", 42)  # 已知目标窗口
    env = {"self": driver,
           "user32": SimpleNamespace(GetAncestor=lambda h, f: 0),
           "HookPacket": HookPacket, "time": _time,
           "_utc_now": lambda: "2026-11-06T00:00:00.000Z"}
    exec(compile(ast.Module(body=[callback_node], type_ignores=[]),
                 "<production-callback>", "exec"), env)
    env["callback"](None, driver.EVENT_OBJECT_DESTROY, 555, 0, 0, None, None)

    # 同一 driver 再叠加一个外部噪音 DESTROY。
    env["callback"](None, driver.EVENT_OBJECT_DESTROY, 9999, 0, 0, None, None)

    assert driver.dropped_window_events == 1, "已存身份 DESTROY 不计丢包"
    ua = getattr(driver, "unattributed_destroy_events", "MISSING")
    assert ua == 1, "外部噪音 DESTROY 应计入 unattributed 明细"
