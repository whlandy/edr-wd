from __future__ import annotations

import ast
import json

import pytest
import jsonschema

from agent.recording.compiler import coalesce_events, compile_recording
from agent.recording.artifacts import write_compilation_artifacts
from agent.recording.generate_pytest import render_pytest
from agent.recording.models import GoldenTrace, RecordedTestCase, ReplayEvaluation, ReplaySelector
from agent.cli import main as cli_main
from target.recording.models import RawRecording, RecordingModelError

pytestmark = pytest.mark.unit


def _event(sequence: int, kind: str = "pointer_click", *, ms: int | None = None, target: dict | None = None, input_: dict | None = None, assertion: dict | None = None, causal_id: str | None = None) -> dict:
    return {
        "sequence": sequence,
        "wallTime": f"2026-08-17T00:00:0{sequence}Z",
        "monotonicMs": ms if ms is not None else sequence * 1000,
        "type": kind,
        "scope": {"target": "win", "backend": "windows_pywinauto", "processName": "EDRClient.exe", "windowTitle": "EDRClient"},
        "input": input_ or {},
        "observedTarget": target if target is not None else {
            "snapshotId": f"OBS-{sequence}", "targetId": f"T{sequence:04d}",
            "fingerprint": "sha256:button", "controlType": "Button",
            "automationId": "btnApply", "text": "应用", "protected": False,
        },
        "evidence": {}, "causalId": causal_id or f"cause-{sequence}", "assertion": assertion,
    }


def _recording(*events: dict) -> RawRecording:
    return RawRecording.from_dict({
        "schema": "edr.desktop-recording/v1",
        "sessionId": "REC-1",
        "name": "policy flow",
        "captureDiagnostics": {"droppedPackets": 0, "correlationErrorCount": 0},
        "events": list(events),
    })


def test_raw_schema_rejects_unknown_fields_and_non_contiguous_sequence():
    payload = _recording(_event(1)).to_dict();payload["extra"] = True
    with pytest.raises(RecordingModelError) as exc:
        RawRecording.from_dict(payload)
    assert exc.value.code == "unknown_field"
    with pytest.raises(RecordingModelError) as exc:
        _recording(_event(2))
    assert exc.value.code == "sequence_invalid"


def test_raw_models_reject_invalid_nested_types_even_without_json_schema():
    event = _event(1)
    event["causalId"] = 123
    with pytest.raises(RecordingModelError) as exc:
        _recording(event)
    assert exc.value.path == "event.causalId"

    event = _event(1)
    event["observedTarget"]["automationId"] = 123
    with pytest.raises(RecordingModelError) as exc:
        _recording(event)
    assert exc.value.path == "observedTarget.automation_id"


@pytest.mark.parametrize(
    ("diagnostics", "issue"),
    [
        ({"droppedPackets": 2, "correlationErrorCount": 0}, "recording_packets_dropped"),
        ({"droppedPackets": 0, "correlationErrorCount": 1}, "recording_correlation_errors"),
    ],
)
def test_capture_health_failures_make_the_trace_incomplete(diagnostics, issue):
    recording = _recording(_event(1))
    recording = RawRecording(
        recording.session_id, recording.name, recording.events, diagnostics,
    )

    result = compile_recording(recording)

    assert result.golden.status == "incomplete"
    assert issue in {item.code for item in result.issues}


def test_empty_recording_is_reviewable_but_never_ready_for_replay():
    result = compile_recording(_recording())

    assert result.case.steps == ()
    assert result.golden.status == "incomplete"
    assert [issue.code for issue in result.issues] == ["compile_recording_empty"]


def test_two_same_target_clicks_inside_threshold_compile_as_double_click():
    recording = _recording(
        _event(1, ms=100, causal_id="double-click-1"),
        _event(2, ms=450, causal_id="double-click-1"),
    )
    events = coalesce_events(recording.events)
    assert len(events) == 1 and events[0].type == "pointer_double_click"
    assert compile_recording(recording).case.steps[0].action_id == "pointer.double_click"


