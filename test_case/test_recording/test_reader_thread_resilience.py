"""Parent-side reader-thread resilience regression (batch 19, §7m).

The SubprocessTkIndicator parent runs one ``_consume`` reader thread that
dispatches child reports to session callbacks (pause/resume/stop) and answers
assertion submissions with an ack.  Those callbacks raise
``RecordingModelError("recording_state_invalid")`` the moment the session is
no longer in the state the callback requires — which is exactly what happens
when the operator double-clicks Pause, clicks Stop while the lease watchdog
is expiring the session, or an assertion form is still open when the session
settles.

A callback exception propagated out of ``_consume`` kills the reader thread
silently: the child's Tk window stays up (the parent's stdin is still open,
so no EOF self-termination fires), but no button report is ever consumed
again — the recorder window becomes a live-looking orphan whose every button
is dead and whose assertion forms never receive an ack.  Unlike the batch 18
orphan, the parent process is alive, so neither the child's EOF path nor the
session watchdog can rescue it.

Contract locked here (against the real ``_consume`` loop and a real
``RecordingSession`` — only the OS pipe plumbing is a double):

- A pause/resume/stop callback that raises (real ``recording_state_invalid``,
  not a simulation) must not kill the reader thread: the next report is still
  dispatched and its ack still reaches the child.
- A healthy callback still reaches the session and drives real state
  transitions, in order (no swallowing of the happy path).
- An add_assertion callback that raises still gets its failure ack delivered
  back to the child form with the form's requestId.

Red baseline note: on the unfixed code the first raising callback kills the
reader, so the follow-up report is never consumed and the follow-up ack never
arrives — the tests fail on the missing ack/dispatch, not on timing.
"""

from __future__ import annotations

import json
import os
import queue
import threading

import pytest

pytestmark = pytest.mark.unit


class _ProcessDouble:
    """OS-pipe stand-in for the indicator child process.

    ``_consume`` reads reports from ``process.stdout`` and acks via
    ``_send`` → ``process.stdin``; both sides here are real os.pipe file
    objects, so the loop-under-test runs unmodified.  Only the child process
    itself (Tk UI) is replaced — that is the plumbing, not the dispatch.
    """

    def __init__(self) -> None:
        report_r, report_w = os.pipe()
        ack_r, ack_w = os.pipe()
        self.stdout = os.fdopen(report_r, "r")
        self._report_w = report_w
        self.stdin = os.fdopen(ack_w, "w")
        self._ack_r = ack_r
        self.acks: queue.Queue = queue.Queue()
        self._drain = threading.Thread(target=self._drain_acks, daemon=True)
        self._drain.start()

    def _drain_acks(self) -> None:
        for line in os.fdopen(os.dup(self._ack_r), "r"):
            try:
                message = json.loads(line)
            except ValueError:
                continue
            # _send wraps acks as {"command": ..., "value": {...}}; the ack
            # payload the child form reads lives in "value".
            value = message.get("value")
            if isinstance(value, dict):
                self.acks.put(value)

    def write_report(self, message: dict) -> None:
        os.write(self._report_w, (json.dumps(message) + "\n").encode())

    def poll(self):
        return None  # alive, so _send keeps writing

    def close_reports(self) -> None:
        os.close(self._report_w)  # EOF ends _consume's for-loop

    def close(self) -> None:
        for stream in (self.stdout, self.stdin):
            try:
                stream.close()
            except Exception:
                pass


def _idle_session():
    from target.recording.models import CaptureScope
    from target.recording.session import RecordingSession

    scope = CaptureScope("t", "windows_pywinauto", "E.exe", "^E$")
    # An IDLE session refuses pause/resume/stop with the real
    # recording_state_invalid — the exact exception shape the live hazard
    # produces (expired session, double-click race), fully deterministic.
    return RecordingSession("REC-reader", "review", scope)


def _recording_session():
    from target.recording.models import CaptureScope
    from target.recording.session import RecordingSession

    scope = CaptureScope("t", "windows_pywinauto", "E.exe", "^E$")
    session = RecordingSession("REC-reader", "review", scope)
    session.start()  # NullCaptureSource + NullRecordingIndicator: safe offline
    return session


