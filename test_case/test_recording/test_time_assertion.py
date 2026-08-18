# -*- coding: utf-8 -*-
"""A recorded time assertion must mean the replay's clock, not the recorder's.

Asserting that a log view shows "today" is only useful if "today" is resolved
when the replay runs. Freezing the recorded timestamp into the golden trace
would make the assertion pass exactly once, on the day it was recorded.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from agent.execution.expectations import (
    _ReplayClock,
    evaluate_control_text_contains_time,
    render_time_pattern,
)
from agent.execution.models import StepStatus
from agent.recording.compiler import compile_recording
from agent.recording.models import RecordedStep, ReplaySelector
from agent.recording.replay import _verifier_expectation
from target.recording.models import RawRecording, RecordingModelError
from target.recording.session import RecordingSession
from target.recording.models import CaptureScope

pytestmark = pytest.mark.unit

RECORDED_ON = datetime(2026, 8, 18, 10, 15, 0)
REPLAYED_ON = datetime(2027, 3, 2, 21, 40, 0)


@pytest.fixture(autouse=True)
def _restore_clock():
    yield
    _ReplayClock.set(None)


class _Expectation:
    def __init__(self, value):
        self.value = value


def _observation(text):
    return {
        "snapshot_id": "OBS-1",
        "controls": [{
            "target_id": "T0001", "automation_id": "logRow", "text": text,
        }],
    }


def _evaluate(pattern, text, *, now):
    _ReplayClock.set(lambda: now)
    return evaluate_control_text_contains_time(
        None,
        _Expectation({"match": {"automation_id": "logRow"}, "pattern": pattern}),
        _observation(text),
        None,
    )


def test_the_pattern_is_rendered_against_the_replay_clock():
    _ReplayClock.set(lambda: REPLAYED_ON)
    assert render_time_pattern("%Y-%m-%d") == "2027-03-02"


def test_a_row_showing_the_replay_date_passes():
    result = _evaluate("%Y-%m-%d", "2027-03-02 21:39:11  用户登录", now=REPLAYED_ON)
    assert result.status is StepStatus.PASSED
    assert result.expected == "2027-03-02"


def test_a_row_still_showing_the_recording_date_fails():
    """This is the bug the assertion exists to catch."""
    result = _evaluate("%Y-%m-%d", "2026-08-18 10:15:00  用户登录", now=REPLAYED_ON)
    assert result.status is StepStatus.FAILED
    assert result.expected == "2027-03-02"
    assert "2026-08-18" in result.actual


def test_the_same_assertion_passes_on_the_day_it_was_recorded():
    result = _evaluate("%Y-%m-%d", "2026-08-18 10:15:00  用户登录", now=RECORDED_ON)
    assert result.status is StepStatus.PASSED


def test_a_non_date_pattern_is_supported_too():
    result = _evaluate("%Y年%m月", "2027年03月 的升级日志", now=REPLAYED_ON)
    assert result.status is StepStatus.PASSED


def test_an_empty_pattern_fails_rather_than_matching_everything():
    result = _evaluate("", "anything", now=REPLAYED_ON)
    assert result.status is StepStatus.FAILED
    assert "strftime" in result.diagnostic


def test_a_malformed_expectation_value_fails_explicitly():
    _ReplayClock.set(lambda: REPLAYED_ON)
    result = evaluate_control_text_contains_time(
        None, _Expectation("not-a-dict"), _observation("x"), None,
    )
    assert result.status is StepStatus.FAILED
    assert "match, pattern" in result.diagnostic


def test_the_golden_verifier_carries_the_pattern_not_a_timestamp():
    step = RecordedStep(
        "step-0001", "gui.click", {},
        ReplaySelector(
            window={"processName": "EDRClient.exe"},
            control={"automationId": "logRow"},
        ),
    )
    expectation = _verifier_expectation(
        step, {"type": "text_contains_time", "expected": "%Y-%m-%d", "timeoutSeconds": 5.0},
    )
    assert expectation.type == "control_text_contains_time"
    assert expectation.value["pattern"] == "%Y-%m-%d"
    assert expectation.timeout_seconds == 5.0


def _scope():
    return CaptureScope("target", "windows_pywinauto", "EDRClient.exe", "^logo1$")


def test_the_recorder_accepts_a_time_assertion_and_stores_the_pattern():
    session = RecordingSession("REC", "flow", _scope())
    session.start()
    session.add_assertion({
        "assertion": "text_contains_time",
        "expected": "%Y-%m-%d",
        "automationId": "logRow",
    })
    event = session.recording().events[0]
    assert event.assertion["type"] == "text_contains_time"
    assert event.assertion["expected"] == "%Y-%m-%d"


def test_the_recorder_rejects_a_malformed_strftime_pattern():
    session = RecordingSession("REC", "flow", _scope())
    session.start()
    with pytest.raises(RecordingModelError) as exc:
        session.add_assertion({
            "assertion": "text_contains_time",
            "expected": 20260818,
            "automationId": "logRow",
        })
    assert exc.value.code == "recording_assertion_invalid"


def test_a_recorded_time_assertion_survives_compilation_unchanged():
    session = RecordingSession("REC", "flow", _scope())
    session.start()
    session.add_assertion({
        "assertion": "text_contains_time",
        "expected": "%Y-%m-%d",
        "automationId": "logRow",
    })
    result = compile_recording(session.recording())
    verifier = result.case.steps[0].verifiers[0]
    assert verifier["type"] == "text_contains_time"
    assert verifier["expected"] == "%Y-%m-%d"
    # No absolute timestamp may leak into the golden trace.
    assert "2026" not in result.golden_json()