def test_nearby_independent_clicks_are_not_merged_without_shared_causality():
    recording = _recording(_event(1, ms=100), _event(2, ms=450))
    assert [event.type for event in coalesce_events(recording.events)] == [
        "pointer_click", "pointer_click",
    ]


@pytest.mark.parametrize(
    ("button", "action_id"),
    [("right", "pointer.right_click"), ("middle", "pointer.middle_click")],
)
def test_non_primary_click_preserves_button_semantics_without_recorded_coordinates(
    button, action_id,
):
    event = _event(1, input_={"button": button, "screenPoint": [400, 300]})

    step = compile_recording(_recording(event)).case.steps[0]

    assert step.action_id == action_id
    assert step.args == {}


def test_two_right_clicks_are_never_miscompiled_as_a_left_double_click():
    first = _event(
        1, ms=100, causal_id="right-double",
        input_={"button": "right", "screenPoint": [50, 40]},
    )
    second = _event(
        2, ms=300, causal_id="right-double",
        input_={"button": "right", "screenPoint": [50, 40]},
    )

    events = coalesce_events(_recording(first, second).events)

    assert [event.type for event in events] == ["pointer_click", "pointer_click"]
    assert [
        step.action_id for step in compile_recording(_recording(first, second)).case.steps
    ] == ["pointer.right_click", "pointer.right_click"]


def test_double_click_compilation_uses_the_recorded_platform_interval():
    first = _event(1, ms=100, causal_id="double-click-1")
    second = _event(2, ms=700, causal_id="double-click-1")
    first["evidence"] = {"doubleClickIntervalMs": 650}

    events = coalesce_events(_recording(first, second).events)

    assert len(events) == 1
    assert events[0].type == "pointer_double_click"


def test_double_click_evidence_spans_the_full_two_click_lifecycle():
    def capture(hex_digit: str) -> dict:
        return {
            "id": "CAP-" + hex_digit * 32,
            "sha256": "sha256:" + hex_digit * 64,
            "width": 800,
            "height": 600,
            "origin": [0, 0],
            "redacted": True,
            "redactions": [],
        }

    first = _event(1, ms=100, causal_id="double-click-1")
    second = _event(2, ms=300, causal_id="double-click-1")
    first["evidence"] = {
        "beforeCapture": capture("a"),
        "capture": capture("b"),
        "doubleClickIntervalMs": 500,
    }
    second["evidence"] = {
        "beforeCapture": capture("b"),
        "capture": capture("c"),
        "doubleClickIntervalMs": 500,
    }

    combined = coalesce_events(_recording(first, second).events)[0]

    assert combined.evidence["beforeCapture"]["id"] == "CAP-" + "a" * 32
    assert combined.evidence["capture"]["id"] == "CAP-" + "c" * 32


def test_window_transition_binds_to_preceding_action_by_causal_id():
    click = _event(1, causal_id="cause-open-dialog")
    transition = _event(
        2, "window_transition", causal_id="cause-open-dialog",
        input_={
            "kind": "opened", "processName": "EDRClient.exe",
            "titleRegex": "^Policy Dialog$", "timeoutSeconds": 15,
        },
    )
    transition["observedTarget"] = None

    result = compile_recording(_recording(click, transition))

    assert len(result.case.steps) == 1
    assert result.case.steps[0].verifiers[-1] == {
        "type": "window_open",
        "expected": {
            "exists": True, "processName": "EDRClient.exe",
            "titleRegex": "^Policy Dialog$",
        },
        "timeoutSeconds": 15,
    }


def test_unbound_window_transition_remains_visible_and_incomplete():
    transition = _event(
        1, "window_transition", causal_id="orphan",
        input_={"kind": "opened"},
    )
    transition["observedTarget"] = None
    result = compile_recording(_recording(transition))
    assert result.golden.status == "incomplete"
    assert result.case.steps[0].issues == ("compile_transition_unbound",)


def test_selector_does_not_persist_observation_local_ids_or_pid():
    result = compile_recording(_recording(_event(1)))
    encoded = result.golden_json()
    assert "snapshotId" not in encoded and "targetId" not in encoded
    assert '"pid"' not in encoded
    assert "btnApply" in encoded


