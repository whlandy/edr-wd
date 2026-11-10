"""C07 探针：短 burst → 立即停止 → 收集（设计11 §9 C07 / §5.3，真实链路）。

账本 C07：scope/目标均合法；正常输入→短burst→立即停止→收集；
接收/归并/拒绝账实一致；无无界stop；耗时计数可解释；
必须保持：不降低采样丢动作来换流畅。

探针A（正例）：真实 QueuedCaptureSource(sink=session.ingest) 链上灌入
50 包 burst（点击/按键/滚轮混发）后立即 stop()：所有已接收入队包必须
全部被处理（dequeued == enqueued，无静默丢弃），session 收到的事件数与
diagnostics.eventsEmitted 一致；stop 有界（real stop 耗时记入断言，不设
通过上限——先固定当前环境基线）。
探针B（pause 丢弃账面）：burst 全部落在 pause 期间：_enqueue 对 paused
包直接 return（连队列都不进）。diagnostics.pausedDropped 是既有计数
字段 —— 若恒 0 而包确实被丢，即为 C06 同型"一层有字段、底层无人递增"
缺口（§5.1：一层说已剔除、底层混算/恒0）。
探针C（队列满丢弃账面）：queue_size=4 的迷你队列灌 30 包，sink 全真：
dropped_packets 计数与 dequeued+dropped 的对账（丢包必须有账，且
droppedEvents 最终文档须含这批流失——refresh_timeline_coverage 已把
dropped_packets 计入，探针核实最终链一致）。
"""
from __future__ import annotations

import time

import pytest

from target.recording.models import CaptureScope, RawCaptureEvent, ObservedTarget
from target.recording.source import CompositeHookDriver, HookPacket, QueuedCaptureSource

pytestmark = pytest.mark.unit

SCOPE = CaptureScope("target", "fake", "EDRClient.exe", "EDRClient")


class FakeDriver:
    def __init__(self):
        self.started = 0
        self.stopped = 0

    def start(self, emit):
        self.started += 1

    def stop(self):
        self.stopped += 1


class FakeCorrelator:
    """逐包产出一个 pointer_click 事件；pause 期间也被调用吗？
    不会 —— pause 在 _enqueue 侧拦截，worker 不出队。"""

    def __init__(self):
        self.calls = 0
        self.foreign_app_events = 0
        self.scope_root_probe = None
        self.packet_trace = []

    def correlate(self, packet, scope, sequence):
        self.calls += 1
        self.packet_trace.append(sequence)
        target = ObservedTarget("OBS", "T0001", "sha256:x", "Button", "apply")
        return RawCaptureEvent(sequence, packet.wall_time, packet.monotonic_ms,
                               "pointer_click", scope, {}, target)


def _packet(i, kind="pointer_click"):
    return HookPacket(
        kind=kind, monotonic_ms=i * 10,
        wall_time="2026-11-06T00:00:00.000Z", screen_point=(100, 100),
    )


def _wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return False


def _make_source(sink, **kw):
    return QueuedCaptureSource(scope=SCOPE, sink=sink,
                               driver=FakeDriver(), correlator=FakeCorrelator(),
                               **kw)


# --- 探针A：burst→立即stop，接收/归并/拒绝账实一致 + 耗时基线 ----------

def test_c07_burst_then_immediate_stop_accounts_every_packet():
    """§5.3 停止收尾：短 burst 后立即 stop，已接收包必须全部走完
    correlate→sink；eventsEmitted==session 接收数；stop 耗时实测入账。"""
    from target.recording.session import RecordingSession

    session = RecordingSession("REC-C07A", "review", SCOPE)
    source = QueuedCaptureSource(
        scope=SCOPE, sink=session.ingest, driver=FakeDriver(),
        correlator=FakeCorrelator())
    session._source = source
    session.start()
    burst = [_packet(i) for i in range(50)]
    t0 = time.monotonic()
    for p in burst:
        source._enqueue(p)
    session.stop()
    stop_ms = (time.monotonic() - t0) * 1000

    diag = source.diagnostics()
    # 账实一致：入队包全部被 worker 消费，无一静默消失。
    assert diag["dequeued"] == diag["packetsEnqueued"] == 50, (
        f"burst 后立即 stop 丢包：enqueued={diag['packetsEnqueued']} "
        f"dequeued={diag['dequeued']} diag={diag}")
    assert diag["eventsEmitted"] == 50, (
        f"diagnostics 与 session 实收不一致：emitted={diag['eventsEmitted']}")
    assert diag["queueDepth"] == 0
    assert not source.correlation_errors, source.correlation_errors
    # worker 与 inventory 均已退出，stop 有界返回。
    assert not diag["workerAlive"]
    # 耗时基线：当前环境实测值固定下来（不断言上限，§5.3 未测数值不得写达标）。
    print(f"C07-A stop drain took {stop_ms:.1f}ms for 50 packets, "
          f"stopDrainMs={diag['stopDrainMs']}")
    assert stop_ms < 30_000, f"stop 耗时 {stop_ms:.0f}ms 已超 stop_timeout 量级"
    # 最终文档计数与 source 诊断同源。
    recording = session.recording()
    assert recording.capture_diagnostics["droppedPackets"] == 0
    assert len(recording.events) == 50


