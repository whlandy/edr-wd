"""录制会话收尾时 capture source 必须无条件停止。

session 的两条收尾路径（用户 stop、lease 过期）先停 indicator 再停
source。若 indicator.stop() 抛错，旧实现直接跳过 source.stop() —— 会话
进入 FAILED 终态后没有任何路径再会停掉 capture hook，钩子与注入监听
在目标机上无限期残留（任务域 5/6/8：服务异常后恢复、孤儿窗口、失败后
不残留后台进程）。start() 早已对 source 做了 best-effort 收尾（session
start 失败 unwind），stop 侧必须同样对称。
"""
from __future__ import annotations

import pytest

from target.recording.models import CaptureScope, RawCaptureEvent, ObservedTarget
from target.recording.session import CaptureSessionState, RecordingSession

pytestmark = pytest.mark.unit

SCOPE = CaptureScope("target", "fake", "EDRClient.exe", "EDRClient")


def event(sequence=1):
    return RawCaptureEvent(
        sequence, "2026-08-17T00:00:00Z", sequence * 10, "pointer_click",
        SCOPE, {}, ObservedTarget("OBS", "T0001", "sha256:x", "Button", "apply"),
    )


class _Source:
    def __init__(self):
        self.calls = []
        self.stop_count = 0

    def start(self): self.calls.append("start")
    def pause(self): self.calls.append("pause")
    def resume(self): self.calls.append("resume")

    def stop(self):
        self.stop_count += 1
        self.calls.append("stop")


class _Indicator:
    def __init__(self, stop_error=None):
        self.stop_error = stop_error
        self.stop_count = 0

    def start(self): pass
    def pause(self): pass
    def resume(self): pass
    def update_count(self, count): pass
    def open_assertion_editor(self, target=None): pass

    def stop(self):
        self.stop_count += 1
        if self.stop_error is not None:
            raise self.stop_error


def test_user_stop_stops_the_source_even_when_the_indicator_stop_raises():
    indicator = _Indicator(stop_error=OSError("subprocess pipe closed"))
    source = _Source()
    session = RecordingSession("REC", "flow", SCOPE, source=source)
    session.attach_indicator(indicator)
    session.start()
    session.ingest(event(1))

    with pytest.raises(OSError):
        session.stop()

    # 收尾顺序保持 indicator 先于 source（先收 UI 再排空 correlator），
    # 但 source 的停止不能因 indicator 异常被跳过。
    assert indicator.stop_count == 1
    assert source.stop_count == 1
    assert source.calls == ["start", "stop"]


def test_failed_stop_still_reaches_terminal_state_and_returns_capture():
    session = RecordingSession(
        "REC", "flow", SCOPE,
        source=_Source(),  # source 停止成功，异常只来自 indicator
    )
    session.attach_indicator(_Indicator(stop_error=RuntimeError("tk died")))
    session.start()
    session.ingest(event(1))

    with pytest.raises(RuntimeError):
        session.stop()

    receipt, recording = session.stop()
    assert receipt.state is CaptureSessionState.FAILED
    assert [e.sequence for e in recording.events] == [1]
    assert session._source.stop_count == 1


def test_lease_expiry_stops_the_source_even_when_the_indicator_stop_raises():
    clock_value = {"now": 1.0}
    source = _Source()
    session = RecordingSession(
        "REC", "flow", SCOPE, source=source,
        lease_seconds=10.0, monotonic=lambda: clock_value["now"],
    )
    session.attach_indicator(_Indicator(stop_error=OSError("pipe closed")))
    session.start()
    session.ingest(event(1))

    clock_value["now"] += 11.0  # 租约过期，watchdog/懒过期进入收尾
    assert session._expire_before_operation() is True
    assert session.state is CaptureSessionState.FAILED

    # 过期收尾失败于 indicator 后，source 必须已被停止——否则 capture
    # hook 在 FAILED 终态后无限期残留。
    assert source.stop_count == 1
    assert source.calls == ["start", "stop"]


def test_source_stop_failure_is_not_masked_by_indicator_failure_order():
    """indicator 异常不得吞掉 source 的异常：source 才是资源残留的主体。"""
    class _BadSource(_Source):
        def stop(self):
            super().stop()
            raise RuntimeError("hook thread did not stop")

    session = RecordingSession("REC", "flow", SCOPE, source=_BadSource())
    session.attach_indicator(_Indicator(stop_error=OSError("pipe closed")))
    session.start()

    with pytest.raises(RuntimeError, match="hook thread"):
        session.stop()