def test_compiler_binds_an_explicit_application_profile_into_the_golden():
    result = compile_recording(
        _recording(_event(1)), profile="windows_hisec",
    )

    assert result.golden.environment["profile"] == "windows_hisec"


def test_unsupported_and_ambiguous_steps_remain_visible_and_incomplete():
    no_identity = {"snapshotId": "OBS-1", "targetId": "T0001", "fingerprint": "sha256:x", "protected": False}
    result = compile_recording(_recording(_event(1, "magic_event", target=no_identity)))
    assert result.golden.status == "incomplete"
    assert len(result.case.steps) == 1
    assert {x.code for x in result.issues} == {"compile_action_unsupported", "compile_selector_ambiguous"}


@pytest.mark.parametrize(("kind", "expected"), [("visible", False), ("text_equals", "")])
def test_assertion_preserves_explicit_falsy_expected(kind, expected):
    result = compile_recording(_recording(_event(1, "assertion", assertion={"type": kind, "expected": expected})))
    assert result.case.steps[0].verifiers[0]["expected"] == expected


def test_raw_evidence_rejects_unknown_or_unredacted_capture_metadata():
    event = _event(1)
    event["evidence"] = {"unexpected": True}
    with pytest.raises(RecordingModelError) as exc:
        _recording(event)
    assert exc.value.code == "unknown_field"

    event = _event(1)
    event["evidence"] = {"capture": {
        "id": "CAP-" + "1" * 32,
        "sha256": "sha256:" + "2" * 64,
        "width": 100,
        "height": 80,
        "origin": [0, 0],
        "redacted": False,
        "redactions": [],
    }}
    with pytest.raises(RecordingModelError) as exc:
        _recording(event)
    assert exc.value.path == "event.evidence.capture.redacted"


_DRAG_TARGET = {
    "snapshotId": "OBS-1", "targetId": "T0001", "fingerprint": "sha256:thumb",
    "controlType": "Thumb", "automationId": "sliderThumb", "text": "阈值",
    "rect": [100, 100, 140, 140], "protected": False,
}
_DRAG_INPUT = {
    "button": "left",
    "screenPoint": [110, 130],
    "endPoint": [305, 210],
    "durationSeconds": 0.4,
    "endTarget": {
        "controlType": "Slider", "automationId": "sliderTrack", "text": "",
        "ancestry": [], "fingerprint": "sha256:track", "rect": [300, 200, 400, 240],
    },
}


def _bound_assertion(sequence: int, causal_id: str, **overrides) -> dict:
    assertion = {"type": "visible", "expected": True, "timeoutSeconds": 5.0}
    assertion.update(overrides)
    return _event(sequence, "assertion", assertion=assertion, causal_id=causal_id)


@pytest.mark.parametrize(
    ("kind", "input_", "issue"),
    [
        ("scroll_commit", {"delta": -120}, "compile_scroll_verifier_required"),
        ("drag_commit", _DRAG_INPUT, "compile_drag_verifier_required"),
    ],
)
def test_scroll_and_drag_cannot_replay_without_an_explicit_result_verifier(kind, input_, issue):
    target = _DRAG_TARGET if kind == "drag_commit" else None
    result = compile_recording(_recording(_event(1, kind, target=target, input_=input_)))
    assert result.golden.status == "incomplete"
    assert result.case.steps[0].issues == (issue,)


@pytest.mark.parametrize(
    ("kind", "input_", "action_id"),
    [
        ("scroll_commit", {"delta": -120}, "pointer.scroll"),
        ("drag_commit", _DRAG_INPUT, "pointer.drag"),
    ],
)
def test_a_causally_bound_assertion_becomes_the_action_result_verifier(kind, input_, action_id):
    target = _DRAG_TARGET if kind == "drag_commit" else None
    result = compile_recording(_recording(
        _event(1, kind, target=target, input_=input_, causal_id="cause-drag"),
        _bound_assertion(2, "cause-drag"),
    ))
    assert result.golden.status == "ready"
    assert len(result.case.steps) == 1
    step = result.case.steps[0]
    assert step.action_id == action_id
    assert step.verifiers == ({"type": "visible", "expected": True, "timeoutSeconds": 5.0},)


