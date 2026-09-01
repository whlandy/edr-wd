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
                try:
                    if packet is not self._STOP and not self._paused.is_set():
                        assert isinstance(packet, HookPacket)
                        next_sequence = self._sequence + 1
                        correlated = self._correlator.correlate(
                            packet, self.scope, next_sequence,
                        )
                        if correlated is not None:
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
        except BaseException:
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
