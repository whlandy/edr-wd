from __future__ import annotations

import base64

import pytest

from action_dispatcher import ActionReceipt
from agent import cli
from agent.execution import (
    AtomicExecutor,
    BackendUnavailable,
    CaseOutcome,
    StepMaterializationError,
    StepStatus,
)
from agent.recording.models import GoldenTrace, RecordedStep, ReplaySelector
from agent.recording.models import canonical_json
from agent.recording.replay import ReplayRuntime, golden_to_test_case, replay_golden_trace
from agent.recording.mcp_runtime import MCPActionDispatch, MCPObservationProvider
from agent.recording.visual import SafeVisualResolver, VisualCandidate
from agent.trace import EventType, TraceStore
from target.recording.models import RecordingModelError
from target.action_catalog import CATALOG_VERSION, catalog_digest

pytestmark = pytest.mark.unit
VISUAL_DIGEST = "sha256:" + "a" * 64


class _Observations:
    def __init__(self, controls):
        self.controls = controls
        self.counter = 0
        self.snapshots = {}

    def refresh(self):
        self.counter += 1
        snapshot_id = f"OBS-FRESH-{self.counter}"
        controls = []
        for index, source in enumerate(self.controls, start=1):
            item = dict(source)
            item["target_id"] = f"T{self.counter:02d}{index:02d}"
            controls.append(item)
        self.snapshots[snapshot_id] = {
            "snapshot_id": snapshot_id,
            "controls": controls,
            "windows": [{"process_name": "EDRClient.exe", "title": "EDRClient"}],
            "active_window": {
                "process_name": "EDRClient.exe", "title": "EDRClient",
                "rect": [0, 0, 800, 600],
            },
            "screenshot_scope": "window",
        }
        return snapshot_id

    def latest_snapshot_id(self):
        return f"OBS-FRESH-{self.counter}" if self.counter else None

    def get_snapshot(self, snapshot_id):
        return self.snapshots[snapshot_id]


def _selector():
    return ReplaySelector(
        window={"processName": "EDRClient.exe", "titleRegex": "^EDRClient$"},
        control={"automationId": "btnApply", "controlType": "Button", "name": "应用"},
    )


def _golden(*steps, status="ready", digest="sha256:catalog", cleanup=()):
    return GoldenTrace(
        name="policy flow",
        source_recording={"schema": "edr.desktop-recording/v1", "sha256": "sha256:raw"},
        catalog={"version": "1.0.0", "digest": digest},
        environment={"backend": "windows_pywinauto", "target": "win-dev"},
        steps=tuple(steps),
        status=status,
        cleanup=tuple(cleanup),
    )


def _runtime(controls):
    observations = _Observations(controls)
    calls = []

    def dispatch(**kwargs):
        calls.append(kwargs)
        return ActionReceipt.from_ok(
            action_id=kwargs["action_id"],
            action_code=kwargs["action_code"],
            request_id=kwargs["request_id"],
            result={"ok": True},
        )

    executor = AtomicExecutor(dispatch=dispatch, observation_provider=observations)
    return ReplayRuntime(executor, "1.0.0", "sha256:catalog"), observations, calls


def test_replay_materializes_a_fresh_target_ref_for_every_step():
    step1 = RecordedStep("step-0001", "gui.click", {}, _selector())
    step2 = RecordedStep("step-0002", "gui.click", {}, _selector())
    runtime, observations, calls = _runtime([{
        "process_name": "EDRClient.exe",
        "window_title": "EDRClient",
        "automation_id": "btnApply",
        "control_type": "Button",
        "text": "应用",
        "fingerprint": "sha256:current",
    }])

    result = replay_golden_trace(runtime, _golden(step1, step2))

    assert result.case_result.outcome is CaseOutcome.PASSED
    assert len(calls) == 2
    snapshot_ids = [call["target_ref"]["snapshot_id"] for call in calls]
    assert snapshot_ids[0] != snapshot_ids[1]
    assert all(value.startswith("OBS-FRESH-") for value in snapshot_ids)
    assert calls[0]["args"]["automation_id"] == "btnApply"
    assert calls[0]["args"]["expected_process_name"] == "EDRClient.exe"
    assert result.evaluation.semantic_resolution_rate == 1.0
    assert result.evaluation.task_success is True
    assert result.task_success is True
    assert result.summary["outcome"] == "passed"


@pytest.mark.parametrize("action_id", ["pointer.right_click", "pointer.middle_click"])
def test_non_primary_click_uses_the_fresh_semantic_target_center(action_id):
    runtime, _, calls = _runtime([{
        "process_name": "EDRClient.exe",
        "window_title": "EDRClient",
        "automation_id": "btnApply",
        "control_type": "Button",
        "text": "应用",
        "rect": [100, 200, 220, 260],
    }])

    result = replay_golden_trace(
        runtime,
        _golden(RecordedStep("step-0001", action_id, {}, _selector())),
    )

    assert result.task_success is True
    assert calls[0]["args"] == {
        "x": 160,
        "y": 230,
        "expected_process_name": "EDRClient.exe",
    }
    assert calls[0]["target_ref"]["snapshot_id"].startswith("OBS-FRESH-")