def test_an_unbound_assertion_keeps_its_own_step_and_does_not_verify_the_scroll():
    result = compile_recording(_recording(
        _event(1, "scroll_commit", input_={"delta": -120}, causal_id="cause-scroll"),
        _bound_assertion(2, "cause-unrelated"),
    ))
    assert result.golden.status == "incomplete"
    assert len(result.case.steps) == 2
    assert result.case.steps[0].issues == ("compile_scroll_verifier_required",)
    assert result.case.steps[1].action_id is None


def test_drag_compiles_stable_anchors_and_never_replays_recorded_coordinates():
    result = compile_recording(_recording(
        _event(1, "drag_commit", target=_DRAG_TARGET, input_=_DRAG_INPUT, causal_id="cause-drag"),
        _bound_assertion(2, "cause-drag"),
    ))
    step = result.case.steps[0]
    assert result.golden.status == "ready"
    assert step.args == {"duration": 0.4}
    assert step.selector.control["anchor"] == {"relativePoint": [0.25, 0.75]}
    assert step.end_selector.control["automationId"] == "sliderTrack"
    assert step.end_selector.control["anchor"] == {"relativePoint": [0.05, 0.25]}
    serialized = json.dumps(result.case.to_dict())
    for coordinate in ("110", "130", "305", "210"):
        assert f'"{coordinate}"' not in serialized


@pytest.mark.parametrize(
    "end_target",
    [None, {"controlType": "Pane", "fingerprint": "sha256:x", "rect": [0, 0, 10, 10]}],
)
def test_a_drag_release_point_without_stable_identity_stays_incomplete(end_target):
    input_ = {**_DRAG_INPUT, "endTarget": end_target}
    result = compile_recording(_recording(
        _event(1, "drag_commit", target=_DRAG_TARGET, input_=input_, causal_id="cause-drag"),
        _bound_assertion(2, "cause-drag"),
    ))
    assert result.golden.status == "incomplete"
    assert "compile_selector_ambiguous" in result.case.steps[0].issues


def test_protected_text_requires_env_source_and_never_persists_plaintext():
    protected = {"snapshotId": "OBS-1", "targetId": "T0001", "fingerprint": "sha256:p", "controlType": "Edit", "automationId": "password", "protected": True}
    with pytest.raises(RecordingModelError) as exc:
        _recording(_event(1, "text_commit", target=protected, input_={"value": "secret-value"}))
    assert exc.value.code == "recording_secret_plaintext"
    good = compile_recording(_recording(_event(1, "text_commit", target=protected, input_={"textSource": {"kind": "env", "name": "EDR_SECRET"}})))
    assert good.golden.status == "ready" and good.case.steps[0].args["textSource"]["name"] == "EDR_SECRET"


def test_unknown_text_sensitivity_rejects_plaintext_before_artifact_persistence():
    unknown = {
        "snapshotId": "OBS-1", "targetId": "T0001", "fingerprint": "sha256:u",
        "controlType": "Edit", "automationId": "username", "protected": None,
    }
    with pytest.raises(RecordingModelError) as exc:
        _recording(_event(1, "text_commit", target=unknown, input_={"value": "possibly-secret"}))
    assert exc.value.code == "recording_secret_plaintext"


def test_compilation_is_byte_deterministic_and_golden_round_trips():
    recording = _recording(_event(1))
    one = compile_recording(recording, catalog_digest="sha256:abc")
    two = compile_recording(recording, catalog_digest="sha256:abc")
    assert one.case_json() == two.case_json()
    assert one.golden_json() == two.golden_json()
    parsed = GoldenTrace.from_dict(json.loads(one.golden_json()))
    assert parsed.to_dict() == one.golden.to_dict()
    parsed_case = RecordedTestCase.from_dict(one.case.to_dict())
    assert parsed_case.to_dict() == one.case.to_dict()