def test_c07_stop_is_bounded_with_wide_open_queue():
    """§5.3 无无界 stop：队列塞满慢 sink 包时，stop() 的 join 有界
    （stop_timeout），超时记 truncated 关联错误、录制不假干净；
    实际 drain 耗时入账（stopDrainMs），不仅报上限值。"""
    import threading

    class SlowSink:
        def __init__(self):
            self.gate = threading.Event()
            self.calls = 0

        def __call__(self, event):
            self.calls += 1
            self.gate.wait(timeout=10)
            return True

    sink = SlowSink()
    source = _make_source(sink=sink, stop_timeout=0.2)
    source.start()
    for i in range(20):
        source._enqueue(_packet(i))
    assert _wait_until(lambda: sink.calls >= 1)
    t0 = time.monotonic()
    source.stop()
    elapsed_ms = (time.monotonic() - t0) * 1000
    assert any("truncated" in e for e in source.correlation_errors), (
        f"慢 sink 下 stop 必须有界并以 truncated 记账，实际 errors="
        f"{source.correlation_errors}")
    # stop 有界：不超过 timeout + 合理调度余量。
    assert elapsed_ms < 2000, f"stop 耗时 {elapsed_ms:.0f}ms，无界"
    # 实际耗时（>timeout 的真实等待）如实在账，不是 None 或 0。
    drain = source.diagnostics()["stopDrainMs"]
    assert isinstance(drain, int) and drain >= 200, (
        f"stopDrainMs 应记录实际 drain 耗时（>=timeout 200ms），实际 {drain}")
    sink.gate.set()
    assert _wait_until(lambda: not source.diagnostics()["workerAlive"])


# --- 探针B：pause 期间入队的包不得无账丢弃（预期红） ------------------

def test_c07_pause_drops_are_counted_not_silent():
    """§5.1：一层说"已剔除"而底层无人递增=接线缺口。pause 期间
    _enqueue 直接 return 的包必须有账（pausedDropped 既有字段）。"""
    source = _make_source(sink=lambda event: True)
    source.start()
    source.pause()
    for i in range(10):
        source._enqueue(_packet(i))
    source.resume()
    source.stop()
    diag = source.diagnostics()
    assert diag["pausedDropped"] == 10, (
        f"C07 缺口：pause 期间丢弃 10 包但 diagnostics 无账 "
        f"pausedDropped={diag['pausedDropped']}，diag={diag}。"
        f"§5.1 禁止一层有字段、底层恒 0")
    assert diag["eventsEmitted"] == 0


# --- 探针C：队列满丢弃账实精确对账 -----------------------------------

def test_c07_queue_overflow_drops_match_dropped_packets_counter():
    """§5.3：队列满时丢包必须有精确账（dropped_packets），且未丢包
    全部到 sink；最终文档 droppedPackets 与 source 计数一致。"""
    from target.recording.session import RecordingSession

    session = RecordingSession("REC-C07C", "review", SCOPE)
    source = QueuedCaptureSource(
        scope=SCOPE, sink=session.ingest, driver=FakeDriver(),
        correlator=FakeCorrelator(), queue_size=4)
    session._source = source
    session.start()
    # 不等 worker：30 包一次性灌入，保证 >4 包与队列竞争。
    for i in range(30):
        source._enqueue(_packet(i))
    session.stop()

    diag = source.diagnostics()
    delivered = diag["dequeued"]
    dropped = source.dropped_packets
    assert delivered + dropped == 30, (
        f"账实不一致：dequeued={delivered} dropped={dropped} 总和≠30，diag={diag}")
    assert dropped > 0, "queue_size=4 灌 30 包应触发丢包计数（探针有效性）"
    # 最终文档：droppedPackets 如实——录制比输入短必须有账可查。
    recording = session.recording()
    assert recording.capture_diagnostics["droppedPackets"] == dropped, (
        f"最终文档 droppedPackets 与 source 计数不一致：文档="
        f"{recording.capture_diagnostics['droppedPackets']} source={dropped}")
    assert len(recording.events) == delivered, (
        f"文档 events 数与 dequeued 不一致：events={len(recording.events)} "
        f"dequeued={delivered}")
    assert not source.correlation_errors, source.correlation_errors
