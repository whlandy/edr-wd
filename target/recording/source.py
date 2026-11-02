"""Queue-isolated capture-source primitives shared by platform adapters.

Native input callbacks must return quickly.  They therefore emit only a small
``HookPacket`` into a bounded queue; target correlation and serialization run
on a worker thread and are the only code allowed to call the session sink.
"""

from __future__ import annotations

import queue
import threading
from dataclasses import dataclass, field
from typing import Callable, Mapping, Protocol

from .models import CaptureScope, RawCaptureEvent


@dataclass(frozen=True)
class HookPacket:
    kind: str
    monotonic_ms: int
    wall_time: str
    screen_point: tuple[int, int] | None = None
    button: str | None = None
    key: str | None = None
    modifiers: tuple[str, ...] = ()
    native: Mapping[str, object] = field(default_factory=dict)


class HookDriver(Protocol):
    def start(self, emit: Callable[[HookPacket], None]) -> None: ...
    def stop(self) -> None: ...


class CompositeHookDriver:
    """Run several platform drivers behind one emitter.

    Input hooks and window-lifecycle subscriptions are separate OS mechanisms
    but feed the same bounded queue, so ordering between an action and the
    transition it caused is preserved.  A driver that fails to start stops the
    ones already running, so no subscription outlives a failed session start.
    """

    def __init__(self, *drivers: HookDriver) -> None:
        if not drivers:
            raise ValueError("CompositeHookDriver requires at least one driver")
        self._drivers = drivers

    def start(self, emit: Callable[[HookPacket], None]) -> None:
        started: list[HookDriver] = []
        try:
            for driver in self._drivers:
                driver.start(emit)
                started.append(driver)
        except BaseException:
            for driver in reversed(started):
                try:
                    driver.stop()
                except Exception:
                    pass
            raise

    def stop(self) -> None:
        failure: Exception | None = None
        for driver in reversed(self._drivers):
            try:
                driver.stop()
            except Exception as exc:  # every driver must still be stopped
                failure = failure or exc
        if failure is not None:
            raise failure


class PacketCorrelator(Protocol):
    def correlate(
        self,
        packet: HookPacket,
        scope: CaptureScope,
        sequence: int,
    ) -> RawCaptureEvent | tuple[RawCaptureEvent, ...] | None: ...

    def current_target(self, scope: CaptureScope): ...


