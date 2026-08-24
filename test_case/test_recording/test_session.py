from __future__ import annotations

import pytest

from target.recording.models import CaptureScope, RawCaptureEvent, ObservedTarget, RecordingModelError
from target.recording.session import CaptureSessionState, RecordingSession, RecordingSessionManager

pytestmark = pytest.mark.unit


class Clock:
    value = 1.0
    def __call__(self): return self.value


class Source:
    def __init__(self): self.calls = []
    def start(self): self.calls.append("start")
    def pause(self): self.calls.append("pause")
    def resume(self): self.calls.append("resume")
    def stop(self): self.calls.append("stop")


SCOPE = CaptureScope("target", "fake", "EDRClient.exe", "EDRClient")


def event(sequence=1, *, scope=SCOPE, protected=False, kind="pointer_click", input_=None):
    return RawCaptureEvent(sequence, "2026-08-17T00:00:00Z", sequence * 10, kind, scope, input_ or {}, ObservedTarget("OBS", "T0001", "sha256:x", "Button", "apply", protected=protected))


def test_session_lifecycle_receipts_and_recording():
    clock=Clock();source=Source();session=RecordingSession("REC", "flow", SCOPE, source=source, monotonic=clock)
    assert session.start().state is CaptureSessionState.RECORDING
    assert session.ingest(event()) is True
    assert session.pause().state is CaptureSessionState.PAUSED
    assert session.ingest(event(2)) is False
    assert session.resume().state is CaptureSessionState.RECORDING
    receipt, recording=session.stop()
    assert receipt.state is CaptureSessionState.STOPPED
    assert [x.sequence for x in recording.events] == [1]
    assert source.calls == ["start", "pause", "resume", "stop"]


def test_scope_and_low_level_noise_are_dropped_before_persistence():
    session=RecordingSession("REC", "flow", SCOPE);session.start()
    other=CaptureScope("target", "fake", "Other.exe", "Other")
    assert session.ingest(event(scope=other)) is False
    assert session.ingest(event(kind="key_down")) is False
    assert session.recording().events == ()


def test_protected_plaintext_is_rejected_at_source_boundary():
    session=RecordingSession("REC", "flow", SCOPE);session.start()
    with pytest.raises(RecordingModelError) as exc:
        session.ingest(event(protected=True, kind="text_commit", input_={"value":"secret"}))
    assert exc.value.code == "recording_secret_plaintext"
    assert session.recording().events == ()


def test_lease_expiry_stops_source_and_rejects_late_events():
    clock=Clock();source=Source();session=RecordingSession("REC", "flow", SCOPE, source=source, lease_seconds=2, monotonic=clock);session.start()
    clock.value=4.1
    assert session.status().reason == "recording_lease_expired"
    assert session.ingest(event()) is False
    assert source.calls == ["start", "stop"]


def test_accepted_user_activity_renews_the_recording_lease():
    clock=Clock();source=Source();session=RecordingSession(
        "REC", "flow", SCOPE, source=source, lease_seconds=2, monotonic=clock,
    );session.start()
    clock.value=2.5
    assert session.ingest(event()) is True
    clock.value=4.0
    assert session.status().state is CaptureSessionState.RECORDING
    clock.value=4.6
    assert session.status().reason == "recording_lease_expired"


def test_manager_rejects_overlapping_active_sessions():
    manager=RecordingSessionManager();manager.start(RecordingSession("A","a",SCOPE))
    with pytest.raises(RecordingModelError) as exc:
        manager.start(RecordingSession("B","b",SCOPE))
    assert exc.value.code == "recording_session_active"


def _assertion(**overrides):
    payload = {
        "assertion": "visible", "expected": True, "timeoutSeconds": 5.0,
        "automationId": "listRows",
    }
    payload.update(overrides)
    return payload


def test_an_assertion_can_bind_itself_to_the_preceding_recorded_action():
    session = RecordingSession("REC", "flow", SCOPE)
    session.start()
    scroll = RawCaptureEvent(
        1, "2026-08-17T00:00:00Z", 10, "scroll_commit", SCOPE, {"delta": -120},
        ObservedTarget("OBS", "T0001", "sha256:x", "List", "listRows"),
        causal_id="CAUSE-scroll",
    )
    assert session.ingest(scroll) is True
    session.add_assertion(_assertion(bindPrevious=True))

    events = session.recording().events
    assert events[1].type == "assertion"
    assert events[1].causal_id == "CAUSE-scroll"


def test_an_unbound_assertion_carries_no_causal_id():
    session = RecordingSession("REC", "flow", SCOPE)
    session.start()
    session.ingest(event())
    session.add_assertion(_assertion())
    assert session.recording().events[1].causal_id is None


def test_binding_a_verifier_without_a_preceding_action_is_refused():
    session = RecordingSession("REC", "flow", SCOPE)
    session.start()
    with pytest.raises(RecordingModelError) as exc:
        session.add_assertion(_assertion(bindPrevious=True))
    assert exc.value.code == "recording_assertion_unbound"
    assert session.recording().events == ()


def test_bind_previous_must_be_boolean():
    session = RecordingSession("REC", "flow", SCOPE)
    session.start()
    session.ingest(event())
    with pytest.raises(RecordingModelError) as exc:
        session.add_assertion(_assertion(bindPrevious="yes"))
    assert exc.value.code == "recording_assertion_invalid"


def test_stop_waits_for_a_concurrent_stop_instead_of_losing_the_recording():
    """A live capture was lost exactly this way.

    The indicator's Stop button stops the session on its own thread; draining
    the correlator can take tens of seconds.  A stop() arriving during that
    window used to raise `recording_state_invalid`, so the caller got an
    exception instead of the events that had already been captured.
    """
    import threading

    release = threading.Event()

    class _SlowSource(Source):
        def stop(self):
            release.wait(timeout=5)
            super().stop()

    session = RecordingSession("REC", "flow", SCOPE, source=_SlowSource())
    session.start()
    session.ingest(event(1))

    first: list = []
    worker = threading.Thread(
        target=lambda: first.append(session.stop("indicator")), daemon=True,
    )
    worker.start()
    # Let the indicator-initiated stop reach STOPPING and block in the drain.
    while session._state is not CaptureSessionState.STOPPING:
        pass
    release.set()

    receipt, recording = session.stop("user")

    worker.join(timeout=5)
    assert receipt.state is CaptureSessionState.STOPPED
    assert [e.sequence for e in recording.events] == [1]


def test_stop_still_returns_the_capture_after_a_failed_stop():
    class _ExplodingSource(Source):
        def stop(self):
            raise RuntimeError("drain blew up")

    session = RecordingSession("REC", "flow", SCOPE, source=_ExplodingSource())
    session.start()
    session.ingest(event(1))

    with pytest.raises(RuntimeError):
        session.stop()

    # The failure is still visible in the receipt, but the events the capture
    # already holds must not be unreachable.
    receipt, recording = session.stop()
    assert receipt.state is CaptureSessionState.FAILED
    assert [e.sequence for e in recording.events] == [1]