def test_replay_ambiguous_selector_blocks_without_dispatching():
    control = {
        "process_name": "EDRClient.exe", "window_title": "EDRClient",
        "automation_id": "btnApply", "control_type": "Button", "text": "应用",
    }
    runtime, _, calls = _runtime([control, control])
    result = replay_golden_trace(
        runtime, _golden(RecordedStep("step-0001", "gui.click", {}, _selector()))
    )
    assert calls == []
    assert result.case_result.step_results[0].status is StepStatus.BLOCKED
    assert result.case_result.step_results[0].error["code"] == "replay_target_ambiguous"
    assert result.evaluation.task_success is False
    assert result.evaluation.path_fidelity == 0.0


def test_assertion_only_step_observes_without_dispatching():
    assertion = RecordedStep(
        "step-0001", None, {}, _selector(),
        verifiers=({"type": "visible", "expected": True},),
    )
    runtime, _, calls = _runtime([{
        "process_name": "EDRClient.exe", "window_title": "EDRClient",
        "automation_id": "btnApply", "control_type": "Button", "text": "应用",
    }])
    result = replay_golden_trace(runtime, _golden(assertion))
    assert calls == []
    assert result.case_result.outcome is CaseOutcome.PASSED
    assert result.evaluation.assertion_pass_rate == 1.0


def test_window_assertion_object_preserves_existence_match_and_timeout():
    assertion = RecordedStep(
        "step-0001", None, {},
        ReplaySelector(
            window={"processName": "EDRClient.exe", "titleRegex": "^EDRClient$"},
            control={},
        ),
        verifiers=({
            "type": "window_open",
            "expected": {"exists": False, "titleRegex": "^Policy Dialog$"},
            "timeoutSeconds": 15,
        },),
    )

    expectation = golden_to_test_case(_golden(assertion)).steps[0].expectations[0]

    assert expectation.type == "window_closed"
    assert expectation.value == {
        "process_name": "EDRClient.exe", "title_regex": "^Policy Dialog$",
    }
    assert expectation.timeout_seconds == 15


def test_replay_rejects_incomplete_and_catalog_mismatch_before_execution():
    runtime, _, calls = _runtime([])
    with pytest.raises(RecordingModelError) as exc:
        replay_golden_trace(runtime, _golden(status="incomplete"))
    assert exc.value.code == "golden_trace_incomplete"
    with pytest.raises(RecordingModelError) as exc:
        replay_golden_trace(runtime, _golden(digest="sha256:old"))
    assert exc.value.code == "golden_catalog_mismatch"
    assert calls == []


def test_recorded_cleanup_runs_through_same_executor_and_gates_success():
    main = RecordedStep("step-0001", "gui.click", {}, _selector())
    cleanup = RecordedStep("cleanup-0001", "gui.click", {}, _selector())
    runtime, _, calls = _runtime([{
        "process_name": "EDRClient.exe", "window_title": "EDRClient",
        "automation_id": "btnApply", "control_type": "Button", "text": "应用",
    }])
    result = replay_golden_trace(runtime, _golden(main, cleanup=(cleanup,)))
    assert len(calls) == 2
    assert result.cleanup_result is not None
    assert result.cleanup_result.outcome is CaseOutcome.PASSED
    assert result.evaluation.cleanup_passed is True
    assert result.evaluation.task_success is True


def test_replay_trace_attributes_cleanup_steps_to_the_cleanup_case(tmp_path):
    main = RecordedStep("step-0001", "gui.click", {}, _selector())
    cleanup = RecordedStep("cleanup-0001", "gui.click", {}, _selector())
    runtime, _, _ = _runtime([{
        "process_name": "EDRClient.exe", "window_title": "EDRClient",
        "automation_id": "btnApply", "control_type": "Button", "text": "应用",
    }])
    trace_store = TraceStore(tmp_path)
    trace_store.open(trace_id="TR-GOLDEN")
    object.__setattr__(runtime, "trace_store", trace_store)

    result = replay_golden_trace(runtime, _golden(main, cleanup=(cleanup,)))

    cleanup_events = [
        event for event in trace_store.read_events()
        if event.step_id == "cleanup-0001"
        and event.event_type is EventType.STEP_COMPLETED
    ]
    assert cleanup_events[0].case_id == result.cleanup_result.case_id
    events = trace_store.read_events()
    assert events[-1].event_type is EventType.TRACE_COMPLETED
    lifecycle = [event.event_type for event in events]
    assert lifecycle.index(EventType.STEP_COMPLETED) < lifecycle.index(EventType.CLEANUP_STARTED)
    assert lifecycle.index(EventType.CLEANUP_STARTED) < lifecycle.index(EventType.CLEANUP_COMPLETED)
    cleanup_started = next(
        event for event in events
        if event.event_type is EventType.STEP_STARTED and event.step_id == "cleanup-0001"
    )
    assert cleanup_started.case_id == result.cleanup_result.case_id
    assert result.evaluation_path == trace_store.root / "evaluation.json"
    persisted = __import__("json").loads(result.evaluation_path.read_text())
    assert persisted == result.evaluation.to_dict()