class _DispatchRecorder:
    """Records every dispatch, then delegates to the real session."""

    def __init__(self, session) -> None:
        self.session = session
        self.calls: list[str] = []

    def callbacks(self):
        from target.recording.indicator import IndicatorCallbacks

        def make(name):
            def callback(*args):
                self.calls.append(name)
                return getattr(self.session, name)(*args)
            return callback

        def add_assertion(payload):
            self.calls.append("add_assertion")
            return self.session.add_assertion(payload or {})

        return IndicatorCallbacks(
            pause=make("pause"), resume=make("resume"),
            stop=make("stop"), add_assertion=add_assertion,
        )


def _run(indicator, double: _ProcessDouble, messages, timeout: float = 5.0):
    from target.recording.indicator import SubprocessTkIndicator  # noqa: F401

    indicator._process = double
    indicator._reader = threading.Thread(
        target=indicator._consume, name="test-reader", daemon=True,
    )
    indicator._reader.start()
    for message in messages:
        double.write_report(message)
    double.close_reports()
    indicator._reader.join(timeout=timeout)
    assert not indicator._reader.is_alive(), (
        "reader thread did not exit after report EOF — cannot verify dispatch"
    )


def _make_indicator(callbacks):
    from target.recording.indicator import SubprocessTkIndicator

    return SubprocessTkIndicator("review", None, callbacks)


def test_reader_thread_survives_raising_pause_callback():
    """Real recording_state_invalid from pause must not kill the reader:
    the next report (an assertion submission) is consumed and acked."""
    recorder = _DispatchRecorder(_idle_session())
    double = _ProcessDouble()
    try:
        _run(
            _make_indicator(recorder.callbacks()),
            double,
            [
                {"callback": "pause", "payload": None},
                {"callback": "add_assertion", "payload": {"requestId": "r1"}},
            ],
        )
        assert recorder.calls.count("pause") == 1
        assert recorder.calls.count("add_assertion") == 1, (
            "a raising pause callback killed the reader thread — the next "
            "report was never dispatched, leaving a live-looking orphan "
            "recorder window with dead buttons and unacked forms"
        )
        ack = double.acks.get(timeout=2)
        assert ack["requestId"] == "r1"
        assert ack["ok"] is False
    finally:
        double.close()


def test_reader_thread_survives_raising_stop_callback():
    """The Stop report itself raising (session just settled by the watchdog)
    must not prevent a later report from being consumed and acked."""
    recorder = _DispatchRecorder(_idle_session())
    double = _ProcessDouble()
    try:
        _run(
            _make_indicator(recorder.callbacks()),
            double,
            [
                {"callback": "stop", "payload": None},
                {"callback": "add_assertion", "payload": {"requestId": "r2"}},
            ],
        )
        assert recorder.calls.count("stop") == 1
        assert recorder.calls.count("add_assertion") == 1, (
            "a raising stop callback killed the reader thread — the "
            "assertion report was never consumed and its form would hang "
            "without an ack forever"
        )
        ack = double.acks.get(timeout=2)
        assert ack["requestId"] == "r2"
        assert ack["ok"] is False
    finally:
        double.close()


def test_healthy_callbacks_drive_real_session_transitions_in_order():
    """Happy path: reports dispatch in order and drive real session state
    (RECORDING → PAUSED → RECORDING) — nothing swallowed."""
    session = _recording_session()
    recorder = _DispatchRecorder(session)
    double = _ProcessDouble()
    try:
        _run(
            _make_indicator(recorder.callbacks()),
            double,
            [
                {"callback": "pause", "payload": None},
                {"callback": "resume", "payload": None},
            ],
        )
        assert recorder.calls == ["pause", "resume"]
        assert session.state.name == "RECORDING", (
            "resume after pause must return the session to RECORDING"
        )
    finally:
        double.close()


def test_assertion_failure_ack_reaches_form_with_request_id():
    """A refused assertion save (real recording_state_invalid on a settled
    session) produces a failure ack carrying the form's requestId — the
    wizard keeps the form instead of hanging."""
    recorder = _DispatchRecorder(_idle_session())
    double = _ProcessDouble()
    try:
        _run(
            _make_indicator(recorder.callbacks()),
            double,
            [{"callback": "add_assertion", "payload": {"requestId": "r9"}}],
        )
        ack = double.acks.get(timeout=2)
        assert ack["requestId"] == "r9"
        assert ack["ok"] is False
        assert ack["error"], "failure ack must carry the refusal reason"
    finally:
        double.close()
