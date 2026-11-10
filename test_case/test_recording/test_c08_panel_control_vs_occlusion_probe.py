"""C08 探针:面板控制输入 vs 面板遮挡目标输入(设计11 §9 C08/§5.1)。

账本 C08:start/stop 控制输入;对照面板遮住目标时点击;
控制事件不混业务;真实遮挡有诊断并有修复;
必须保持:不把遮挡输入写成目标动作。

§5.1:正常录制控制不无条件污染整份业务录制;控制输入与遮挡目标输入分开。

真实链探针(WindowsUIACorrelator + QueuedCaptureSource + RecordingSession):
- 探针A(正例,当前红):Stop/Pause 按钮点击的 down+up 两包落在面板矩形上
  ——这是正常录制的停止操作,不是"面板吞掉了想点目标的输入"。控制标记
  (mark)之后落在面板矩形上的包必须按控制输入记账,不得计入
  recorderUiEvents 触发 recording_recorder_ui_events 失实诊断。
  0046 原件实锤:live-REC-ec395765861244bdb27a3d976aa6e136 正常录制
  (仅 Stop 点击)captureDiagnostics.recorderUiEvents=2(Stop down+up),
  当前 compile 会发失实 incomplete 诊断。
- 探针B(负例对照,必须保持红不变绿):无控制 mark、纯粹落在面板矩形上的
  点击才是真遮挡 —— 仍计 recorderUiEvents,事件不进业务步,且有拒绝明细。
- 探针C(计数语义):recorderUiEvents 原始计数保留(不删改);控制数在
  recorderControlEvents 明细;遮挡诊断只对"总计数-控制数"成立。
"""
from __future__ import annotations

import time

import pytest

from target.recording.models import CaptureScope
from target.recording.source import HookPacket, QueuedCaptureSource
from target.recording.windows import WindowsUIACorrelator

pytestmark = pytest.mark.unit

SCOPE = CaptureScope("target", "windows_pywinauto", "EDRClient.exe", "^logo1$")

PANEL_RECT = (900, 700, 120, 60)  # x, y, w, h
PANEL_POINT = (960, 730)          # inside the panel rect
TARGET_POINT = (100, 100)         # inside the target window, not the panel


class _AppResolver:
    def __init__(self):
        self.element_calls = 0

    def find_windows(self, *args, **kwargs):
        return [{"title": "logo1", "pid": 42, "processName": "EDRClient.exe",
                 "handle": 13239958}]

    def process_pid_belongs_to_root(self, pid, root):
        return True

    def process_in_scope(self, name, pid=None):
        return True

    def foreground(self):
        return {"windowTitle": "logo1", "processName": "EDRClient.exe",
                "pid": 42, "handle": 13239958}

    def window_at(self, x, y):
        return {"windowTitle": "logo1", "processName": "EDRClient.exe",
                "pid": 42, "handle": 13239958}

    def element_at(self, x, y, pid=None):
        self.element_calls += 1
        # The panel is topmost: a hit test there returns nothing the target
        # can name (the click never reached the target application).
        if PANEL_RECT[0] <= x < PANEL_RECT[0] + PANEL_RECT[2] and \
                PANEL_RECT[1] <= y < PANEL_RECT[1] + PANEL_RECT[3]:
            return None
        return {"controlType": "Button", "automationId": "btnApply",
                "text": "应用", "ownerPid": 42}

    def focused(self):
        return None


def _packet(ms, point, kind="pointer_down", button="left"):
    return HookPacket(
        kind=kind, monotonic_ms=ms, wall_time="2026-11-06T00:00:00.000Z",
        screen_point=point, button=button,
    )


def _click(source, point, t0):
    source._enqueue(_packet(t0, point, "pointer_down"))
    source._enqueue(_packet(t0 + 40, point, "pointer_up"))


class _NoopDriver:
    def start(self, emit): ...
    def stop(self): ...


def _session_with(correlator):
    from target.recording.session import RecordingSession
    session = RecordingSession("REC-C08", "review", SCOPE)
    source = QueuedCaptureSource(
        scope=SCOPE, sink=session.ingest, driver=_NoopDriver(),
        correlator=correlator)
    session._source = source
    # start() 的 _bind_recorder_ui_rect 只在真实 indicator/解析器上报矩形时
    # 绑定;探针直接固定矩形 —— 在 start() 之后再 set,避免被 bind 覆写。
    session.start()
    correlator.set_recorder_ui_rect(PANEL_RECT)
    return session, source


def _drain(source):
    """真实队列 worker 需要 wall-clock 出队;轮询至队列清空。"""
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        d = source.diagnostics()
        if d["queueDepth"] == 0 and not d["workerAlive"] is False:
            pass
        if d["queueDepth"] == 0:
            break
        time.sleep(0.01)
    time.sleep(0.05)


# --- 探针A(正例,预期红):控制点击不触发遮挡诊断 ----------------------

