"""End-to-end contracts for prefix and duration time assertions."""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

from agent.execution.expectations import (
    evaluate_control_text_time_range, evaluate_window_text_time_range,
)
from agent.execution.models import StepStatus
from agent.recording.compiler import compile_recording
from agent.recording.replay import _verifier_expectation
from target.recording.models import CaptureScope, RawCaptureEvent, RecordingModelError
from target.recording.session import RecordingSession
from target.time_assertions import matching_times, parse_time_range


pytestmark = pytest.mark.unit


class _Expectation:
    def __init__(self, value):
        self.value = value


def _session():
    session = RecordingSession(
        "REC", "time-range", CaptureScope("target", "windows_pywinauto", "EDRClient.exe", "^EDRClient$"),
    )
    session.start()
    return session


@pytest.mark.parametrize("actual,passes", [
    ("10:10:0", True), ("10:10:9", True),
    ("10:10:00", True), ("10:10:10", True), ("10:10:59", True),
    ("10:09:59", False), ("10:11:00", False), ("110:10:10", False),
    ("10:10:60", False),
])
def test_minute_prefix_boundaries(actual, passes):
    assert bool(matching_times(f"事件 {actual}", "10:10")) is passes


@pytest.mark.parametrize("actual,passes", [
    ("10:10:00", True), ("10:11:59", True), ("10:12:00", False),
    ("10:09:59", False),
])
def test_duration_overrides_prefix_precision(actual, passes):
    assert bool(matching_times(actual, "10:10", 120)) is passes


def test_second_precision_and_midnight_wrap():
    assert matching_times("10:10:10", "10:10:10") == ("10:10:10",)
    assert matching_times("10:10:0", "10:10:0") == ("10:10:0",)
    assert matching_times("10:10:00", "10:10:0") == ("10:10:00",)
    assert not matching_times("10:10:11", "10:10:10")
    assert matching_times("00:00:09", "23:59:50", 20)
    assert not matching_times("00:00:10", "23:59:50", 20)


def test_iso_timezone_offset_is_not_mistaken_for_event_clock():
    text = "2026-10-16T10:10:59+08:00"
    assert matching_times(text, "10:10") == ("10:10:59",)
    assert matching_times(text, "08:00") == ()
    assert matching_times("2026-10-16T10:10:59-05:00", "05:00") == ()


@pytest.mark.parametrize("prefix,duration", [
    ("10:60", None), ("25:10", None), ("10:10:60", None),
    ("10:10x", None), ("10:10", 0), ("10:10", -1),
    ("10:10", True), ("10:10", 86401),
])
def test_invalid_ranges_are_rejected(prefix, duration):
    with pytest.raises(ValueError):
        parse_time_range(prefix, duration)


def test_record_compile_and_replay_expectation_preserve_duration():
    session = _session()
    session.add_assertion({
        "assertion": "text_time_range", "expected": "10:10",
        "durationSeconds": "120", "automationId": "logRow", "name": "录制时 10:10:10",
    })
    result = compile_recording(session.recording())
    assert result.golden.status == "ready"
    schema_root = Path(__file__).resolve().parents[1] / "schema"
    for document, schema_name in (
        (session.recording().to_dict(), "desktop-recording.schema.json"),
        (result.case.to_dict(), "desktop-recorded-case.schema.json"),
        (result.golden.to_dict(), "desktop-golden-trace.schema.json"),
    ):
        jsonschema.validate(document, json.loads((schema_root / schema_name).read_text()))
    step = result.golden.steps[0]
    assert step.verifiers[0]["durationSeconds"] == 120
    expectation = _verifier_expectation(step, step.verifiers[0])
    assert expectation.type == "control_text_time_range"
    assert expectation.value["match"] == {"automation_id": "logRow"}
    observation = {"snapshot_id": "OBS-2", "controls": [
        {"automation_id": "logRow", "text": "回放时 10:11:59"},
    ]}
    assert evaluate_control_text_time_range(None, expectation, observation, None).status is StepStatus.PASSED
    observation["controls"][0]["text"] = "回放时 10:12:00"
    assert evaluate_control_text_time_range(None, expectation, observation, None).status is StepStatus.FAILED


def test_recorded_unpadded_second_prefix_replays_with_duration():
    session = _session()
    session.add_assertion({
        "assertion": "text_time_range", "expected": "10:10:0",
        "durationSeconds": 2, "automationId": "logTime", "name": "事件时间",
    })
    golden = compile_recording(session.recording()).golden
    assert golden.status == "ready"
    expectation = _verifier_expectation(golden.steps[0], golden.steps[0].verifiers[0])
    observation = {"snapshot_id": "OBS-2", "controls": [
        {"automation_id": "logTime", "text": "事件时间", "value": "10:10:1"},
    ]}
    assert evaluate_control_text_time_range(None, expectation, observation, None).status is StepStatus.PASSED
    observation["controls"][0]["value"] = "10:10:2"
    assert evaluate_control_text_time_range(None, expectation, observation, None).status is StepStatus.FAILED