def test_semantic_first_visual_fallback_is_window_bounded_and_reported():
    selector = ReplaySelector(
        window={"processName": "EDRClient.exe", "titleRegex": "^EDRClient$"},
        control={"automationId": "missing"},
        visual={
            "template": "assets/apply.png", "redacted": True,
            "elementSha256": VISUAL_DIGEST,
            "relativePoint": [0.5, 0.5],
        },
    )
    runtime, _, calls = _runtime([])
    object.__setattr__(runtime, "replay_mode", "semantic_first")
    object.__setattr__(runtime, "visual_resolver", SafeVisualResolver(
        lambda _template, _observation: [VisualCandidate((100, 120, 200, 160), 0.98)]
    ))
    result = replay_golden_trace(
        runtime, _golden(RecordedStep("step-0001", "gui.click", {}, selector))
    )
    assert calls[0]["action_id"] == "pointer.click_window"
    assert calls[0]["target_ref"] is None
    assert calls[0]["args"]["x"] == 150
    assert calls[0]["args"]["y"] == 140
    assert result.evaluation.visual_fallback_count == 1
    assert result.evaluation.semantic_resolution_rate == 0.0


def test_visual_fallback_refuses_ambiguous_candidates_without_clicking():
    selector = ReplaySelector(
        window={"processName": "EDRClient.exe", "titleRegex": "^EDRClient$"},
        control={"automationId": "missing"},
        visual={
            "template": "assets/apply.png", "redacted": True,
            "elementSha256": VISUAL_DIGEST,
        },
    )
    runtime, _, calls = _runtime([])
    object.__setattr__(runtime, "replay_mode", "semantic_first")
    object.__setattr__(runtime, "visual_resolver", SafeVisualResolver(
        lambda _template, _observation: [
            VisualCandidate((100, 100, 150, 150), 0.98),
            VisualCandidate((300, 100, 350, 150), 0.96),
        ]
    ))
    result = replay_golden_trace(
        runtime, _golden(RecordedStep("step-0001", "gui.click", {}, selector))
    )
    assert calls == []
    assert result.case_result.step_results[0].error["code"] == "visual_match_ambiguous"


def test_visual_fallback_refuses_template_that_fails_digest_verification():
    selector = ReplaySelector(
        window={"processName": "EDRClient.exe", "titleRegex": "^EDRClient$"},
        control={"automationId": "missing"},
        visual={
            "template": "assets/apply.png", "redacted": True,
            "elementSha256": VISUAL_DIGEST,
        },
    )
    runtime, _, calls = _runtime([])
    object.__setattr__(runtime, "replay_mode", "semantic_first")
    object.__setattr__(runtime, "visual_resolver", SafeVisualResolver(
        lambda _template, _observation: [VisualCandidate((20, 30, 120, 70), 0.99)],
        template_verifier=lambda _template, _visual: False,
    ))

    result = replay_golden_trace(
        runtime, _golden(RecordedStep("step-0001", "gui.click", {}, selector))
    )

    assert calls == []
    assert result.case_result.step_results[0].error["code"] == "visual_template_integrity_failed"


def test_visual_only_mode_does_not_disguise_visual_hit_as_semantic_resolution():
    selector = ReplaySelector(
        window={"processName": "EDRClient.exe", "titleRegex": "^EDRClient$"},
        control={"automationId": "btnApply", "controlType": "Button", "name": "应用"},
        visual={
            "template": "assets/apply.png", "redacted": True,
            "elementSha256": VISUAL_DIGEST,
        },
    )
    runtime, _, calls = _runtime([{
        "process_name": "EDRClient.exe", "window_title": "EDRClient",
        "automation_id": "btnApply", "control_type": "Button", "text": "应用",
    }])
    object.__setattr__(runtime, "replay_mode", "visual_only")
    object.__setattr__(runtime, "visual_resolver", SafeVisualResolver(
        lambda _template, _observation: [VisualCandidate((20, 30, 120, 70), 0.99)]
    ))
    result = replay_golden_trace(
        runtime, _golden(RecordedStep("step-0001", "gui.click", {}, selector))
    )
    assert calls[0]["action_id"] == "pointer.click_window"
    assert result.evaluation.visual_fallback_count == 1
    assert result.evaluation.semantic_resolution_rate == 0.0


