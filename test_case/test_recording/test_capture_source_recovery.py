"""Offline regression for QueuedCaptureSource recovery paths (commercial gate).

Covers the item 5/8 recovery honesty guarantees a native capture must keep when
the worker thread cannot drain cleanly at stop() time (target/recording/source.py,
``QueuedCaptureSource.stop``):

- test_stop_timeout_records_truncation_not_clean: when stop()'s worker join
  times out (the worker is blocked inside the sink), the source must NOT raise
  (raising would discard the whole recording) and must NOT look clean. It
  records a truncation correlation error so ``correlationErrorCount`` keeps the
  golden trace incomplete (P1.5a honesty gate): a hung worker must never
  masquerade as a clean ready.

- test_clean_stop_records_no_truncation: the truncation branch is not vacuous --
  a normal, fully-draining stop records zero correlation errors stolen from the
  hung path circuits / a false positive.

- test_capture_events_are_still_delivered_after_worker_release: once the hang is
  released the worker drains the already-packaged action into the sink, so the
  timeout records a *cause*, it never silently drops the captured action.

These are load-bearing honesty behaviors. A mutation that deletes the truncation
error reporting in ``stop`` must flip the first test red.
"""

from __future__ import annotations

import threading
import time

import pytest

from target.recording.models import CaptureScope, RawCaptureEvent, ObservedTarget
from target.recording.source import HookPacket, QueuedCaptureSource

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
                               packet.kind, scope, {}, target)


def event_packet(kind="pointer_click", seq=1):
    return HookPacket(kind=kind, monotonic_ms=seq * 10,
                      wall_time="2026-08-17T00:00:00Z", screen_point=(100, 100))


class BlockingSink:
    """Blocks the worker inside sink() until the test releases it.

    Holds the worker mid-correlation so stop()'s join(timeout) must time out and
    exercise the truncation branch deterministically.
    """

    def __init__(self):
        self.release = threading.Event()
        self.inside_sink = threading.Event()
        self.accepted = []

    def __call__(self, event: RawCaptureEvent) -> bool:
        self.inside_sink.set()
        self.release.wait(timeout=10)
        self.accepted.append(event)
        return True


class CountingSink:
    def __init__(self):
        self.calls = 0

    def __call__(self, event: RawCaptureEvent) -> bool:
        self.calls += 1
        return True


def _make_source(sink, **kw):
    return QueuedCaptureSource(scope=SCOPE, sink=sink,
                               driver=FakeDriver(), correlator=FakeCorrelator(),
                               **kw)


def _wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return False


def test_stop_timeout_records_truncation_not_clean():
    sink = BlockingSink()
    source = _make_source(sink=sink, stop_timeout=0.1)
    source.start()
    try:
        source._enqueue(event_packet())
        assert _wait_until(sink.inside_sink.is_set), "worker never reached sink"
        # stop() must time out the join, surface the truncation as a correlation
        # error, and NOT raise (raising would discard the whole recording).
        source.stop()
        # While the worker is still blocked, nothing sank yet -- the capture is
        # not reported as clean.
        assert source.diagnostics()["eventsEmitted"] == 0
    finally:
        sink.release.set()
    assert any("truncated" in err for err in source.correlation_errors), (
        "stop-timeout must surface a truncation error, got %r"
        % source.correlation_errors
    )
    assert source._started is False
    # No zombie worker after the hang is released.
    assert _wait_until(lambda: not source.diagnostics()["workerAlive"])


def test_clean_stop_records_no_truncation():
    # A normal fully-draining stop records zero correlation errors -- the
    # truncation branch above is not a vacuous false positive.
    sink = CountingSink()
    source = _make_source(sink=sink)
    source.start()
    source._enqueue(event_packet())
    source.stop()
    assert sink.calls == 1
    assert source.diagnostics()["eventsEmitted"] == 1
    assert source.correlation_errors == []
    assert source._started is False


def test_capture_events_are_still_delivered_after_worker_release():
    # Timing-out records a *cause*; it never silently drops the already-captured
    # action. Once the hang is released the packaged event is sunk exactly once.
    sink = BlockingSink()
    source = _make_source(sink=sink, stop_timeout=0.1)
    source.start()
    try:
        source._enqueue(event_packet())
        assert _wait_until(sink.inside_sink.is_set)
        source.stop()
        assert any("truncated" in err for err in source.correlation_errors)
    finally:
        sink.release.set()
    assert _wait_until(lambda: len(sink.accepted) == 1), "captured action lost"
    assert source.diagnostics()["eventsEmitted"] == 1
    # Idempotent: a second stop after the worker has fully exited is a no-op.
    source.stop()
    truncation = [e for e in source.correlation_errors if "truncated" in e]
    assert len(truncation) == 1