def test_window_range_needs_no_control_identity_and_searches_tree():
    session = _session()
    session.add_assertion({"assertion": "window_text_time_range", "expected": "10:10"})
    step = compile_recording(session.recording()).golden.steps[0]
    assert step.selector.control == {}
    expectation = _verifier_expectation(step, step.verifiers[0])
    assert expectation.type == "window_text_time_range"
    observation = {"snapshot_id": "OBS-2", "windows": [{"title": "日志中心"}],
                   "controls": [{"text": "10:10:59 用户登录"}]}
    assert evaluate_window_text_time_range(None, expectation, observation, None).status is StepStatus.PASSED


@pytest.mark.parametrize("assertion", [
    {"assertion": "window_text_time_range", "expected": "10:10"},
    {"assertion": "text_time_range", "expected": "10:10", "automationId": "logTime"},
])
def test_hotkey_authored_assertion_replays_in_its_actual_product_window(assertion):
    session = _session()
    hotkey = RawCaptureEvent(
        sequence=1, wall_time="2026-10-19T00:00:00Z", monotonic_ms=1,
        type="key_command", scope=session.scope,
        input={"key": "A", "modifiers": ["CTRL", "SHIFT"]},
        evidence={"window": {"title": "日志中心", "processName": "EDRClient.exe",
                             "handle": 101, "rootAutomationId": "LogCenterRoot"}},
    )
    assert session.ingest(hotkey) is False
    session.add_assertion(assertion)

    golden = compile_recording(session.recording()).golden

    assert golden.status == "ready"
    assert golden.steps[0].selector.window == {
        "processName": "EDRClient.exe", "rootAutomationId": "LogCenterRoot",
    }


def test_cli_assertion_editor_uses_last_recorded_product_window():
    session = _session()
    click = RawCaptureEvent(
        sequence=1, wall_time="2026-10-19T00:00:00Z", monotonic_ms=1,
        type="pointer_click", scope=session.scope,
        input={"button": "left", "screenPoint": [20, 20]},
        evidence={"window": {"title": "日志中心", "processName": "EDRClient.exe"}},
        causal_id="CAUSE-1",
    )
    assert session.ingest(click) is True
    session.request_assertion_editor()
    session.add_assertion({"assertion": "window_text_time_range", "expected": "10:10"})

    assert session.recording().events[-1].evidence["window"]["title"] == "日志中心"


def test_cli_assertion_editor_uses_newly_opened_window_before_first_click():
    session = _session()
    transition = RawCaptureEvent(
        sequence=1, wall_time="2026-10-19T00:00:00Z", monotonic_ms=1,
        type="window_transition", scope=session.scope,
        input={"kind": "opened", "title": "日志中心", "processName": "EDRClient.exe"},
        causal_id="CAUSE-1",
    )
    assert session.ingest(transition) is True
    session.request_assertion_editor()
    session.add_assertion({"assertion": "window_text_time_range", "expected": "10:10"})

    golden = compile_recording(session.recording()).golden
    assert golden.steps[-1].selector.window == {
        "processName": "EDRClient.exe", "titleRegex": "^日志中心$",
    }


def test_time_range_reads_value_when_text_is_a_static_label():
    session = _session()
    session.add_assertion({"assertion": "text_time_range", "expected": "10:10",
                           "automationId": "logTime", "name": "事件时间"})
    step = compile_recording(session.recording()).golden.steps[0]
    expectation = _verifier_expectation(step, step.verifiers[0])
    observation = {"snapshot_id": "OBS-2", "controls": [
        {"automation_id": "logTime", "text": "事件时间", "value": "10:10:59"},
    ]}
    assert evaluate_control_text_time_range(None, expectation, observation, None).status is StepStatus.PASSED
    observation["controls"][0]["value"] = "10:11:00"
    assert evaluate_control_text_time_range(None, expectation, observation, None).status is StepStatus.FAILED


def test_control_time_range_rejects_duplicate_component_id_even_if_one_matches():
    expectation = _Expectation({"match": {"automation_id": "logTime"}, "prefix": "10:10"})
    observation = {"snapshot_id": "OBS-2", "controls": [
        {"automation_id": "logTime", "text": "10:10:59"},
        {"automation_id": "logTime", "text": "10:11:00"},
    ]}

    result = evaluate_control_text_time_range(None, expectation, observation, None)

    assert result.status is StepStatus.FAILED
    assert "not uniquely found" in result.diagnostic


def test_invalid_duration_is_rejected_before_recording():
    session = _session()
    with pytest.raises(RecordingModelError, match="time_range_invalid_duration"):
        session.add_assertion({"assertion": "window_text_time_range", "expected": "10:10",
                               "durationSeconds": "0"})