def test_replay_missing_secret_blocks_without_dispatch_or_plaintext(monkeypatch):
    monkeypatch.delenv("EDR_WD_SECRET_1", raising=False)
    step = RecordedStep(
        "step-0001", "gui.type_text",
        {"textSource": {"kind": "env", "name": "EDR_WD_SECRET_1"}},
        _selector(),
    )
    runtime, _, calls = _runtime([{
        "process_name": "EDRClient.exe", "window_title": "EDRClient",
        "automation_id": "btnApply", "control_type": "Button", "text": "应用",
    }])
    result = replay_golden_trace(runtime, _golden(step))
    assert calls == []
    assert result.case_result.step_results[0].error["code"] == "golden_secret_missing"


class _MCPAgent:
    def __init__(self):
        self.calls = []

    def ensure_ready(self):
        return {"ok": True}

    def call_tool(self, name, arguments, timeout=None):
        self.calls.append((name, arguments))
        if name == "recording_status":
            return {"ok": False, "code": "recording_session_missing"}
        if name in {"connect", "lock_window", "verify_window_lock"}:
            return {"ok": True}
        if name == "get_window_lock":
            return {"ok": True, "lock": {
                "process_name": "EDRClient.exe",
                "snapshot": {"process_name": "EDRClient.exe", "title": "EDRClient"},
            }}
        if name == "dump_tree":
            return {"ok": True, "backend": "windows_pywinauto", "controls": [{
                "automation_id": "btnApply", "control_type": "Button",
                "text": "应用", "rectangle": {"x": 10, "y": 20, "w": 100, "h": 40},
                "is_enabled": True,
            }]}
        if name == "execute_action":
            args = arguments
            return ActionReceipt.from_ok(
                action_id=args["action_id"], action_code=args["action_code"],
                request_id=args["request_id"], result={"ok": True},
            ).to_dict()
        raise AssertionError(name)


def test_mcp_runtime_builds_observation_local_ids_and_dispatches_unified_action():
    agent = _MCPAgent()
    observations = MCPObservationProvider(agent)
    first = observations.refresh()
    second = observations.refresh()
    assert first != second
    assert observations.get_snapshot(first)["controls"][0]["target_id"] == "T0001"
    assert observations.get_snapshot(second)["controls"][0]["target_id"] == "T0001"
    receipt = MCPActionDispatch(agent)(
        action_id="gui.click", action_code=None, args={},
        target_ref={"snapshot_id": second, "target_id": "T0001"}, request_id="R-1",
    )
    assert receipt.ok is True
    execute = next(call for call in agent.calls if call[0] == "execute_action")
    assert execute[1]["target_ref"]["snapshot_id"] == second


def test_fresh_observation_refuses_a_lost_window_lock_before_dumping_tree():
    class _LostLockAgent(_MCPAgent):
        def call_tool(self, name, arguments, timeout=None):
            if name == "verify_window_lock":
                self.calls.append((name, arguments))
                return {"ok": False, "error": "foreground changed"}
            return super().call_tool(name, arguments)

    agent = _LostLockAgent()
    with pytest.raises(BackendUnavailable):
        MCPObservationProvider(agent).refresh()
    assert [name for name, _ in agent.calls] == ["verify_window_lock"]


def test_visual_observation_refuses_a_full_screen_capture():
    class _FullScreenAgent(_MCPAgent):
        def call_tool(self, name, arguments, timeout=None):
            if name == "replay_capture":
                self.calls.append((name, arguments))
                return {
                    "ok": True,
                    "capture_scope": "screen",
                    "image_b64": base64.b64encode(
                        b"\x89PNG\r\n\x1a\nnot-needed"
                    ).decode("ascii"),
                }
            return super().call_tool(name, arguments)

    with pytest.raises(BackendUnavailable, match="locked-window screenshot"):
        MCPObservationProvider(
            _FullScreenAgent(), capture_screenshot=True,
        ).refresh()


def test_visual_resolver_refuses_observation_without_window_capture_scope():
    selector = ReplaySelector(
        window={"processName": "EDRClient.exe", "titleRegex": "^EDRClient$"},
        control={"automationId": "missing"},
        visual={
            "template": "assets/apply.png",
            "elementSha256": VISUAL_DIGEST,
            "redacted": True,
        },
    )
    resolver = SafeVisualResolver(
        lambda _template, _observation: [
            VisualCandidate((20, 30, 120, 70), 0.99)
        ]
    )

    with pytest.raises(StepMaterializationError) as exc_info:
        resolver.resolve("gui.click", selector, {
            "active_window": {"rect": [0, 0, 800, 600]},
            "screenshot_scope": "screen",
        })

    assert exc_info.value.code == "visual_window_unverified"


