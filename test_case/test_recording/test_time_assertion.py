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


# ── window-scoped assertions need no control identity ────────────────────────


from agent.execution.expectations import evaluate_window_text_contains_time


def _window_observation(text, control_text=""):
    return {
        "snapshot_id": "OBS-1",
        "windows": [{"title": text}],
        "controls": [{"target_id": "T0001", "text": control_text}],
    }


class _Value:
    def __init__(self, value):
        self.value = value


def test_a_window_time_assertion_matches_the_window_title():
    _ReplayClock.set(lambda: REPLAYED_ON)
    result = evaluate_window_text_contains_time(
        None, _Value("%Y-%m-%d"), _window_observation("日志 2027-03-02"), None,
    )
    assert result.status is StepStatus.PASSED


def test_a_window_time_assertion_also_searches_the_control_tree():
    _ReplayClock.set(lambda: REPLAYED_ON)
    result = evaluate_window_text_contains_time(
        None, _Value("%Y-%m-%d"),
        _window_observation("日志中心", control_text="2027-03-02 21:39  用户登录"),
        None,
    )
    assert result.status is StepStatus.PASSED


def test_a_window_time_assertion_fails_when_only_the_recorded_date_is_present():
    _ReplayClock.set(lambda: REPLAYED_ON)
    result = evaluate_window_text_contains_time(
        None, _Value("%Y-%m-%d"),
        _window_observation("日志中心", control_text="2026-08-18 10:15  用户登录"),
        None,
    )
    assert result.status is StepStatus.FAILED
    assert result.expected == "2027-03-02"


@pytest.mark.parametrize(
    "assertion_type", ["window_text_contains", "window_text_contains_time"],
)
def test_a_window_assertion_is_accepted_without_any_control_identity(assertion_type):
    """The user knows what the screen must show, not which widget shows it."""
    session = RecordingSession("REC", "flow", _scope())
    session.start()
    expected = "%Y-%m-%d" if assertion_type.endswith("_time") else "管理员"

    session.add_assertion({"assertion": assertion_type, "expected": expected})

    event = session.recording().events[0]
    assert event.assertion["type"] == assertion_type
    assert event.observed_target is None


def test_a_control_assertion_still_requires_an_identity():
    session = RecordingSession("REC", "flow", _scope())
    session.start()
    with pytest.raises(RecordingModelError) as exc:
        session.add_assertion({"assertion": "text_equals", "expected": "管理员"})
    assert exc.value.code == "recording_target_unresolved"


def test_a_window_assertion_compiles_to_a_window_only_selector():
    session = RecordingSession("REC", "flow", _scope())
    session.start()
    session.add_assertion({
        "assertion": "window_text_contains_time", "expected": "%Y-%m-%d",
    })
    result = compile_recording(session.recording())
    step = result.case.steps[0]
    assert result.golden.status == "ready"
    assert step.selector.control == {}
    assert step.verifiers[0]["type"] == "window_text_contains_time"


def test_the_window_time_verifier_maps_to_a_pattern_valued_expectation():
    step = RecordedStep(
        "step-0001", None, {},
        ReplaySelector(window={"processName": "EDRClient.exe"}, control={}),
        verifiers=({"type": "window_text_contains_time", "expected": "%Y-%m-%d"},),
    )
    expectation = _verifier_expectation(step, step.verifiers[0])
    assert expectation.type == "window_text_contains_time"
    assert expectation.value == "%Y-%m-%d"