def test_replay_selector_rejects_unknown_nested_fields():
    with pytest.raises(RecordingModelError) as exc:
        ReplaySelector(
            window={"processName": "EDRClient.exe", "pid": 42},
            control={"automationId": "btnApply"},
        )
    assert exc.value.code == "unknown_field"


def test_golden_loader_rejects_disconnected_or_mismatched_step_graph():
    payload = compile_recording(_recording(_event(1))).golden.to_dict()
    payload["steps"]["orphan"] = {**payload["steps"]["step-0001"], "stepId": "orphan"}
    with pytest.raises(RecordingModelError) as exc:
        GoldenTrace.from_dict(payload)
    assert exc.value.code == "step_graph_invalid"


def test_golden_loader_rejects_invalid_verifier_before_replay_can_mutate_ui():
    payload = compile_recording(_recording(_event(1))).golden.to_dict()
    payload["steps"]["step-0001"]["verifiers"] = [{
        "type": "made_up", "expected": True,
    }]
    with pytest.raises(RecordingModelError) as exc:
        GoldenTrace.from_dict(payload)
    assert exc.value.code == "golden_verifier_unsupported"

    payload = compile_recording(_recording(_event(1))).golden.to_dict()
    payload["steps"]["step-0001"]["verifiers"] = [{
        "type": "checked", "expected": "true",
    }]
    with pytest.raises(RecordingModelError) as exc:
        GoldenTrace.from_dict(payload)
    assert exc.value.path.endswith(".expected")

    payload = compile_recording(_recording(_event(1))).golden.to_dict()
    payload["steps"]["step-0001"]["stepId"] = "different"
    with pytest.raises(RecordingModelError) as exc:
        GoldenTrace.from_dict(payload)
    assert exc.value.code == "step_graph_invalid"


def test_generated_pytest_is_valid_and_only_uses_public_replay_api():
    source = render_pytest("Policy Flow")
    ast.parse(source)
    assert "from agent.recording.replay import load_golden_trace, replay_golden_trace" in source
    assert "AtomicExecutor" not in source and "target_id" not in source


def test_task_success_cannot_ignore_assertion_cleanup_or_integrity():
    with pytest.raises(RecordingModelError) as exc:
        ReplayEvaluation(True, 2, 1, 1.0, 1.0)
    assert exc.value.code == "task_success_invalid"
    valid = ReplayEvaluation(True, 2, 2, 1.0, 1.0)
    assert valid.to_dict()["taskSuccess"] is True


def test_offline_compilation_writes_complete_rebuildable_artifact_set(tmp_path):
    recording = _recording(_event(1))
    result = compile_recording(recording, catalog_digest="sha256:catalog")
    artifacts = write_compilation_artifacts(tmp_path, recording, result)
    assert {p.name for p in artifacts.directory.iterdir()} == {
        "recording.json", "case.json", "golden-trace.json",
        "test_policy_flow.py", "compile-report.json",
    }
    loaded = GoldenTrace.from_dict(json.loads(artifacts.golden_trace.read_text()))
    assert loaded.to_dict() == result.golden.to_dict()
    ast.parse(artifacts.generated_test.read_text())