def test_action_trace_records_resolution_strategy_without_argument_values(tmp_path):
    agent = _MCPAgent()
    store = TraceStore(tmp_path)
    store.open(trace_id="TR-DISPATCH")
    dispatch = MCPActionDispatch(agent, trace_store=store)

    dispatch(
        action_id="gui.type_text", action_code=None,
        args={"text": "must-not-enter-trace"},
        target_ref={"snapshot_id": "OBS-1", "target_id": "T0001"},
        request_id="R-1",
    )

    requested = next(
        event for event in store.read_events()
        if event.event_type is EventType.ACTION_REQUESTED
    )
    assert requested.payload["resolution"] == "semantic"
    assert requested.payload["argument_keys"] == ["text"]
    assert "must-not-enter-trace" not in str(requested.to_dict())


def test_replay_cli_wires_confirmation_and_mcp_runtime(monkeypatch, tmp_path, capsys):
    agent = _MCPAgent()
    monkeypatch.setattr(cli, "_target_agent", lambda *_args: agent)
    golden = GoldenTrace(
        name="policy flow",
        source_recording={"schema": "edr.desktop-recording/v1", "sha256": "sha256:raw"},
        catalog={"version": CATALOG_VERSION, "digest": catalog_digest()},
        environment={"backend": "windows_pywinauto", "target": "win-dev"},
        steps=(RecordedStep("step-0001", "gui.click", {}, _selector()),),
        status="ready",
    )
    path = tmp_path / "golden-trace.json"
    path.write_text(canonical_json(golden.to_dict()), encoding="utf-8")

    exit_code = cli.main([
        "--target", "win-dev", "replay", str(path),
        "--confirm-action", "gui.click",
        "--trace-root", str(tmp_path / "traces"),
    ])
    output = __import__("json").loads(capsys.readouterr().out)
    assert exit_code == 0
    assert output["evaluation"]["taskSuccess"] is True
    assert __import__("pathlib").Path(output["evaluation_path"]).exists()
    assert (tmp_path / "traces" / output["trace_directory"].split("/")[-1] / "events.jsonl").exists()
    assert any(name == "execute_action" for name, _ in agent.calls)


@pytest.mark.parametrize(
    ("status", "digest", "profile", "expected_code"),
    [
        ("incomplete", catalog_digest(), None, "golden_trace_incomplete"),
        ("ready", "sha256:stale", None, "golden_catalog_mismatch"),
        ("ready", catalog_digest(), "locked-profile", "golden_profile_mismatch"),
    ],
)
def test_replay_cli_rejects_invalid_golden_before_target_gui_calls(
    monkeypatch, tmp_path, capsys, status, digest, profile, expected_code,
):
    agent = _MCPAgent()
    monkeypatch.setattr(cli, "_target_agent", lambda *_args: agent)
    environment = {
        "backend": "windows_pywinauto", "target": "win-dev",
        **({"profile": profile} if profile else {}),
    }
    golden = GoldenTrace(
        name="policy flow",
        source_recording={"schema": "edr.desktop-recording/v1", "sha256": "sha256:raw"},
        catalog={"version": CATALOG_VERSION, "digest": digest},
        environment=environment,
        steps=(RecordedStep("step-0001", "gui.click", {}, _selector()),),
        status=status,
    )
    path = tmp_path / "golden-trace.json"
    path.write_text(canonical_json(golden.to_dict()), encoding="utf-8")

    assert cli.main(["--target", "win-dev", "replay", str(path)]) == 1
    output = __import__("json").loads(capsys.readouterr().out)

    assert output["code"] == expected_code
    assert agent.calls == []


def test_replay_cli_blocks_an_active_capture_before_connecting(monkeypatch, tmp_path, capsys):
    class _ActiveRecordingAgent(_MCPAgent):
        def call_tool(self, name, arguments):
            self.calls.append((name, arguments))
            if name == "recording_status":
                return {"ok": True, "state": "recording"}
            raise AssertionError(name)

    agent = _ActiveRecordingAgent()
    monkeypatch.setattr(cli, "_target_agent", lambda *_args: agent)
    golden = GoldenTrace(
        name="policy flow",
        source_recording={"schema": "edr.desktop-recording/v1", "sha256": "sha256:raw"},
        catalog={"version": CATALOG_VERSION, "digest": catalog_digest()},
        environment={"backend": "windows_pywinauto", "target": "win-dev"},
        steps=(RecordedStep("step-0001", "gui.click", {}, _selector()),),
        status="ready",
    )
    path = tmp_path / "golden-trace.json"
    path.write_text(canonical_json(golden.to_dict()), encoding="utf-8")

    assert cli.main(["--target", "win-dev", "replay", str(path)]) == 1
    output = __import__("json").loads(capsys.readouterr().out)

    assert output["code"] == "recording_session_active"
    assert agent.calls == [("recording_status", {"heartbeat": False})]