def test_c08_stop_button_click_is_control_input_not_occlusion():
    """Stop 按钮的 down+up:mark 之后落在面板矩形上 = 控制输入。
    recorderUiEvents 不得计数,compile 不得发 recording_recorder_ui_events。"""
    from agent.recording.compiler import compile_recording

    resolver = _AppResolver()
    correlator = WindowsUIACorrelator(resolver)
    session, source = _session_with(correlator)

    # 面板按钮回调先打控制 mark(接线后由 service 层真实触发),再收 down/up。
    correlator.mark_recorder_control()
    source._enqueue(_packet(10_000, PANEL_POINT, "pointer_down"))
    source._enqueue(_packet(10_040, PANEL_POINT, "pointer_up"))
    _drain(source)
    session.stop()

    recording = session.recording()
    diag = recording.capture_diagnostics
    assert diag["recorderUiEvents"] == 0, (
        f"C08 缺口:正常停止控制被计成面板遮挡,recorderUiEvents={diag},"
        "每份正常录制都会带失实 incomplete 诊断(0046 实锤=2)")
    assert diag.get("recorderControlEvents", 0) == 2, (
        f"控制 down+up 应在 recorderControlEvents 明细,实际 {diag}")
    assert not any(e.type == "pointer_click" for e in recording.events), \
        "控制输入不得混入业务步"
    result = compile_recording(recording)
    assert "recording_recorder_ui_events" not in {i.code for i in result.issues}, \
        "正常停止控制不得触发『面板吞掉想点目标的输入』失实诊断"


def test_c08_pause_button_click_is_also_control_input():
    """Pause 按钮同型:mark 后落在面板上的点击都是控制输入。"""
    resolver = _AppResolver()
    correlator = WindowsUIACorrelator(resolver)
    session, source = _session_with(correlator)

    correlator.mark_recorder_control()
    source._enqueue(_packet(20_000, PANEL_POINT, "pointer_down"))
    source._enqueue(_packet(20_040, PANEL_POINT, "pointer_up"))
    _drain(source)
    session.pause()
    correlator.mark_recorder_control()
    source._enqueue(_packet(21_000, PANEL_POINT, "pointer_down"))
    source._enqueue(_packet(21_040, PANEL_POINT, "pointer_up"))
    _drain(source)
    session.resume()

    diag = session.recording().capture_diagnostics
    assert diag["recorderUiEvents"] == 0, diag
    assert diag.get("recorderControlEvents", 0) == 4, diag


# --- 探针B(负例对照,必须保持):真遮挡仍如实诊断 --------------------

def test_c08_true_occlusion_without_control_mark_still_diagnosed():
    """无 mark、落在面板矩形上的点击 = 真遮挡:recorderUiEvents 计数、
    事件不进业务步、拒绝明细在案。这是既有正确行为,必须保持。"""
    from agent.recording.compiler import compile_recording

    resolver = _AppResolver()
    correlator = WindowsUIACorrelator(resolver)
    session, source = _session_with(correlator)

    _click(source, PANEL_POINT, 10_000)
    _drain(source)
    session.stop()

    recording = session.recording()
    diag = recording.capture_diagnostics
    assert diag["recorderUiEvents"] == 2, f"真遮挡必须计数,实际 {diag}"
    assert not any(e.type == "pointer_click" for e in recording.events), \
        "遮挡输入不得写成目标动作"
    rejections = [r["reason"] for r in recording.rejections]
    assert "recorder_ui" in rejections, recording.rejections
    result = compile_recording(recording)
    assert "recording_recorder_ui_events" in {i.code for i in result.issues}


def test_c08_occluded_click_on_target_window_not_written_as_action():
    """面板遮住目标窗口时的点击:事件不进业务步(不把遮挡输入写成
    目标动作),unidentified/遮挡归属由矩形与 hit-test 事实共同支撑。"""
    resolver = _AppResolver()
    correlator = WindowsUIACorrelator(resolver)
    session, source = _session_with(correlator)

    # 正常目标点击进业务步(对照)。
    _click(source, TARGET_POINT, 5_000)
    # 面板遮住目标时点击:不进业务步。
    _click(source, PANEL_POINT, 6_000)
    _drain(source)
    session.stop()

    events = [e for e in session.recording().events if e.type == "pointer_click"]
    assert len(events) == 1, "遮挡输入不得混入业务步"


# --- 探针C(计数语义):原始计数保留 + 诊断只对净遮挡数 ----------------

def test_c08_control_marks_do_not_suppress_real_occlusion_diagnostics():
    """mark 豁免只覆盖 mark 后短窗内的面板点击;窗外的真遮挡仍计数。
    总数-控制数>0 时 compile 仍发遮挡诊断(诊断对净遮挡数成立)。"""
    from agent.recording.compiler import compile_recording

    resolver = _AppResolver()
    correlator = WindowsUIACorrelator(resolver)
    session, source = _session_with(correlator)

    # Stop 控制点击(有 mark)。
    correlator.mark_recorder_control()
    _click(source, PANEL_POINT, 10_000)
    _drain(source)
    # 毫无 mark 的面板点击:用户以为点到了目标,面板吞了它(真遮挡)。
    # mark 窗口是真实时刻(±2s);把 mark 变成 5 秒前的旧记录,等价于
    # "点击发生在任何 mark 窗口之外"的确定性构造(不必真等 2 秒)。
    correlator._recorder_control_marks = [
        correlator._monotonic_ms() - 5_000]
    _click(source, PANEL_POINT, 30_000)
    _drain(source)
    session.stop()

    recording = session.recording()
    diag = recording.capture_diagnostics
    # 原始计数不删改:两次点击各有其账(控制 2 + 遮挡 2 = 面板总包 4)。
    # recorderUiEvents 语义精确化为"面板吞掉的用户输入"——控制点击不再
    # 混入;但控制点击并未从账面上消失,recorderControlEvents 单独在案。
    assert diag["recorderUiEvents"] == 2, f"净遮挡必须计数,实际 {diag}"
    assert diag.get("recorderControlEvents", 0) == 2, diag
    result = compile_recording(recording)
    assert "recording_recorder_ui_events" in {i.code for i in result.issues}, \
        "净遮挡(总-控制)>0 时不得漏诊断"