class QueuedCaptureSource:
    """Bounded callback-to-worker bridge with pause and clean-drain semantics."""

    _STOP = object()

    def __init__(
        self,
        *,
        scope: CaptureScope,
        sink: Callable[[RawCaptureEvent], bool],
        driver: HookDriver,
        correlator: PacketCorrelator,
        queue_size: int = 2048,
        stop_timeout: float = 30.0,
        inventory: object | None = None,
    ) -> None:
        if queue_size < 1:
            raise ValueError("queue_size must be positive")
        if stop_timeout <= 0:
            raise ValueError("stop_timeout must be positive")
        self.scope = scope
        self._sink = sink
        self._driver = driver
        self._correlator = correlator
        self._queue: queue.Queue[HookPacket | object] = queue.Queue(maxsize=queue_size)
        self._worker: threading.Thread | None = None
        self._paused = threading.Event()
        self._stop_requested = threading.Event()
        self._started = False
        self._stop_timeout = stop_timeout
        self._sequence = 0
        # Whole-window control capture. It runs on its own thread precisely so
        # that a tree walk never delays correlation, but its lifetime is this
        # source's: it must not outlive the hooks that drive it.
        self._inventory = inventory
        self.dropped_packets = 0
        self.correlation_errors: list[str] = []
        self.last_observed_target = None
        self.seeded_scope: tuple[str, ...] = ()
        self._flushed = False
        # Component-picking mode (U2): while set, correlated business events
        # are *not* persisted — the wizard's confirm key travels its own
        # management path, and a stray click during picking must not end up
        # recorded as if the user performed it.
        self._picking = False
        self.picking_dropped_events = 0
        # F52 attach 诊断：事件到达计数与驱动启动快照——actions=0 时
        # 区分「钩子起了但事件没来」vs「钩子没起」，同窗口同部署可对账。
        self._events_emitted = 0
        self._packets_enqueued = 0
        self._packets_by_kind: dict[str, int] = {}
        self._dequeued = 0
        self._paused_dropped = 0
        self._sink_false = 0
        self._start_error: str | None = None

    def diagnostics(self) -> dict:
        """F52/F52d：attach/运行健康快照（service.status 经由 session 暴露）。

        workerAlive=True 而 eventsEmitted=0 → 钩子回调从未入队（注册了但
        事件未达）；packetsEnqueued>0 而 eventsEmitted=0 → 关联/出队环节
        问题——不再需要重启即可定位环节。"""
        worker = self._worker
        return {
            "started": self._started,
            "workerAlive": bool(worker is not None and worker.is_alive()),
            "packetsEnqueued": self._packets_enqueued,
            "packetsByKind": dict(self._packets_by_kind),
            "dequeued": self._dequeued,
            "pausedDropped": self._paused_dropped,
            "pickingDropped": self.picking_dropped_events,
            "sinkFalse": self._sink_false,
            "eventsEmitted": self._events_emitted,
            "queueDepth": self._queue.qsize(),
            "correlationErrorCount": len(self.correlation_errors),
            "foreignAppEvents": getattr(self._correlator, 'foreign_app_events', None),
            "scopeRootProbe": getattr(self._correlator, 'scope_root_probe', None),
            # F53：有界错误摘要（最近3条、每条截断200字）——读实际异常定根因。
            "correlationErrors": [
                e[:200] for e in list(self.correlation_errors)[-3:]
            ],
            "startError": self._start_error,
            # F53e：运行实例取证——加载路径/函数首行/worker 线程名。
            "correlatorModule": str(getattr(type(self._correlator), '__module__', '?')),
            "correlateFile": str(getattr(self._correlator, 'correlate', None) and __import__('inspect').getsourcefile(type(self._correlator).correlate)),
            "correlateFirstLine": (lambda f: (lambda sl: sl[1] if sl else -1)(__import__('inspect').getsourcelines(f)))(getattr(type(self._correlator), 'correlate', None)) if hasattr(type(self._correlator), 'correlate') else -1,
            "packetTrace": list(getattr(self._correlator, 'packet_trace', []) or [])[-12:],
        }

    def begin_pick(self) -> None:
        """Enter picking mode: business persistence is suspended for the
        duration of one component selection."""
        self._picking = True

    def end_pick(self) -> None:
        self._picking = False

    def inspect_at(self, point=None):
        """Management-path hit test for the wizard (worker-safe).

        Reuses the correlator's pointer hit test; ``point`` is accepted for
        future explicit-coordinate hit tests but the current backends resolve
        the control under the live pointer, which is what the "point, then
        confirm" interaction needs.
        """
        resolver = getattr(self._correlator, "current_target", None)
        if not callable(resolver):
            return None
        try:
            return resolver(self.scope)
        except Exception as exc:
            self.correlation_errors.append(str(exc))
            return None

    def _flush_pending(self) -> None:
        if self._flushed:
            return
        self._flushed = True
        flush = getattr(self._correlator, "flush", None)
        if not callable(flush):
            return
        try:
            flushed = flush(self.scope, self._sequence + 1)
            events = (
                flushed if isinstance(flushed, tuple)
                else () if flushed is None else (flushed,)
            )
            for event in events:
                if self._sink(event):
                    self._sequence = event.sequence
                    if event.observed_target is not None:
                        self.last_observed_target = event.observed_target
        except Exception as exc:
            self.correlation_errors.append(str(exc))

    @property
    def control_snapshots(self) -> tuple[Mapping[str, object], ...]:
        """Complete control trees captured for the windows this flow visited."""
        if self._inventory is None:
            return ()
        return tuple(getattr(self._inventory, "snapshots", ()) or ())

    @property
    def out_of_scope_events(self) -> int:
        """Input the correlator saw but refused as outside the capture scope."""
        return int(getattr(self._correlator, "out_of_scope_events", 0))

    def current_observed_target(self):
        """Return the control currently under the pointer when supported.

        Assertion editing is a management path, not a native-hook callback, so
        doing a synchronous UIA/AX hit test here does not violate the bounded
        callback rule.  A failed live hit test falls back to the last correlated
        semantic target in :class:`RecordingSession`.
        """
        resolver = getattr(self._correlator, "current_target", None)
        if not callable(resolver):
            return None
        try:
            return resolver(self.scope)
        except Exception as exc:
            self.correlation_errors.append(str(exc))
            return None

    def _enqueue(self, packet: HookPacket) -> None:
        """Native callback path: one non-blocking queue operation only."""
        # F52d 诊断：入队计数——区分「钩子回调没来」vs「来了但出队/关联没走」。
        self._packets_enqueued += 1
        kind = getattr(packet, "kind", None)
        if kind:
            self._packets_by_kind[kind] = self._packets_by_kind.get(kind, 0) + 1
        if self._paused.is_set():
            return
        try:
            self._queue.put_nowait(packet)
        except queue.Full:
            self.dropped_packets += 1

    def _run(self) -> None:
        try:
            while True:
                packet = self._queue.get()
                self._dequeued += 1
                try:
                    if packet is not self._STOP and self._paused.is_set():
                        self._paused_dropped += 1
                    if packet is not self._STOP and not self._paused.is_set():
                        assert isinstance(packet, HookPacket)
                        next_sequence = self._sequence + 1
                        correlated = self._correlator.correlate(
                            packet, self.scope, next_sequence,
                        )
                        if correlated is not None:
                            if self._picking:
                                # Picking suspends business persistence; the
                                # packet is accounted for, never recorded.
                                self.picking_dropped_events += 1
                                continue
                            events = (
                                correlated if isinstance(correlated, tuple)
                                else (correlated,)
                            )
                            for event in events:
                                if event.sequence != self._sequence + 1:
                                    raise ValueError(
                                        "correlator returned a non-contiguous sequence"
                                    )
                                if self._sink(event):
                                    self._sequence = event.sequence
                                    self._events_emitted += 1
                                else:
                                    self._sink_false += 1
                                    if event.observed_target is not None:
                                        self.last_observed_target = event.observed_target
                except Exception as exc:  # native capture must remain alive
                    self.correlation_errors.append(str(exc))
                finally:
                    self._queue.task_done()
                # A lease can expire inside sink() on this worker.  When the
                # bounded queue is full, stop() cannot enqueue a sentinel;
                # finish draining pre-stop packets and terminate at empty.
                if packet is self._STOP or (
                    self._stop_requested.is_set() and self._queue.empty()
                ):
                    self._flush_pending()
                    return
        finally:
            self._started = False

    def start(self) -> None:
        if self._started:
            raise RuntimeError("capture source already started")
        # Admit the application's existing windows before any input arrives:
        # scope growth by transition only sees windows opened from here on.
        seed = getattr(self._correlator, "seed_scope", None)
        if callable(seed):
            try:
                self.seeded_scope = tuple(seed(self.scope) or ())
            except Exception as exc:
                self.correlation_errors.append(str(exc))
        if self._inventory is not None:
            # Started after seeding, so the walks the seed queued for windows
            # that were already open are the first thing it does.
            self._inventory.start()
        self._started = True
        self._stop_requested.clear()
        self._flushed = False
        self._worker = threading.Thread(
            target=self._run,
            name="edr-wd-recording-correlator",
            daemon=True,
        )
        self._worker.start()
        try:
            self._driver.start(self._enqueue)
        except BaseException as exc:
            # F52 attach 诊断：驱动启动失败如实入快照（同窗口同部署对账）。
            self._start_error = f"{type(exc).__name__}: {exc}"
            self._stop_requested.set()
            try:
                self._queue.put_nowait(self._STOP)
            except queue.Full:
                pass
            self._worker.join(timeout=2)
            self._started = False
            raise

    def pause(self) -> None:
        self._paused.set()

    def resume(self) -> None:
        self._paused.clear()

    def stop(self) -> None:
        if not self._started:
            return
        self._driver.stop()
        self._stop_requested.set()
        try:
            self._queue.put_nowait(self._STOP)
        except queue.Full:
            # The worker exits after draining the queue.  This branch is
            # especially important when stop() originates from that worker.
            pass
        if self._worker is not None:
            if self._worker is not threading.current_thread():
                self._worker.join(timeout=self._stop_timeout)
                if self._worker.is_alive():
                    # The worker is still inside a correlation call.  The
                    # native driver is already stopped, so no further input
                    # can be captured and the events collected so far are
                    # intact.  Raising here would propagate out of
                    # RecordingSession.stop() and discard the whole
                    # recording, so record the truncation as a correlation
                    # error instead: correlationErrorCount then keeps the
                    # golden trace incomplete (P1.5a) rather than letting a
                    # short capture look clean.
                    self.correlation_errors.append(
                        "correlator worker did not stop within "
                        f"{self._stop_timeout:g}s; recording truncated"
                    )
                self._started = False
        if self._inventory is not None:
            # Last, and never before the correlator has drained: the final
            # walk it queues is the page the user ended on, which is the one a
            # generated trace most needs and the one whose debounce has not
            # expired.
            try:
                self._inventory.stop()
            except Exception as exc:
                self.correlation_errors.append(f"control inventory stop: {exc}")