def _drag_selectors():
    start = ReplaySelector(
        window={"processName": "EDRClient.exe", "titleRegex": "^EDRClient$"},
        control={
            "automationId": "sliderThumb", "controlType": "Thumb", "name": "阈值",
            "anchor": {"relativePoint": [0.25, 0.75]},
        },
    )
    end = ReplaySelector(
        window={"processName": "EDRClient.exe", "titleRegex": "^EDRClient$"},
        control={
            "automationId": "sliderTrack", "controlType": "Slider",
            "anchor": {"relativePoint": [0.05, 0.25]},
        },
    )
    return start, end


def _drag_controls(thumb_rect, track_rect):
    return [
        {
            "process_name": "EDRClient.exe", "window_title": "EDRClient",
            "automation_id": "sliderThumb", "control_type": "Thumb",
            "text": "阈值", "rect": thumb_rect,
        },
        {
            "process_name": "EDRClient.exe", "window_title": "EDRClient",
            "automation_id": "sliderTrack", "control_type": "Slider",
            "rect": track_rect,
        },
    ]


def test_drag_replays_anchors_against_the_current_rectangles_not_recorded_pixels():
    start, end = _drag_selectors()
    step = RecordedStep(
        "step-0001", "pointer.drag", {"duration": 0.4}, start,
        verifiers=({"type": "visible", "expected": True},),
        end_selector=end,
    )
    # Both controls sit 500px right of where they were recorded.
    runtime, _, calls = _runtime(_drag_controls([600, 100, 640, 140], [800, 200, 900, 240]))

    result = replay_golden_trace(runtime, _golden(step))

    assert result.task_success is True
    assert calls[0]["args"] == {
        "duration": 0.4, "x1": 610, "y1": 130, "x2": 805, "y2": 210,
    }


def test_a_drag_without_a_release_selector_blocks_before_dispatch():
    start, _ = _drag_selectors()
    step = RecordedStep(
        "step-0001", "pointer.drag", {}, start,
        verifiers=({"type": "visible", "expected": True},),
    )
    runtime, _, calls = _runtime(_drag_controls([600, 100, 640, 140], [800, 200, 900, 240]))

    result = replay_golden_trace(runtime, _golden(step))

    assert calls == []
    assert result.case_result.step_results[0].error["code"] == "replay_drag_end_missing"
    assert result.task_success is False


def test_a_drag_whose_release_target_disappeared_blocks_before_dispatch():
    start, end = _drag_selectors()
    step = RecordedStep(
        "step-0001", "pointer.drag", {}, start,
        verifiers=({"type": "visible", "expected": True},),
        end_selector=end,
    )
    controls = _drag_controls([600, 100, 640, 140], [800, 200, 900, 240])
    runtime, _, calls = _runtime(controls[:1])

    result = replay_golden_trace(runtime, _golden(step))

    assert calls == []
    assert result.case_result.step_results[0].error["code"] == "replay_target_not_found"


def _png(width=8, height=6):
    from PIL import Image
    import io

    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (30, 30, 30)).save(buffer, format="PNG")
    return buffer.getvalue()


class _CapturingObservations(_Observations):
    """A fresh observation that also carries a runtime frame, as visual replay does."""

    def __init__(self, controls, **frame):
        super().__init__(controls)
        self._frame = frame

    def refresh(self):
        snapshot_id = super().refresh()
        self.snapshots[snapshot_id].update(self._frame)
        self.snapshots[snapshot_id]["active_window"] = {
            "process_name": "EDRClient.exe", "title": "EDRClient",
            "pid": 4242, "rect": [0, 0, 800, 600],
        }
        return snapshot_id


def _persisting_runtime(tmp_path, observations):
    calls = []

    def dispatch(**kwargs):
        calls.append(kwargs)
        return ActionReceipt.from_ok(
            action_id=kwargs["action_id"], action_code=kwargs["action_code"],
            request_id=kwargs["request_id"], result={"ok": True},
        )

    store = TraceStore(tmp_path)
    store.open(trace_id="TR-EVIDENCE")
    executor = AtomicExecutor(dispatch=dispatch, observation_provider=observations)
    return ReplayRuntime(
        executor, "1.0.0", "sha256:catalog",
        trace_store=store, persist_replay_screenshots=True,
    ), store, calls


_CONTROL = {
    "process_name": "EDRClient.exe", "window_title": "EDRClient",
    "automation_id": "btnApply", "control_type": "Button", "text": "应用",
}


