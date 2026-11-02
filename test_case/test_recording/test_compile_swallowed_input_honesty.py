"""Swallowed-input honesty: the compiler must surface capture-side losses.

The recording document persists two diagnostics the capture path itself
documents as "never silently dropped" (``target/recording/session.py``):
``recorderUiEvents`` — input the user aimed at the target but that the
recorder's own always-on-top panel swallowed — and
``unidentifiedTargetEvents`` — clicks that landed on nothing the
application can name. The compiler already turns ``droppedPackets`` and
``correlationErrorCount`` into compile issues that force ``incomplete``
(``test_phase0_recording.py::test_capture_health_failures_make_the_trace_incomplete``);
the other two counters currently reach no issue at all, so a recording
that missed part of what the user did can compile ``ready``. This pins
the same honesty contract for those counters.
"""

import pytest

from agent.recording.compiler import compile_recording
from target.recording.models import RawRecording


def _recording_with_diagnostics(diagnostics: dict) -> RawRecording:
    event = {
        "sequence": 1,
        "wallTime": "2026-10-30T08:00:01Z",
        "monotonicMs": 1000,
        "type": "pointer_click",
        "scope": {"target": "win", "backend": "windows_pywinauto",
                  "processName": "EDRClient.exe", "windowTitle": "^logo1$"},
        "input": {},
        "observedTarget": {
            "snapshotId": "OBS-1", "targetId": "T0001",
            "fingerprint": "sha256:button", "controlType": "Button",
            "automationId": "btnApply", "text": "应用", "protected": False,
        },
        "evidence": {}, "causalId": "cause-1", "assertion": None,
    }
    return RawRecording.from_dict({
        "schema": "edr.desktop-recording/v1",
        "sessionId": "REC-1",
        "name": "swallowed input",
        "captureDiagnostics": diagnostics,
        "events": [event],
    })


@pytest.mark.parametrize(
    ("diagnostics", "issue"),
    [
        ({"droppedPackets": 0, "correlationErrorCount": 0,
          "outOfScopeEvents": 0, "recorderUiEvents": 1,
          "unidentifiedTargetEvents": 0},
         "recording_recorder_ui_events"),
        ({"droppedPackets": 0, "correlationErrorCount": 0,
          "outOfScopeEvents": 0, "recorderUiEvents": 0,
          "unidentifiedTargetEvents": 2},
         "recording_unidentified_target_events"),
    ],
)
def test_swallowed_input_makes_the_trace_incomplete(diagnostics, issue):
    """A recording whose capture swallowed user input must not compile ready."""
    result = compile_recording(_recording_with_diagnostics(diagnostics))
    assert result.golden.status == "incomplete"
    assert issue in {item.code for item in result.issues}


def test_zero_swallowed_input_stays_ready():
    """The guard must not fire on a clean capture (regression against
    turning the counters on unconditionally)."""
    result = compile_recording(_recording_with_diagnostics({
        "droppedPackets": 0, "correlationErrorCount": 0,
        "outOfScopeEvents": 0, "recorderUiEvents": 0,
        "unidentifiedTargetEvents": 0,
    }))
    assert result.golden.status == "ready"