def test_record_compile_cli_does_not_require_target(tmp_path, capsys):
    source = tmp_path / "recording.json"
    source.write_text(json.dumps(_recording(_event(1)).to_dict()), encoding="utf-8")
    assert cli_main(["record", "compile", str(source), "--output-root", str(tmp_path / "out")]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "ready"
    assert (tmp_path / "out" / "policy_flow" / "golden-trace.json").exists()


def test_canonical_documents_validate_against_strict_json_schemas():
    schema_root = __import__("pathlib").Path(__file__).resolve().parents[1] / "schema"
    recording = _recording(_event(1))
    compilation = compile_recording(recording)
    evaluation = ReplayEvaluation(True, 1, 1, 1.0, 1.0)
    for document, schema_name in (
        (recording.to_dict(), "desktop-recording.schema.json"),
        (compilation.case.to_dict(), "desktop-recorded-case.schema.json"),
        (compilation.golden.to_dict(), "desktop-golden-trace.schema.json"),
        (evaluation.to_dict(), "desktop-replay-evaluation.schema.json"),
    ):
        schema = json.loads((schema_root / schema_name).read_text(encoding="utf-8"))
        jsonschema.validate(document, schema)


# ── scroll gestures compile as one action, not one per notch ─────────────────


_SCROLL_TARGET = {
    "snapshotId": "OBS-1", "targetId": "T0001", "fingerprint": "sha256:list",
    "controlType": "List", "automationId": "logList", "text": "操作日志",
    "protected": False,
}


def _scroll(sequence, delta=-120, ms=None, causal_id=None, target=None):
    return _event(
        sequence, "scroll_commit",
        ms=ms if ms is not None else sequence * 60,
        target=target if target is not None else _SCROLL_TARGET,
        input_={"delta": delta, "screenPoint": [400, 300]},
        causal_id=causal_id or f"cause-{sequence}",
    )


def test_one_wheel_gesture_compiles_into_one_scroll_step():
    recording = _recording(*[_scroll(i) for i in range(1, 11)])
    result = compile_recording(recording)
    steps = [s for s in result.case.steps if s.action_id == "pointer.scroll"]
    assert len(steps) == 1
    # Ten notches of -120 replay as one ten-click scroll.
    assert steps[0].args == {"clicks": -10}


def test_the_merged_gesture_keeps_the_last_notch_cause_so_a_verifier_binds():
    """A user binds the assertion after scrolling, i.e. to the final notch."""
    events = [_scroll(i) for i in range(1, 4)]
    events.append(_bound_assertion(4, "cause-3"))
    result = compile_recording(_recording(*events))
    assert result.golden.status == "ready"
    assert len(result.case.steps) == 1
    assert result.case.steps[0].verifiers[0]["type"] == "visible"


def test_reversing_direction_starts_a_new_gesture():
    recording = _recording(
        _scroll(1, delta=-120), _scroll(2, delta=-120),
        _scroll(3, delta=120), _scroll(4, delta=120),
    )
    steps = [s for s in compile_recording(recording).case.steps if s.action_id == "pointer.scroll"]
    assert [s.args["clicks"] for s in steps] == [-2, 2]


def test_a_pause_between_bursts_starts_a_new_gesture():
    recording = _recording(
        _scroll(1, ms=1000), _scroll(2, ms=1100),
        _scroll(3, ms=5000), _scroll(4, ms=5100),
    )
    steps = [s for s in compile_recording(recording).case.steps if s.action_id == "pointer.scroll"]
    assert [s.args["clicks"] for s in steps] == [-2, -2]


def test_content_changing_under_a_still_pointer_is_one_gesture():
    """Scrolling moves rows past the cursor; that is not a new gesture.

    A live capture hit exactly this: 67 notches from one flick resolved to five
    different controls as rows scrolled by, and keying on control identity
    shattered the flick into six steps.
    """
    rows = [
        {**_SCROLL_TARGET, "fingerprint": f"sha256:row{i}", "text": f"日志 {i}"}
        for i in range(1, 5)
    ]
    recording = _recording(*[
        _scroll(i, ms=i * 20, target=rows[i - 1]) for i in range(1, 5)
    ])
    steps = [s for s in compile_recording(recording).case.steps if s.action_id == "pointer.scroll"]
    assert len(steps) == 1


def test_scrolling_somewhere_else_starts_a_new_gesture():
    def _at(sequence, point, ms):
        return _event(
            sequence, "scroll_commit", ms=ms, target=_SCROLL_TARGET,
            input_={"delta": -120, "screenPoint": point},
            causal_id=f"cause-{sequence}",
        )

    recording = _recording(
        _at(1, [400, 300], 20), _at(2, [400, 300], 40),
        _at(3, [900, 300], 60), _at(4, [900, 300], 80),
    )
    steps = [s for s in compile_recording(recording).case.steps if s.action_id == "pointer.scroll"]
    assert len(steps) == 2


def test_a_small_pointer_drift_stays_one_gesture():
    def _at(sequence, point, ms):
        return _event(
            sequence, "scroll_commit", ms=ms, target=_SCROLL_TARGET,
            input_={"delta": -120, "screenPoint": point},
            causal_id=f"cause-{sequence}",
        )

    recording = _recording(_at(1, [400, 300], 20), _at(2, [402, 301], 40))
    steps = [s for s in compile_recording(recording).case.steps if s.action_id == "pointer.scroll"]
    assert len(steps) == 1


def test_a_single_notch_is_unchanged():
    result = compile_recording(_recording(_scroll(1)))
    step = result.case.steps[0]
    assert step.args == {"clicks": -1}
    assert "notches" not in step.args


def test_a_high_resolution_wheel_still_replays_as_movement():
    """RDP forwards trackpad scroll in units far below WHEEL_DELTA."""
    recording = _recording(*[
        _scroll(i, delta=-3, ms=i * 20) for i in range(1, 11)
    ])
    steps = [s for s in compile_recording(recording).case.steps if s.action_id == "pointer.scroll"]
    assert len(steps) == 1
    # -30 raw units is well under one notch, but the user did scroll.
    assert steps[0].args == {"clicks": -1}


def test_a_standard_wheel_keeps_its_notch_count():
    recording = _recording(*[_scroll(i, delta=-120, ms=i * 20) for i in range(1, 6)])
    steps = [s for s in compile_recording(recording).case.steps if s.action_id == "pointer.scroll"]
    assert steps[0].args == {"clicks": -5}


def test_an_upward_high_resolution_gesture_keeps_its_direction():
    recording = _recording(*[_scroll(i, delta=2, ms=i * 20) for i in range(1, 5)])
    steps = [s for s in compile_recording(recording).case.steps if s.action_id == "pointer.scroll"]
    assert steps[0].args == {"clicks": 1}


_LOG_ROW = {
    "snapshotId": "OBS-1", "targetId": "T0001", "fingerprint": "sha256:row",
    "controlType": "DataItem", "automationId": "", "text": "用户 admin : 更新语言设置",
    "protected": False,
    "ancestry": [
        {"automationId": "LogCenterWindow.pagedTable.tableView", "controlType": "Table"},
        {"automationId": "LogCenterWindow.pagedTable", "controlType": "Group"},
    ],
}


def test_a_scroll_targets_the_container_not_the_row_under_the_pointer():
    """A row's only identity is its text, which differs on the next run."""
    result = compile_recording(_recording(_scroll(1, target=_LOG_ROW)))
    control = result.case.steps[0].selector.control
    assert control == {
        "automationId": "LogCenterWindow.pagedTable.tableView",
        "controlType": "Table",
    }
    assert "用户 admin" not in json.dumps(result.case.to_dict(), ensure_ascii=False)


def test_a_scroll_without_a_scrollable_ancestor_falls_back_to_the_control():
    target = {**_LOG_ROW, "automationId": "logList", "ancestry": []}
    result = compile_recording(_recording(_scroll(1, target=target)))
    assert result.case.steps[0].selector.control["automationId"] == "logList"


def test_an_ancestor_without_an_automation_id_is_not_a_usable_container():
    target = {
        **_LOG_ROW,
        "ancestry": [{"controlType": "Table"}, {"automationId": "outer", "controlType": "Group"}],
    }
    result = compile_recording(_recording(_scroll(1, target=target)))
    # Group is not scrollable and the Table has no id: fall back to the row.
    assert result.case.steps[0].selector.control.get("name") == "用户 admin : 更新语言设置"


def test_clicks_are_unaffected_and_still_target_the_row(): 
    result = compile_recording(_recording(
        _event(1, "pointer_click", target=_LOG_ROW, input_={"button": "left"}),
    ))
    assert result.case.steps[0].selector.control["name"] == "用户 admin : 更新语言设置"