def test_a_redacted_replay_frame_is_persisted_into_the_execution_trace(tmp_path):
    png = _png()
    observations = _CapturingObservations(
        [_CONTROL],
        screenshot_bytes=png,
        screenshot_sha256="sha256:" + __import__("hashlib").sha256(png).hexdigest(),
        screenshot_scope="window",
        screenshot_redacted=True,
    )
    runtime, store, calls = _persisting_runtime(tmp_path, observations)

    result = replay_golden_trace(
        runtime, _golden(RecordedStep("step-0001", "gui.click", {}, _selector())),
    )

    assert result.task_success is True and len(calls) == 1
    persisted = [
        event for event in store.read_events()
        if event.event_type is EventType.SCREENSHOT_PERSISTED
    ]
    assert len(persisted) == 1
    relative = persisted[0].payload["relative_path"]
    written = store.root / relative
    assert written.read_bytes() == png
    assert persisted[0].payload["redaction"]["applied"] is False
    assert persisted[0].payload["window_title"] == "EDRClient"


def test_an_unredacted_replay_frame_is_refused_instead_of_written(tmp_path):
    observations = _CapturingObservations(
        [_CONTROL],
        screenshot_bytes=_png(),
        screenshot_scope="window",
        screenshot_redacted=False,
    )
    runtime, store, calls = _persisting_runtime(tmp_path, observations)

    result = replay_golden_trace(
        runtime, _golden(RecordedStep("step-0001", "gui.click", {}, _selector())),
    )

    assert calls == []
    assert result.case_result.step_results[0].error["code"] == "replay_capture_not_redacted"
    assert not (store.root / "screenshots").exists()


def test_a_full_screen_replay_frame_is_refused_instead_of_written(tmp_path):
    observations = _CapturingObservations(
        [_CONTROL],
        screenshot_bytes=_png(),
        screenshot_scope="screen",
        screenshot_redacted=True,
    )
    runtime, store, calls = _persisting_runtime(tmp_path, observations)

    result = replay_golden_trace(
        runtime, _golden(RecordedStep("step-0001", "gui.click", {}, _selector())),
    )

    assert calls == []
    assert result.case_result.step_results[0].error["code"] == "replay_capture_not_window_scoped"


def test_persisting_replay_screenshots_requires_a_trace_store():
    runtime, _, _ = _runtime([_CONTROL])
    object.__setattr__(runtime, "persist_replay_screenshots", True)
    with pytest.raises(RecordingModelError) as exc:
        replay_golden_trace(
            runtime, _golden(RecordedStep("step-0001", "gui.click", {}, _selector())),
        )
    assert exc.value.code == "replay_evidence_root_missing"


def test_replay_without_a_runtime_frame_persists_nothing(tmp_path):
    runtime, store, calls = _persisting_runtime(tmp_path, _Observations([_CONTROL]))

    result = replay_golden_trace(
        runtime, _golden(RecordedStep("step-0001", "gui.click", {}, _selector())),
    )

    assert result.task_success is True and len(calls) == 1
    assert not any(
        event.event_type is EventType.SCREENSHOT_PERSISTED
        for event in store.read_events()
    )


# ── ancestry must not turn a unique id into an unmatchable selector ──────────


def test_a_uniquely_identified_control_carries_no_ancestry():
    """The recorder walks the tree; the replay observation may report none."""
    from agent.recording.selectors import synthesize_selector
    from target.recording.models import CaptureScope, ObservedTarget, RawCaptureEvent

    event = RawCaptureEvent(
        sequence=1, wall_time="2026-08-18T00:00:00Z", monotonic_ms=1,
        type="pointer_click",
        scope=CaptureScope("t", "windows_pywinauto", "EDRClient.exe", "^logo1$"),
        input={"button": "left"},
        observed_target=ObservedTarget(
            "OBS-1", "T0001", "sha256:x", "Button",
            automation_id="EdrMainWindow.logCenterBtn", text="日志中心",
            ancestry=({"automationId": "EdrMainWindow.operationWidget",
                       "controlType": "Group"},),
        ),
    )

    control = synthesize_selector(event).control

    assert control["automationId"] == "EdrMainWindow.logCenterBtn"
    assert "ancestry" not in control


def test_a_control_without_a_strong_id_still_keeps_its_ancestry():
    from agent.recording.selectors import synthesize_selector
    from target.recording.models import CaptureScope, ObservedTarget, RawCaptureEvent

    event = RawCaptureEvent(
        sequence=1, wall_time="2026-08-18T00:00:00Z", monotonic_ms=1,
        type="pointer_click",
        scope=CaptureScope("t", "windows_pywinauto", "EDRClient.exe", "^logo1$"),
        input={"button": "left"},
        observed_target=ObservedTarget(
            "OBS-1", "T0001", "sha256:x", "DataItem",
            text="管理员",
            ancestry=({"automationId": "table", "controlType": "Table"},),
        ),
    )

    assert synthesize_selector(event).control["ancestry"] == [
        {"automationId": "table", "controlType": "Table"},
    ]


def _ancestry_selector(ancestry):
    return ReplaySelector(
        window={"processName": "EDRClient.exe", "titleRegex": "^EDRClient$"},
        control={"controlType": "DataItem", "name": "管理员", "ancestry": ancestry},
    )


