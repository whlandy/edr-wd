"""Lease-watchdog regression: an abandoned recording session must end itself.

Commercial gate (batch 5 disconnect recovery / batch 6 orphan windows / batch 8
no-residue).  The capture lease is a contract: an agent that started a recording
must keep renewing it via heartbeat/status.  Historically the only enforcement
of that contract was lazy — ``_expire_before_operation`` ran when the next
session operation arrived.  With no agent and no operator input, nothing ever
arrives, so the recorder indicator window and the native capture hook stayed up
forever: an orphaned capture on the operator's desktop with no deadline at all.

The contract these tests lock in:

- When the lease lapses with no input and no renewals, the session must reach a
  terminal state on its own — the source and the indicator are stopped, and the
  reason says the lease expired (so the eventual stop/compile stays honest:
  an abandoned capture must never masquerade as a clean user stop).
- The watchdog must keep working long after start() returns (it is not a
  one-shot timer tied to start), and must NOT fire while the session is being
  renewed normally (heartbeat inside the lease).
- A terminal session must not keep the watchdog thread alive: the watch stops
  once the session is no longer active, and the thread exits cleanly
  (joinable, no leak — the service creates one per recording).
- Expiry must be visible in the same evidence the operator already reads:
  state=stopped with reason "recording_lease_expired", exactly like the lazy
  path, so callers cannot distinguish which mechanism fired and no special
  case is needed downstream.
"""

from __future__ import annotations

import threading
import time

import pytest

from target.recording.models import CaptureScope
from target.recording.session import CaptureSessionState, RecordingSession

pytestmark = pytest.mark.unit

SCOPE = CaptureScope("target", "fake", "EDRClient.exe", "EDRClient")


class Clock:
    """Controllable monotonic clock the watchdog polls (no real sleeping)."""

    def __init__(self) -> None:
        self._value = 1.0
        self._cv = threading.Condition()

    def __call__(self) -> float:
        with self._cv:
            return self._value

    def advance(self, delta: float) -> None:
        with self._cv:
            self._value += delta
            self._cv.notify_all()


class Source:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self._lock = threading.Lock()

    def start(self) -> None:
        with self._lock:
            self.calls.append("start")

    def pause(self) -> None:
        with self._lock:
            self.calls.append("pause")

    def resume(self) -> None:
        with self._lock:
            self.calls.append("resume")

    def stop(self) -> None:
        with self._lock:
            self.calls.append("stop")


class Indicator:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def start(self) -> None:
        self.calls.append("start")

    def pause(self) -> None:
        self.calls.append("pause")

    def resume(self) -> None:
        self.calls.append("resume")

    def update_count(self, count: int) -> None:
        pass

    def stop(self) -> None:
        self.calls.append("stop")


def wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def _watchdog_thread_of(session: RecordingSession) -> threading.Thread:
    for thread in threading.enumerate():
        if thread.name.startswith(f"recording-lease-watchdog-{session.session_id}"):
            return thread
    pytest.fail("no lease-watchdog thread is running for an active session")


def test_abandoned_session_expires_itself_with_no_operations_at_all():
    """No ingest, no heartbeat, no status — the lease still ends the capture."""
    clock = Clock()
    source = Source()
    indicator = Indicator()
    session = RecordingSession(
        "REC-WD1", "flow", SCOPE, source=source, lease_seconds=0.5,
        monotonic=clock,
    )
    session.attach_indicator(indicator)
    session.start()
    watchdog = _watchdog_thread_of(session)

    # The contract boundary: nothing from outside ever touches the session
    # after start(). The clock alone moves.
    clock.advance(1.0)

    assert wait_until(
        lambda: session.status().state is CaptureSessionState.STOPPED
    ), "an abandoned recording never reached a terminal state on its own"
    assert source.calls[-1] == "stop", "orphaned capture source kept running"
    assert indicator.calls[-1] == "stop", "orphaned recorder indicator kept running"
    assert session.status().reason == "recording_lease_expired"

    watchdog.join(timeout=2.0)
    assert not watchdog.is_alive(), (
        "the watchdog thread outlived the terminated session (thread leak)"
    )


def test_watchdog_does_not_fire_while_the_lease_is_being_renewed():
    clock = Clock()
    source = Source()
    session = RecordingSession(
        "REC-WD2", "flow", SCOPE, source=source, lease_seconds=0.5,
        monotonic=clock,
    )
    session.start()

    # Renew at 0.25s intervals — inside the lease — for 3 full lease periods.
    # Status polling between renewals is deliberately *not* used: heartbeat is
    # the renewal, and status() would itself run the lazy expiry path and make
    # this test vacuous.
    for _ in range(12):
        clock.advance(0.25)
        session.heartbeat()

    assert session._state is CaptureSessionState.RECORDING, (
        "a heartbeat-renewed recording was terminated by its own watchdog"
    )
    assert "stop" not in source.calls

    # And after renewal stops, the same watchdog still ends it.
    clock.advance(1.0)
    assert wait_until(
        lambda: source.calls and source.calls[-1] == "stop"
    ), "the watchdog did not end the capture after renewals stopped"


def test_watchdog_exits_when_the_session_stops_normally():
    """A user stop must not leave the watch running against a dead session."""
    clock = Clock()
    source = Source()
    session = RecordingSession(
        "REC-WD3", "flow", SCOPE, source=source, lease_seconds=60.0,
        monotonic=clock,
    )
    session.start()
    watchdog = _watchdog_thread_of(session)
    session.stop()

    watchdog.join(timeout=2.0)
    assert not watchdog.is_alive(), (
        "watchdog kept polling after the session ended normally"
    )
    # A long lapse after a normal stop must not mutate the finished session.
    clock.advance(120.0)
    time.sleep(0.05)
    assert session.status().state is CaptureSessionState.STOPPED
    assert session.status().reason == "user"


def test_paused_session_is_still_under_the_lease():
    """Pause is a capture state, not a release: it must not escape the lease."""
    clock = Clock()
    source = Source()
    session = RecordingSession(
        "REC-WD4", "flow", SCOPE, source=source, lease_seconds=0.5,
        monotonic=clock,
    )
    session.start()
    session.pause()
    clock.advance(1.0)

    # Poll the source, not the session: session.status() runs the lazy expiry
    # path itself and would make this test vacuous against the watchdog.
    assert wait_until(
        lambda: source.calls and source.calls[-1] == "stop"
    ), "a paused recording escaped lease enforcement"