def _row(ancestry):
    return {
        "process_name": "EDRClient.exe", "window_title": "EDRClient",
        "control_type": "DataItem", "text": "管理员", "ancestry": ancestry,
    }


def test_a_recorded_chain_matches_a_deeper_observed_one():
    runtime, _, calls = _runtime([_row([
        {"automation_id": "table", "control_type": "Table"},
        {"automation_id": "page", "control_type": "Group"},
        {"automation_id": "root", "control_type": "Window"},
    ])])
    step = RecordedStep(
        "step-0001", "gui.click", {},
        _ancestry_selector([{"automationId": "table", "controlType": "Table"}]),
    )

    result = replay_golden_trace(runtime, _golden(step))

    assert result.task_success is True and len(calls) == 1


def test_a_chain_that_disagrees_still_fails_to_match():
    runtime, _, calls = _runtime([_row([
        {"automation_id": "other", "control_type": "Table"},
    ])])
    step = RecordedStep(
        "step-0001", "gui.click", {},
        _ancestry_selector([{"automationId": "table", "controlType": "Table"}]),
    )

    result = replay_golden_trace(runtime, _golden(step))

    assert calls == []
    assert result.case_result.step_results[0].error["code"] == "replay_target_not_found"


def test_an_observation_reporting_no_ancestry_cannot_satisfy_a_recorded_chain():
    runtime, _, calls = _runtime([_row([])])
    step = RecordedStep(
        "step-0001", "gui.click", {},
        _ancestry_selector([{"automationId": "table", "controlType": "Table"}]),
    )

    result = replay_golden_trace(runtime, _golden(step))

    assert calls == []
    assert result.case_result.step_results[0].error["code"] == "replay_target_not_found"


# ── a recorded flow spans several windows ────────────────────────────────────


def _window_selector(title_regex, automation_id):
    return ReplaySelector(
        window={"processName": "EDRClient.exe", "titleRegex": title_regex},
        control={"automationId": automation_id},
    )


class _WindowedObservations(_Observations):
    """Only the focused window's controls are observable, as on a real target."""

    def __init__(self, windows):
        super().__init__([])
        self._windows = windows
        self.focused = None
        self.focus_calls = []

    def focus(self, process_name, title_regex):
        self.focus_calls.append((process_name, title_regex))
        self.focused = title_regex
        self.controls = self._windows.get(title_regex, [])
        return self.get_snapshot(self.refresh())


def _control(title, automation_id):
    return {
        "process_name": "EDRClient.exe", "window_title": title,
        "automation_id": automation_id, "control_type": "Button",
    }


def test_replay_focuses_the_window_each_step_was_recorded_in(tmp_path):
    observations = _WindowedObservations({
        "^logo1$": [_control("logo1", "logCenterBtn")],
        "^日志中心$": [_control("日志中心", "operationLogBtn")],
    })
    calls = []

    def dispatch(**kwargs):
        calls.append(kwargs)
        return ActionReceipt.from_ok(
            action_id=kwargs["action_id"], action_code=kwargs["action_code"],
            request_id=kwargs["request_id"], result={"ok": True},
        )

    runtime = ReplayRuntime(
        AtomicExecutor(dispatch=dispatch, observation_provider=observations),
        "1.0.0", "sha256:catalog",
        window_focus=observations.focus,
    )
    golden = _golden(
        RecordedStep("step-0001", "gui.click", {}, _window_selector("^logo1$", "logCenterBtn")),
        RecordedStep("step-0002", "gui.click", {}, _window_selector("^日志中心$", "operationLogBtn")),
    )

    result = replay_golden_trace(runtime, golden)

    assert result.task_success is True
    assert len(calls) == 2
    assert observations.focus_calls == [
        ("EDRClient.exe", "^logo1$"), ("EDRClient.exe", "^日志中心$"),
    ]


def test_consecutive_steps_in_one_window_focus_it_only_once():
    observations = _WindowedObservations({
        "^日志中心$": [_control("日志中心", "operationLogBtn")],
    })

    def dispatch(**kwargs):
        return ActionReceipt.from_ok(
            action_id=kwargs["action_id"], action_code=kwargs["action_code"],
            request_id=kwargs["request_id"], result={"ok": True},
        )

    runtime = ReplayRuntime(
        AtomicExecutor(dispatch=dispatch, observation_provider=observations),
        "1.0.0", "sha256:catalog",
        window_focus=observations.focus,
    )
    golden = _golden(
        RecordedStep("step-0001", "gui.click", {}, _window_selector("^日志中心$", "operationLogBtn")),
        RecordedStep("step-0002", "gui.click", {}, _window_selector("^日志中心$", "operationLogBtn")),
    )

    result = replay_golden_trace(runtime, golden)

    assert result.task_success is True
    assert observations.focus_calls == [("EDRClient.exe", "^日志中心$")]
