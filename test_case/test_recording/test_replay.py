from __future__ import annotations

import base64
import hashlib
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

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
from agent.recording.replay import (_anchor_point, GoldenStepMaterializer, ReplayRuntime,
                                    golden_to_test_case, replay_golden_trace)
from agent.recording.runtime import build_replay_runtime
from agent.recording.runtime import golden_entry_scope
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
            "screenshot_redacted": True,
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


def test_replay_entry_scope_uses_first_recorded_window_not_application_label():
    selector = ReplaySelector(
        window={"processName": "HiSecEndpointAgent.exe", "titleRegex": "^logo1$"},
        control={"automationId": "entryButton"},
    )
    golden = _golden(RecordedStep("step-0001", "gui.click", {}, selector))
    golden = replace(golden, environment={**golden.environment,
                                          "application": "EDRClient.exe"})

    scope = golden_entry_scope(golden)
    assert (scope.process_name, scope.title_regex) == ("HiSecEndpointAgent.exe", "^logo1$")


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


def test_missing_visual_asset_root_is_rejected_before_target_window_lock(tmp_path):
    class NoRemoteCalls:
        def call_tool(self, *args, **kwargs):
            pytest.fail("invalid local assets must not touch the target")

    golden = _golden(RecordedStep("step-0001", "gui.click", {}, _selector()))
    with pytest.raises(RecordingModelError, match="visual replay asset directory does not exist"):
        build_replay_runtime(NoRemoteCalls(), golden, replay_mode="visual_only",
                             asset_root=tmp_path / "missing")


@pytest.mark.parametrize("asset_bytes", [None, b"tampered"])
def test_missing_or_tampered_visual_template_is_rejected_before_target_lock(
    tmp_path, asset_bytes,
):
    class NoRemoteCalls:
        def call_tool(self, *args, **kwargs):
            pytest.fail("invalid local template must not touch the target")

    if asset_bytes is not None:
        (tmp_path / "template.png").write_bytes(asset_bytes)
    selector = ReplaySelector(
        window={"processName": "EDRClient.exe", "titleRegex": "^EDRClient$"},
        control={"automationId": "btnApply"},
        visual={"template": "template.png", "elementSha256": VISUAL_DIGEST,
                "redacted": True},
    )
    golden = _golden(RecordedStep("step-0001", "gui.click", {}, selector))
    with pytest.raises(RecordingModelError, match="template"):
        build_replay_runtime(NoRemoteCalls(), golden, replay_mode="visual_only",
                             asset_root=tmp_path)


def test_digest_matching_non_png_template_is_rejected_before_target_lock(tmp_path):
    class NoRemoteCalls:
        def call_tool(self, *args, **kwargs):
            pytest.fail("invalid image must not touch the target")

    content = b"not-a-png"
    (tmp_path / "template.png").write_bytes(content)
    selector = ReplaySelector(
        window={"processName": "EDRClient.exe", "titleRegex": "^EDRClient$"},
        control={"automationId": "btnApply"},
        visual={"template": "template.png",
                "elementSha256": "sha256:" + hashlib.sha256(content).hexdigest(),
                "redacted": True},
    )
    golden = _golden(RecordedStep("step-0001", "gui.click", {}, selector))
    with pytest.raises(RecordingModelError, match="valid PNG"):
        build_replay_runtime(NoRemoteCalls(), golden, replay_mode="visual_only",
                             asset_root=tmp_path)


def test_stable_control_id_replays_when_recorded_label_has_changed():
    runtime, _, calls = _runtime([{
        "process_name": "EDRClient.exe", "window_title": "EDRClient",
        "automation_id": "btnApply", "control_type": "Button", "text": "立即应用",
    }])
    result = replay_golden_trace(
        runtime, _golden(RecordedStep("step-0001", "gui.click", {}, _selector())),
    )
    assert result.task_success is True
    assert calls[0]["args"]["automation_id"] == "btnApply"
    assert "text" not in calls[0]["args"]


def test_assertion_replays_against_changed_time_text_without_click_materialization():
    selector = ReplaySelector(
        window={"processName": "EDRClient.exe", "titleRegex": "^EDRClient$"},
        control={"automationId": "logRow", "controlType": "Text", "name": "10:10:10"},
    )
    assertion = RecordedStep(
        "step-0001", None, {}, selector,
        verifiers=({"type": "text_time_range", "expected": "10:10"},),
    )
    runtime, _, calls = _runtime([{
        "process_name": "EDRClient.exe", "window_title": "EDRClient",
        "automation_id": "logRow", "control_type": "Text", "text": "10:10:59 用户登录",
    }])
    result = replay_golden_trace(runtime, _golden(assertion))
    assert result.task_success is True
    assert calls == []


def test_assertion_reobserves_until_async_log_appears_without_replaying_actions():
    selector = ReplaySelector(
        window={"processName": "EDRClient.exe", "titleRegex": "^EDRClient$"},
        control={"automationId": "logRow", "controlType": "Text", "name": "事件时间"},
    )
    assertion = RecordedStep(
        "step-0001", None, {}, selector,
        verifiers=({"type": "text_time_range", "expected": "10:10",
                    "timeoutSeconds": 1},),
    )

    class DelayedObservations(_Observations):
        def refresh(self):
            self.controls = [{
                "process_name": "EDRClient.exe", "window_title": "EDRClient",
                "automation_id": "logRow", "control_type": "Text",
                "text": "事件时间", "value": "10:10:42" if self.counter >= 3 else "等待同步",
            }]
            return super().refresh()

    observations = DelayedObservations([])
    calls = []
    executor = AtomicExecutor(dispatch=lambda **kwargs: calls.append(kwargs),
                              observation_provider=observations)
    runtime = ReplayRuntime(executor, "1.0.0", "sha256:catalog")
    result = replay_golden_trace(runtime, _golden(assertion))
    assert result.task_success is True
    assert observations.counter >= 4
    assert calls == []


def test_assertion_deadline_stops_polling_without_repeating_action():
    selector = ReplaySelector(
        window={"processName": "EDRClient.exe", "titleRegex": "^EDRClient$"},
        control={"automationId": "logRow", "controlType": "Text"},
    )
    assertion = RecordedStep(
        "step-0001", None, {}, selector,
        verifiers=({"type": "text_time_range", "expected": "10:10",
                    "timeoutSeconds": 0.05},),
    )
    runtime, observations, calls = _runtime([{
        "process_name": "EDRClient.exe", "window_title": "EDRClient",
        "automation_id": "logRow", "control_type": "Text", "text": "10:11:00",
    }])
    result = replay_golden_trace(runtime, _golden(assertion))
    assert result.task_success is False
    assert 3 <= observations.counter <= 5
    assert calls == []


def test_bound_assertion_polls_state_without_reclicking():
    step = RecordedStep(
        "step-0001", "gui.click", {}, _selector(),
        verifiers=({"type": "text_equals", "expected": "已应用",
                    "timeoutSeconds": 1},),
    )

    class DelayedObservations(_Observations):
        def refresh(self):
            self.controls = [{
                "process_name": "EDRClient.exe", "window_title": "EDRClient",
                "automation_id": "btnApply", "control_type": "Button",
                "text": "已应用" if self.counter >= 3 else "应用",
            }]
            return super().refresh()

    observations = DelayedObservations([])
    calls = []

    def dispatch(**kwargs):
        calls.append(kwargs)
        return ActionReceipt.from_ok(action_id=kwargs["action_id"],
                                     action_code=kwargs["action_code"],
                                     request_id=kwargs["request_id"], result={})

    runtime = ReplayRuntime(AtomicExecutor(dispatch=dispatch,
                                           observation_provider=observations),
                            "1.0.0", "sha256:catalog")
    result = replay_golden_trace(runtime, _golden(step))
    assert result.task_success is True
    assert observations.counter >= 4
    assert len(calls) == 1


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
        "rect": [100, 200, 220, 260],
    }
    other = dict(control)
    other["rect"] = [100, 280, 220, 340]
    runtime, _, calls = _runtime([control, other])
    result = replay_golden_trace(
        runtime, _golden(RecordedStep("step-0001", "gui.click", {}, _selector()))
    )
    assert calls == []
    assert result.case_result.step_results[0].status is StepStatus.BLOCKED
    assert result.case_result.step_results[0].error["code"] == "replay_target_ambiguous"
    assert result.evaluation.task_success is False
    assert result.evaluation.path_fidelity == 0.0
    # Actionable diagnostic surfaces which rects were ambiguous (2 identical
    # controls here -> 2 rects); still a refusal, never a silent pick.
    details = result.case_result.step_results[0].error.get("details") or {}
    assert details.get("candidate_count") == 2
    assert len(details.get("candidate_rects") or []) == 2


def test_replay_ambiguity_remediation_is_actionable_and_never_a_coordinate():
    control = {
        "process_name": "EDRClient.exe", "window_title": "EDRClient",
        "automation_id": "btnApply", "control_type": "Button", "text": "应用",
        "rect": [100, 200, 220, 260],
    }
    other = dict(control)
    other["rect"] = [100, 280, 220, 340]
    runtime, _, calls = _runtime([control, other])
    result = replay_golden_trace(
        runtime, _golden(RecordedStep("step-0001", "gui.click", {}, _selector()))
    )
    assert calls == []
    details = result.case_result.step_results[0].error.get("details") or {}
    remediation = details.get("remediation")
    # A refusal with actionable guidance, never a silent pinch of a coordinate.
    assert isinstance(remediation, str) and remediation
    assert "candidate_count" in remediation
    assert not any(banned in remediation
                   for banned in ("点击坐标", "使用录制坐标", "x=", "y="))


def test_replay_not_found_remediation_guides_window_state_check():
    selector = ReplaySelector(
        window={"processName": "EDRClient.exe", "titleRegex": "^EDRClient$"},
        control={"controlType": "Button", "name": "PolicySave"},
    )
    runtime, _, calls = _runtime([])  # observation has no matching control
    result = replay_golden_trace(
        runtime, _golden(RecordedStep("step-0001", "gui.click", {}, selector))
    )
    assert calls == []
    step = result.case_result.step_results[0]
    assert step.error["code"] in ("replay_target_not_found", "replay_target_ambiguous")
    details = step.error.get("details") or {}
    remediation = details.get("remediation")
    assert isinstance(remediation, str) and remediation
    assert not any(banned in remediation for banned in ("x=", "y="))


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


def test_window_assertion_requires_scoped_inventory_before_target_lock():
    class OldTarget:
        def tools_list(self):
            return {"tools": [{"name": "begin_product_run"}]}

        def call_tool(self, *args, **kwargs):
            pytest.fail("unsupported window assertion must not lock a target")

    golden = _golden(RecordedStep(
        "step-0001", "gui.click", {}, _selector(),
        verifiers=({"type": "window_open", "expected": True},),
    ))

    with pytest.raises(RecordingModelError, match="product-scoped window inventory"):
        build_replay_runtime(OldTarget(), golden)


def test_replay_rejects_incomplete_and_catalog_mismatch_before_execution():
    runtime, _, calls = _runtime([])
    with pytest.raises(RecordingModelError) as exc:
        replay_golden_trace(runtime, _golden(status="incomplete"))
    assert exc.value.code == "golden_trace_incomplete"
    with pytest.raises(RecordingModelError) as exc:
        replay_golden_trace(runtime, _golden(digest="sha256:old"))
    assert exc.value.code == "golden_catalog_mismatch"
    assert calls == []


def test_incomplete_rejection_names_unready_steps_and_result_verifier():
    """门槛拒绝时要把「哪几步、缺什么」说清楚 —— scroll/drag 缺的是绑定的结果
    verifier，不是补模板。只断言报错文案，不弱化门槛本身。"""
    runtime, _, calls = _runtime([])
    scroll = RecordedStep(
        "step-0001", "pointer.scroll", {"clicks": 1}, _selector(), status="incomplete",
        issues=("compile_scroll_verifier_required",),
    )
    golden = _golden(scroll, status="incomplete")
    with pytest.raises(RecordingModelError) as exc:
        replay_golden_trace(runtime, golden)
    assert exc.value.code == "golden_trace_incomplete"
    message = str(exc.value)
    assert "step-0001" in message and "pointer.scroll" in message
    assert "结果 verifier" in message or "结果断言" in message
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


def test_cleanup_resolution_metrics_do_not_pollute_main_case():
    main = RecordedStep("step-0001", "gui.click", {}, _selector())
    cleanup_selector = ReplaySelector(
        window={"processName": "EDRClient.exe", "titleRegex": "^EDRClient$"},
        control={"automationId": "missing"},
        visual={
            "template": "assets/cleanup.png", "redacted": True,
            "elementSha256": VISUAL_DIGEST,
        },
    )
    cleanup = RecordedStep(
        "cleanup-0001", "gui.click", {}, cleanup_selector,
    )
    runtime, _, calls = _runtime([{
        "process_name": "EDRClient.exe", "window_title": "EDRClient",
        "automation_id": "btnApply", "control_type": "Button", "text": "应用",
    }])
    object.__setattr__(runtime, "replay_mode", "semantic_first")
    object.__setattr__(runtime, "visual_resolver", SafeVisualResolver(
        lambda _template, _observation: [
            VisualCandidate((100, 120, 200, 160), 0.98),
        ]
    ))

    result = replay_golden_trace(runtime, _golden(main, cleanup=(cleanup,)))

    assert len(calls) == 2
    assert calls[1]["action_id"] == "pointer.click_window"
    assert result.evaluation.semantic_resolution_rate == 1.0
    assert result.evaluation.visual_fallback_count == 0


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


def test_semantic_first_rejects_conflicting_visual_evidence():
    selector = ReplaySelector(
        window={"processName": "EDRClient.exe", "titleRegex": "^EDRClient$"},
        control={"automationId": "btnApply"},
        visual={"template": "assets/apply.png", "redacted": True,
                "elementSha256": VISUAL_DIGEST},
    )
    runtime, _, calls = _runtime([{
        "process_name": "EDRClient.exe", "window_title": "EDRClient",
        "automation_id": "btnApply", "rect": [100, 100, 150, 150],
    }])
    object.__setattr__(runtime, "replay_mode", "semantic_first")
    object.__setattr__(runtime, "visual_resolver", SafeVisualResolver(
        lambda _template, _observation: [VisualCandidate((300, 300, 350, 350), 0.99)]
    ))
    result = replay_golden_trace(runtime, _golden(RecordedStep("step-0001", "gui.click", {}, selector)))
    assert calls == []
    assert result.case_result.step_results[0].error["code"] == "locator_evidence_conflict"


def test_semantic_first_uses_visual_to_disambiguate_two_controls():
    selector = ReplaySelector(
        window={"processName": "EDRClient.exe", "titleRegex": "^EDRClient$"},
        control={"controlType": "Button", "name": "应用"},
        visual={"template": "assets/apply.png", "redacted": True,
                "elementSha256": VISUAL_DIGEST},
    )
    runtime, _, calls = _runtime([
        {"process_name": "EDRClient.exe", "window_title": "EDRClient",
         "control_type": "Button", "text": "应用", "rect": [100, 100, 150, 150]},
        {"process_name": "EDRClient.exe", "window_title": "EDRClient",
         "control_type": "Button", "text": "应用", "rect": [300, 100, 350, 150]},
    ])
    object.__setattr__(runtime, "replay_mode", "semantic_first")
    object.__setattr__(runtime, "visual_resolver", SafeVisualResolver(
        lambda _template, _observation: [VisualCandidate((300, 100, 350, 150), 0.99)]
    ))
    result = replay_golden_trace(runtime, _golden(RecordedStep("step-0001", "gui.click", {}, selector)))
    assert result.task_success is True
    assert len(calls) == 1
    assert calls[0]["target_ref"]["target_id"].endswith("02")


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


@pytest.mark.parametrize("mode", ["semantic_first", "visual_only"])
def test_visual_only_selector_without_control_identity_replays_in_visual_modes(mode):
    selector = ReplaySelector(
        window={"processName": "EDRClient.exe", "titleRegex": "^EDRClient$"},
        control={},
        visual={"template": "assets/unlabeled.png", "redacted": True,
                "elementSha256": VISUAL_DIGEST},
    )
    runtime, _, calls = _runtime([])
    object.__setattr__(runtime, "replay_mode", mode)
    object.__setattr__(runtime, "visual_resolver", SafeVisualResolver(
        lambda _template, _observation: [VisualCandidate((20, 30, 120, 70), 0.99)],
    ))
    result = replay_golden_trace(
        runtime, _golden(RecordedStep("step-0001", "gui.click", {}, selector)),
    )
    assert result.task_success is True
    assert calls[0]["action_id"] == "pointer.click_window"
    assert result.evaluation.visual_fallback_count == 1


def test_image_only_replay_does_not_click_sole_control_when_template_is_missing():
    selector = ReplaySelector(
        window={"processName": "EDRClient.exe", "titleRegex": "^EDRClient$"},
        control={},
        visual={"template": "assets/unlabeled.png", "redacted": True,
                "elementSha256": VISUAL_DIGEST},
    )
    runtime, _, calls = _runtime([{
        "process_name": "EDRClient.exe", "window_title": "EDRClient",
        "automation_id": "unrelated", "control_type": "Button", "text": "Cancel",
        "rect": [0, 0, 30, 30],
    }])
    object.__setattr__(runtime, "replay_mode", "semantic_first")
    object.__setattr__(runtime, "visual_resolver", SafeVisualResolver(
        lambda _template, _observation: [],
    ))

    result = replay_golden_trace(
        runtime, _golden(RecordedStep("step-0001", "gui.click", {}, selector)),
    )

    assert result.task_success is False
    assert result.case_result.step_results[0].error["code"] == "visual_match_low_confidence"
    assert calls == []


def test_visual_only_selector_does_not_silently_run_in_semantic_only_mode():
    selector = ReplaySelector(
        window={"processName": "EDRClient.exe", "titleRegex": "^EDRClient$"},
        control={},
        visual={"template": "assets/unlabeled.png", "redacted": True,
                "elementSha256": VISUAL_DIGEST},
    )
    runtime, _, calls = _runtime([])
    result = replay_golden_trace(
        runtime, _golden(RecordedStep("step-0001", "gui.click", {}, selector)),
    )
    assert calls == []
    assert result.case_result.step_results[0].error["code"] == "replay_selector_semantic_missing"


def test_image_only_trace_semantic_mode_is_rejected_before_target_lock():
    class NoRemoteCalls:
        def call_tool(self, *args, **kwargs):
            pytest.fail("incompatible replay mode must not touch the target")

    selector = ReplaySelector(
        window={"processName": "EDRClient.exe", "titleRegex": "^EDRClient$"},
        control={},
        visual={"template": "assets/unlabeled.png", "redacted": True,
                "elementSha256": VISUAL_DIGEST},
    )
    golden = _golden(RecordedStep("step-0001", "gui.click", {}, selector))
    with pytest.raises(RecordingModelError) as exc:
        build_replay_runtime(NoRemoteCalls(), golden, replay_mode="semantic_only")
    assert exc.value.code == "replay_selector_semantic_missing"


@pytest.mark.parametrize("mode", ["semantic_only", "visual_only"])
def test_two_fresh_replays_use_new_observations_in_both_locator_modes(mode):
    selector = ReplaySelector(
        window={"processName": "EDRClient.exe", "titleRegex": "^EDRClient$"},
        control={"automationId": "btnApply", "controlType": "Button", "name": "应用"},
        visual={"template": "assets/apply.png", "redacted": True,
                "elementSha256": VISUAL_DIGEST},
    )
    golden = _golden(RecordedStep("step-0001", "gui.click", {}, selector))
    for label in ("应用", "立即应用"):
        runtime, observations, calls = _runtime([{
            "process_name": "EDRClient.exe", "window_title": "EDRClient",
            "automation_id": "btnApply", "control_type": "Button", "text": label,
        }])
        object.__setattr__(runtime, "replay_mode", mode)
        if mode == "visual_only":
            object.__setattr__(runtime, "visual_resolver", SafeVisualResolver(
                lambda _template, _observation: [VisualCandidate((20, 30, 120, 70), 0.99)],
                template_verifier=lambda _template, _visual: True,
            ))
        result = replay_golden_trace(runtime, golden)
        assert result.task_success is True
        assert observations.counter >= 1
        assert calls[0]["action_id"] == (
            "pointer.click_window" if mode == "visual_only" else "gui.click"
        )


def test_visual_only_replay_uses_context_to_choose_one_of_repeated_icons():
    selector = ReplaySelector(
        window={"processName": "EDRClient.exe", "titleRegex": "^EDRClient$"},
        control={"name": "重复图标"},
        visual={"template": "element.png", "elementSha256": VISUAL_DIGEST,
                "contextTemplate": "context.png", "contextSha256": VISUAL_DIGEST,
                "contextOffset": [10, 10], "redacted": True},
    )
    runtime, _, calls = _runtime([])
    def match(template, _observation):
        if template == "element.png":
            return [VisualCandidate((20, 30, 40, 50), 0.99),
                    VisualCandidate((120, 30, 140, 50), 0.99)]
        return [VisualCandidate((110, 20, 160, 70), 0.98)]
    object.__setattr__(runtime, "replay_mode", "visual_only")
    object.__setattr__(runtime, "visual_resolver", SafeVisualResolver(
        match, template_verifier=lambda _template, _visual: True,
    ))
    result = replay_golden_trace(
        runtime, _golden(RecordedStep("step-0001", "gui.click", {}, selector)),
    )
    assert result.task_success is True
    assert calls[0]["action_id"] == "pointer.click_window"
    assert calls[0]["args"]["x"] == 130


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
    """Test double for the agent-side MCP session.

    Legacy replay tools are canned; the controlled-chain v3 tools
    (`_V3_SERVER_TOOLS`) forward to the REAL `target.server` module — the
    fake OS layer installed by `_v3_cli_env` replaces only OS facts, the
    server lease/binding/dispatcher chain stays authentic.  Tests that
    never install the fake OS must not hit the v3 tools (they route away
    from them), so the forwarding is gated on `_v3_server_tools_enabled`.
    """

    def __init__(self):
        self.calls = []
        self.v3_server_tools = False

    def ensure_ready(self):
        return {"ok": True}

    def call_tool(self, name, arguments, timeout=None):
        self.calls.append((name, arguments))
        if name == "recording_status":
            if self.v3_server_tools:
                from target import server as _server
                return __import__("json").loads(_server.recording_status(**arguments))
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
        if self.v3_server_tools and name in _V3_SERVER_TOOLS:
            # Controlled-chain tools go to the REAL server module (the fake
            # OS layer is already installed by _v3_cli_env); no mocked logic.
            from target import server as _server
            tool = getattr(_server, name)
            return __import__("json").loads(tool(**arguments))
        raise AssertionError(name)


_V3_SERVER_TOOLS = frozenset({
    "begin_product_run", "register_window_declaration", "bind_entry_window",
    "focus_bound_window", "execute_action", "end_product_run",
    "observe_product_windows", "reverify_bound_window",
    "begin_effect_baseline", "observe_effect_after_action",
    "bind_increment",
})


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


def test_fresh_observation_rejects_malformed_control_entries():
    class _MalformedTreeAgent(_MCPAgent):
        def call_tool(self, name, arguments, timeout=None):
            if name == "dump_tree":
                return {"ok": True, "controls": [{"automation_id": "btn-ok"}, None]}
            return super().call_tool(name, arguments, timeout=timeout)

    with pytest.raises(BackendUnavailable, match="invalid control entry"):
        MCPObservationProvider(_MalformedTreeAgent()).refresh()


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


def test_visual_resolver_refuses_unredacted_window_capture():
    selector = ReplaySelector(
        window={"processName": "EDRClient.exe"}, control={"automationId": "missing"},
        visual={"template": "assets/apply.png", "elementSha256": VISUAL_DIGEST,
                "redacted": True},
    )
    resolver = SafeVisualResolver(
        lambda _template, _observation: [VisualCandidate((20, 30, 120, 70), 0.99)]
    )
    with pytest.raises(StepMaterializationError) as exc_info:
        resolver.resolve("gui.click", selector, {
            "active_window": {"rect": [0, 0, 800, 600]},
            "screenshot_scope": "window", "screenshot_redacted": False,
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


def test_replay_cli_uses_target_app_profile_for_bound_trace(monkeypatch, tmp_path, capsys):
    agent = _MCPAgent()
    monkeypatch.setattr(cli, "_target_agent", lambda *_args: agent)

    class _TargetConfig:
        def __init__(self, _path=None):
            pass

        def get_target_app_profile(self, name):
            assert name == "win-dev"
            return "windows_hisec"

    monkeypatch.setattr(cli, "TargetConfig", _TargetConfig)
    golden = GoldenTrace(
        name="policy flow",
        source_recording={"schema": "edr.desktop-recording/v1", "sha256": "sha256:raw"},
        catalog={"version": CATALOG_VERSION, "digest": catalog_digest()},
        environment={"backend": "windows_pywinauto", "target": "win-dev",
                     "profile": "windows_hisec"},
        steps=(RecordedStep("step-0001", "gui.click", {}, _selector()),),
        status="ready",
    )
    path = tmp_path / "golden-trace.json"
    path.write_text(canonical_json(golden.to_dict()), encoding="utf-8")

    exit_code = cli.main(["--target", "win-dev", "replay", str(path),
                          "--confirm-action", "gui.click",
                          "--trace-root", str(tmp_path / "traces")])
    output = __import__("json").loads(capsys.readouterr().out)

    assert exit_code == 0
    assert output["evaluation"]["taskSuccess"] is True
    assert any(name == "execute_action" for name, _ in agent.calls)


def test_runtime_rejects_bound_profile_mismatch_before_target_calls():
    golden = _golden(RecordedStep("step-0001", "gui.click", {}, _selector()))
    golden = replace(golden, environment={**golden.environment,
                                          "profile": "windows_hisec"})

    class NoRemoteCalls:
        def __getattr__(self, name):
            raise AssertionError(f"unexpected target access: {name}")

    with pytest.raises(RecordingModelError, match="golden trace profile") as exc:
        build_replay_runtime(NoRemoteCalls(), golden, profile="windows_notepad")
    assert exc.value.code == "golden_profile_mismatch"


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


def _compile_v3_golden_same_window(tmp_path):
    """真实 compiler ready v3 产物(连续同窗两步、无 effects/verifiers)
    → 写盘 JSON → round-trip 回 GoldenTrace(同 CLI load_golden_trace)。"""
    import dataclasses
    import runpy

    ns = runpy.run_path("test_case/test_recording/test_golden_v3_window_sequence.py")
    from agent.recording.compiler import compile_recording

    raw = ns["_with_timeline"](ns["_recording"]([
        ns["_click"](1, 100, "CAUSE-1", 100, instance_id="RI-0001",
                     automation_id="btnApply", snapshot_id="CS-0001"),
        ns["_click"](2, 300, "CAUSE-2", 100, instance_id="RI-0001",
                     automation_id="btnApply", snapshot_id="CS-0001"),
    ], windows=[dict(ns["_MAIN"])], snapshots=(
        ns["_snap"]("CS-0001", 50, 100, "EdrMainWindow",
                    [{"automationId": "EdrMainWindow",
                      "controlType": "Window"}]),
    )), instances=("RI-0001",))
    result = compile_recording(raw, profile="windows_hisec")
    assert result.golden.status == "ready", result.golden.status
    golden = dataclasses.replace(result.golden, steps=tuple(
        dataclasses.replace(s, verifiers=()) for s in result.golden.steps))
    path = tmp_path / "golden-v3.json"
    path.write_text(canonical_json(golden.to_dict()), encoding="utf-8")
    return path


def _v3_cli_env(monkeypatch, tmp_path, agent):
    """CLI replay 的目标/配置/虚假 OS 层(仅 OS 事实替身;server 链真实)。"""
    from types import SimpleNamespace

    import target.server as server
    from target import product_policy
    from target.action_dispatcher import set_backend_resolver
    from test_case.test_recording.test_v3_real_server_adapter import _FakeOSBackend

    product_policy.reset_for_tests()
    server._binding_facade_holder["facade"] = None
    backend = _FakeOSBackend({
        "100": {"process_id": 42, "title": "logo1",
                "root_control_id": "EdrMainWindow",
                "visible": True, "foreground": True,
                "extra_controls": [
                    {"automation_id": "btnApply", "text": "Apply"}]},
    })
    monkeypatch.setattr(
        server, "_backend",
        product_policy.GuardedBackend(backend, "windows_pywinauto"))
    monkeypatch.setattr(server, "_backend_kind", "windows_pywinauto")
    monkeypatch.setattr(
        server, "_recording_service",
        SimpleNamespace(status=lambda **kw: {"state": "idle"}))
    set_backend_resolver(lambda: server._backend)

    class _TargetConfig:
        def __init__(self, _path=None):
            pass

        def get_target_app_profile(self, name):
            assert name == "win-dev"
            return "windows_hisec"

    monkeypatch.setattr(cli, "_target_agent", lambda *_args: agent)
    monkeypatch.setattr(cli, "TargetConfig", _TargetConfig)
    agent.v3_server_tools = True
    return backend


def test_replay_cli_routes_v3_golden_to_controlled_chain(monkeypatch, tmp_path, capsys):
    """B 阶段片(设计 §6 步骤2):golden 带 windowSequence 时 CLI replay
    路由到 execute_v3_window_stage_real 受控链;v1 路径不回退。

    链路真实性:server 工具链全真实(begin/声明/绑定/聚焦/派发/end),
    仅 OS 事实层是替身;role map 经 derive_role_map 从 golden 身份 +
    已装产品角色推导(不硬编码)。断言受控链报告形状(受控字段)与
    每步恰一次真实派发,不沿用 v1 的 evaluation/case_result 投影。"""
    import json as _json
    from unittest.mock import patch as _patch

    agent = _MCPAgent()
    _v3_cli_env(monkeypatch, tmp_path, agent)
    path = _compile_v3_golden_same_window(tmp_path)
    trace_root = tmp_path / "traces"

    # 入口替换但链路原样受控:精确钉住 CLI → execute_v3_window_stage_real
    # (经 v3_transport 载体),受控链内部零 mock。
    from agent.recording import execution_ledger
    real_adapter = execution_ledger.execute_v3_window_stage_real
    adapter_calls = []

    def _spying_adapter(**kwargs):
        adapter_calls.append({
            "server_tools": type(kwargs["server_tools"]).__name__,
            "window_role_map": dict(kwargs["window_role_map"]),
            "ledger_root": str(kwargs["ledger"].root),
        })
        return real_adapter(**kwargs)

    with _patch.object(execution_ledger, "execute_v3_window_stage_real",
                       side_effect=_spying_adapter):
        exit_code = cli.main(["--target", "win-dev", "replay", str(path),
                              "--trace-root", str(trace_root)])
    output = _json.loads(capsys.readouterr().out)

    assert exit_code == 0, output
    assert len(adapter_calls) == 1
    wiring = adapter_calls[0]
    # 受控链载体与授权映射(derive_role_map 产物,不硬编码)。
    assert wiring["server_tools"] == "MCPServerTools"
    assert wiring["window_role_map"] == {"W1": "main"}
    # ledger 落在 CLI trace_root 下的受控账本。
    assert wiring["ledger_root"] == str(trace_root / "v3-attempts")
    # 受控链真实跑完两步:每步恰一次派发、previous 链持久。
    assert output["runId"]
    assert [s["outcome"] for s in output["steps"]] == ["pass", "pass"]
    assert output["steps"][0]["transitionId"] == "first_bind"
    assert output["steps"][1]["previousStepId"] == "step-0001"
    # 受控链真实跑完两步:每步恰一次真实派发(统一 dispatcher 在
    # focus_bound_window(action_id=...) 内)、previous 链持久。
    dispatches = [c for c in agent.calls if c[0] == "focus_bound_window"
                  and c[1].get("action_id")]
    assert len(dispatches) == 2
    assert all(d[1]["action_id"] == "gui.click" for d in dispatches)
    # token 仅内存:不进报告。
    assert "run_token" not in _json.dumps(output)
    # 账本事件真实落盘在 trace_root 下(重启可重建边界)。
    assert list((trace_root / "v3-attempts").glob("EVT-*.json"))


def test_replay_cli_v1_golden_keeps_legacy_path(monkeypatch, tmp_path, capsys):
    """v1 golden(无 windowSequence)不回退:仍走 build_replay_runtime +
    replay_golden_trace,不受 v3 路由影响。"""
    agent = _MCPAgent()
    monkeypatch.setattr(cli, "_target_agent", lambda *_args: agent)
    golden = _golden(RecordedStep("step-0001", "gui.click", {}, _selector()),
                     digest=catalog_digest())
    golden = replace(golden, catalog={
        "version": CATALOG_VERSION, "digest": catalog_digest()})
    path = tmp_path / "golden-trace.json"
    path.write_text(canonical_json(golden.to_dict()), encoding="utf-8")

    from unittest.mock import patch as _patch
    build_calls = []
    real_build = cli.build_replay_runtime

    def _spy_build(*a, **kw):
        build_calls.append(kw)
        return real_build(*a, **kw)

    with _patch.object(cli, "build_replay_runtime",
                       side_effect=_spy_build):
        exit_code = cli.main(["--target", "win-dev", "replay", str(path),
                              "--confirm-action", "gui.click",
                              "--trace-root", str(tmp_path / "traces")])
    output = __import__("json").loads(capsys.readouterr().out)

    assert exit_code == 0
    assert len(build_calls) == 1, "v1 golden must keep the legacy runtime path"
    assert output["evaluation"]["taskSuccess"] is True


def test_replay_cli_refuses_v3_role_derivation_failure_before_target_gui(monkeypatch, tmp_path, capsys):
    """golden 身份无法唯一映射到已装产品角色 → CLI 层 fail-closed
    (RoleDerivationError),零 target GUI 调用(仅 recording_status 探测)。"""
    agent = _MCPAgent()
    _v3_cli_env(monkeypatch, tmp_path, agent)
    path = _compile_v3_golden_same_window(tmp_path)
    # 篡改 title 到无角色匹配的值(selector 与 identity 同步改,仍通过
    # load 的一致性校验;仅角色派生无解),走真实 load 路径。
    import json as _json
    doc = _json.loads(path.read_text(encoding="utf-8"))
    doc["windowSequence"]["windows"][0]["identity"]["titleRegex"] = \
        "^Totally-Unknown-Title$"
    for step in doc["steps"].values():
        if isinstance(step, dict) and (step.get("selector") or {}).get("window"):
            step["selector"]["window"]["titleRegex"] = \
                "^Totally-Unknown-Title$"
    # entryWindow 与首 selector 步严格一致(§5.1 一致性门),同步改。
    if doc.get("entryWindow"):
        doc["entryWindow"]["titleRegex"] = "^Totally-Unknown-Title$"
    path.write_text(canonical_json(doc), encoding="utf-8")

    exit_code = cli.main(["--target", "win-dev", "replay", str(path),
                          "--trace-root", str(tmp_path / "traces")])
    output = _json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert output["code"] == "v3_role_derivation_failed"
    assert not [c for c in agent.calls if c[0] in {
        "begin_product_run", "register_window_declaration",
        "bind_entry_window", "execute_action"}]


def test_library_gate_still_refuses_direct_replay_of_v3():
    """守卫:CLI 拆门只在 CLI 层;replay_golden_trace 直调 v3 仍拒绝
    (设计 §6 步骤2:库级门保留语义)。"""
    import dataclasses

    from agent.recording.models import WindowSequence
    from agent.recording.replay import _refuse_v3_execution

    golden = dataclasses.replace(
        _golden(RecordedStep("step-0001", "gui.click", {}, _selector())),
        schema="edr.desktop-golden-trace/v3",
        window_sequence=WindowSequence(
            "edr.window-sequence/v1",
            ({"id": "W1", "identity": dict(_selector().window),
              "origin": "already_open"},),
            ({"stepId": "step-0001", "windowId": "W1"},)))
    runtime, _, _ = _runtime([{
        "process_name": "EDRClient.exe", "window_title": "EDRClient",
        "automation_id": "btnApply", "control_type": "Button",
        "text": "应用", "fingerprint": "sha256:current"}])
    from target.recording.models import RecordingModelError
    with pytest.raises(RecordingModelError) as excinfo:
        from agent.recording.replay import replay_golden_trace
        replay_golden_trace(runtime, golden)
    assert excinfo.value.code == "window_sequence_execution_unsupported"
    # golden_to_test_case 同门。
    from agent.recording.replay import golden_to_test_case
    with pytest.raises(RecordingModelError):
        golden_to_test_case(golden)


def test_replay_cli_v3_effects_surface_over_mcp_carrier(monkeypatch, tmp_path, capsys):
    """B 阶段片(轮次 7):带 effects 的跨窗口 A→B→A golden 经 CLI replay
    在 MCP 载体下跑通——begin_effect_baseline / observe_effect_after_action
    / bind_increment 经 server 工具往返,不再 AttributeError fail-closed。

    链路真实性:server 工具链全真实(含轮次 7 新增的三个 S4 效果工具,
    走 _binding_facade_for_run 同门),仅 OS 事实层是替身;OS 层接动作
    效果(click btnApply → Log Center 出现;click logRow → 关闭),让
    appeared/absent 对真实差分求值。"""
    import json as _json

    agent = _MCPAgent()
    backend = _v3_cli_env(monkeypatch, tmp_path, agent)
    # W2(Log Center)OS 事实:初始不可见,与
    # test_v3_real_server_adapter._fake_backend_for("a_b_a") 同形状;
    # W1 同时声明 btnApply/btnBack(第三步点击目标)。
    backend._windows["100"]["extra_controls"].append(
        {"automation_id": "btnBack", "text": "Apply"})
    backend._windows["200"] = {
        "process_id": 42, "title": "Log Center",
        "root_control_id": "LogCenterWindow",
        "visible": False, "foreground": True,
        "process_name": "EDRClient.exe",
        "extra_controls": [
            {"automation_id": "logRow", "text": "Apply"}]}
    # A→B→A 动作效果接线(录制语义:点 btnApply 出现 Log Center,
    # 点 logRow 关闭)——与 test_v3_real_server_adapter._fake_backend_for
    # 同一事实形状。
    original_click = backend.click

    def click_with_effect(**kwargs):
        out = original_click(**kwargs)
        if kwargs.get("automation_id") == "btnApply":
            backend._windows["200"]["visible"] = True
        elif kwargs.get("automation_id") == "logRow":
            backend._windows["200"]["visible"] = False
        return out

    backend.click = click_with_effect

    # 真实 compiler ready 产物:动作后出现(W2 appeared)+ 关闭
    # (W2 absent)显式断言 → effects 携带于 steps(与 adapter 测试
    # _compile_effect_golden 同一构造)。
    import dataclasses
    import runpy

    from agent.recording.compiler import compile_recording
    from agent.recording.models import GoldenTrace

    ns = runpy.run_path(
        "test_case/test_recording/test_golden_v3_window_sequence.py")
    raw = ns["_cross_window_events"]()
    evs = list(raw.events)
    evs[1] = dataclasses.replace(evs[1], assertion={
        "type": "window_open",
        "expected": {"exists": True, "title": "Log Center",
                     "processName": "EDRClient.exe"}})
    evs[3] = dataclasses.replace(evs[3], assertion={
        "type": "window_open",
        "expected": {"exists": False, "title": "Log Center",
                     "processName": "EDRClient.exe"}})
    raw = dataclasses.replace(raw, events=evs)
    result = compile_recording(raw, profile="windows_hisec")
    assert result.golden.status == "ready"
    golden = GoldenTrace.from_dict(result.golden.to_dict())
    sequence = golden.window_sequence.to_dict()
    assert [s.get("effects") for s in sequence["steps"] if s.get("effects")], (
        "前置:编译产物必须带 effects,否则本用例测不到效果面")
    path = tmp_path / "golden-v3-effects.json"
    path.write_text(canonical_json(golden.to_dict()), encoding="utf-8")

    trace_root = tmp_path / "traces"
    exit_code = cli.main(["--target", "win-dev", "replay", str(path),
                          "--trace-root", str(trace_root)])
    output = _json.loads(capsys.readouterr().out)

    assert exit_code == 0, output
    assert output["latched"] is False
    assert [s["outcome"] for s in output["steps"]] == ["pass", "pass", "pass"]
    # 效果面真实经 MCP 工具往返:_MCPAgent 记录到三类效果工具调用,
    # 且 effect 工具在真实 server 门(_binding_facade_for_run)下被接受
    # (若 shim 缺方法/服务端缺工具,这里要么 AttributeError 要么空记录)。
    effect_calls = {name for name, _ in agent.calls if name in {
        "begin_effect_baseline", "observe_effect_after_action",
        "bind_increment"}}
    assert effect_calls == {
        "begin_effect_baseline", "observe_effect_after_action",
        "bind_increment"}, agent.calls
    # appeared 唯一增量 → 延迟绑定经 bind_increment 真实发生。
    bind_calls = [a for n, a in agent.calls if n == "bind_increment"]
    assert bind_calls and bind_calls[0].get("native_id") == "200"
    assert bind_calls[0].get("window_id") == "W2"
    # 轮次 8:效果评估结果进 CLI 报告——output steps 带各自声明的
    # windowEffect(appeared/absent satisfied=true;编译产物里
    # appeared 在 step-0001、absent 在 step-0002、step-0003 无)。
    step_effects = {s["stepId"]: s.get("windowEffect") for s in
                    output["steps"]}
    assert step_effects["step-0001"] and \
        step_effects["step-0001"][0]["kind"] == "appeared" and \
        step_effects["step-0001"][0]["satisfied"] is True, step_effects
    assert step_effects["step-0002"] and \
        step_effects["step-0002"][0]["kind"] == "absent" and \
        step_effects["step-0002"][0]["satisfied"] is True, step_effects
    assert "windowEffect" not in output["steps"][2], step_effects


def test_replay_cli_v3_visual_only_golden_resolves_over_mcp_carrier(
        monkeypatch, tmp_path, capsys):
    """B 阶段片(轮次 16):visual-only golden 经 CLI replay 在 MCP 载体下
    走 A023 派发 — CLI 面为声明含 visual selector 的 golden 构造
    visual_resolver(asset_root = golden 文件父目录,v1 路径同款约定),
    经 execute_v3_window_stage_real 注入受控链;无 visual 步的 golden 不
    构造 resolver(既有 round 6/7 CLI 用例继续绿 = 零行为变化背书)。
    OS 假层照 adapter 正例 test_visual_only_step_resolves_and_dispatches_
    click_window 的夹具形状(真实 PNG 模板 digest 对齐 + 灰底帧唯一
    匹配 + click_window_at/screenshot 假层)。"""
    import base64
    import dataclasses
    import hashlib
    import io
    import json as _json
    from unittest.mock import patch as _patch

    from PIL import Image, ImageDraw

    from agent.recording import execution_ledger
    from agent.recording.models import GoldenTrace
    from target.windowing.attempt_ledger import AttemptLedger
    from test_case.test_recording.test_v3_real_server_adapter import (
        _compile_effect_golden)

    agent = _MCPAgent()
    backend = _v3_cli_env(monkeypatch, tmp_path, agent)
    # W2(Log Center)OS 事实 + A→B→A 语义效果接线(与既有 effects CLI
    # 用例同形状);step-0003 btnBack 无效果声明无需接线。
    backend._windows["100"]["extra_controls"].append(
        {"automation_id": "btnBack", "text": "Apply"})
    backend._windows["200"] = {
        "process_id": 42, "title": "Log Center",
        "root_control_id": "LogCenterWindow",
        "visible": False, "foreground": True,
        "process_name": "EDRClient.exe",
        "extra_controls": [
            {"automation_id": "logRow", "text": "Apply"}]}
    original_click = backend.click

    def click_with_effect(**kwargs):
        out = original_click(**kwargs)
        if kwargs.get("automation_id") == "logRow":
            backend._windows["200"]["visible"] = False
        return out

    backend.click = click_with_effect
    # step-0001 现在是 visual-only 步:appeared 效果挂 A023 的 OS 工具
    # (语义点击不再发生,backend.click 的 btnApply 分支不会触发)。
    def click_window_at(x, y, window_title_re=None):
        backend.dispatch_calls.append({
            "method": "click_window_at", "x": int(x), "y": int(y),
            "window_title_re": window_title_re})
        backend._windows["200"]["visible"] = True
        return {"ok": True, "method": "mouse.click",
                "window_relative": {"x": int(x), "y": int(y)}}

    backend.click_window_at = click_window_at
    # 红框锁定窗口帧假层(照 adapter 正例):800x600 灰底,噪声模板
    # (红底白十字)贴 (0,0) — 全帧唯一过置信度门槛的匹配区。
    template = Image.new("RGB", (60, 40), (200, 30, 30))
    tdraw = ImageDraw.Draw(template)
    tdraw.line([(30, 5), (30, 35)], fill=(255, 255, 255), width=3)
    tdraw.line([(10, 20), (50, 20)], fill=(255, 255, 255), width=3)
    buf = io.BytesIO()
    template.save(buf, format="PNG")
    template_bytes = buf.getvalue()
    frame = Image.new("RGB", (800, 600), (90, 90, 90))
    frame.paste(template, (0, 0))
    fbuf = io.BytesIO()
    frame.save(fbuf, format="PNG")
    frame_bytes = fbuf.getvalue()

    def screenshot(_path=None):
        return {"ok": True, "capture_scope": "window",
                "image_b64": base64.b64encode(frame_bytes).decode("ascii"),
                "origin": [0, 0]}

    backend.screenshot = screenshot
    backend.recorder_ui_rectangles = lambda: []
    backend.protected_rectangles = lambda: []

    # 真实 compiler ready golden:step-0001 控件身份置空 + visual 模板
    # (digest 对齐),照 adapter 正例的替换形状。模板与 golden 同目录 —
    # CLI asset_root 约定 = golden 文件父目录,本用例同时钉住该接线。
    golden = _compile_effect_golden()
    visual = {
        "template": "apply.png",
        "elementSha256": "sha256:" +
            hashlib.sha256(template_bytes).hexdigest(),
        "redacted": True,
        "relativePoint": [0.5, 0.5],
    }
    first = dataclasses.replace(
        golden.steps[0],
        selector=dataclasses.replace(golden.steps[0].selector, control={},
                                     visual=visual))
    golden = GoldenTrace.from_dict(dataclasses.replace(
        golden, steps=(first, *golden.steps[1:])).to_dict())
    path = tmp_path / "golden-v3-visual.json"
    path.write_text(canonical_json(golden.to_dict()), encoding="utf-8")
    (tmp_path / "apply.png").write_bytes(template_bytes)

    trace_root = tmp_path / "traces"
    # 入口替换但链路原样受控:钉住 CLI → execute_v3_window_stage_real
    # 的 visual_resolver 注入(受控链内部零 mock)。
    real_adapter = execution_ledger.execute_v3_window_stage_real
    adapter_calls = []

    def _spying_adapter(**kwargs):
        adapter_calls.append(
            {"visual_resolver": kwargs.get("visual_resolver")})
        return real_adapter(**kwargs)

    with _patch.object(execution_ledger, "execute_v3_window_stage_real",
                       side_effect=_spying_adapter):
        exit_code = cli.main(["--target", "win-dev", "replay", str(path),
                              "--trace-root", str(trace_root),
                              "--replay-mode", "semantic_first"])
    output = _json.loads(capsys.readouterr().out)

    assert exit_code == 0, output
    assert len(adapter_calls) == 1
    # CLI 面接线:声明含 visual selector → resolver 非空注入受控链。
    assert adapter_calls[0]["visual_resolver"] is not None, (
        "CLI 必须为 visual-only golden 构造 visual_resolver")
    # 三步全链(visual step-0001 + 语义 step-0002/0003)在 MCP 载体下
    # 全 pass;每步恰一次派发。
    assert output["latched"] is False, output["steps"]
    assert [s["outcome"] for s in output["steps"]] == \
        ["pass", "pass", "pass"], output["steps"]
    assert output["dispatchInputs"] == 3, output["steps"]
    # 实际动作:A023 恰一次(visual 步,窗口相对坐标落模板中心
    # relativePoint [0.5,0.5] × 60x40 → 30,20);语义步走 gui.click。
    click_calls = [c for c in backend.dispatch_calls
                   if c["method"] == "click_window_at"]
    assert len(click_calls) == 1, backend.dispatch_calls
    assert click_calls[0]["x"] == 30 and click_calls[0]["y"] == 20, \
        click_calls[0]
    events = AttemptLedger(trace_root / "v3-attempts").rebuild_history(
        run_id=output["runId"])
    assert events[0]["receiptPayload"].get("action_id") == \
        "pointer.click_window", events[0]["receiptPayload"]
    assert [e["receiptPayload"].get("action_id") for e in events[1:]] == \
        ["gui.click", "gui.click"], \
        [e["receiptPayload"] for e in events[1:]]


def test_replay_cli_v3_golden_without_visual_gets_no_resolver(
        monkeypatch, tmp_path, capsys):
    """轮次 16 条件面钉死:无 visual 步的 golden 经 CLI replay 不构造
    visual_resolver(零行为变化承诺的负面对照——无条件构造会改变既有
    无 visual 流的接线面,模板目录缺省时的行为差异必须不发生)。"""
    import json as _json
    from unittest.mock import patch as _patch

    from agent.recording import execution_ledger

    agent = _MCPAgent()
    _v3_cli_env(monkeypatch, tmp_path, agent)
    path = _compile_v3_golden_same_window(tmp_path)
    trace_root = tmp_path / "traces"
    real_adapter = execution_ledger.execute_v3_window_stage_real
    adapter_calls = []

    def _spying_adapter(**kwargs):
        adapter_calls.append(
            {"visual_resolver": kwargs.get("visual_resolver")})
        return real_adapter(**kwargs)

    with _patch.object(execution_ledger, "execute_v3_window_stage_real",
                       side_effect=_spying_adapter):
        exit_code = cli.main(["--target", "win-dev", "replay", str(path),
                              "--trace-root", str(trace_root)])
    output = _json.loads(capsys.readouterr().out)

    assert exit_code == 0, output
    assert len(adapter_calls) == 1
    assert adapter_calls[0]["visual_resolver"] is None, (
        "无 visual 步的 golden 不应构造 visual_resolver")
    assert [s["outcome"] for s in output["steps"]] == ["pass", "pass"], \
        output["steps"]


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


def test_drag_100_percent_anchors_stay_inside_both_current_controls():
    start, end = _drag_selectors()
    start.control["anchor"] = {"relativePoint": [1.0, 1.0]}
    end.control["anchor"] = {"relativePoint": [1.0, 1.0]}
    step = RecordedStep("step-0001", "pointer.drag", {}, start, end_selector=end)
    runtime, _, calls = _runtime(_drag_controls([600, 100, 640, 140], [800, 200, 900, 240]))

    result = replay_golden_trace(runtime, _golden(step))

    assert result.task_success is True
    assert calls[0]["args"] == {"x1": 639, "y1": 139, "x2": 899, "y2": 239}


@pytest.mark.parametrize("rect", ([10, 10, 10, 20], [20, 10, 10, 20]))
def test_drag_anchor_rejects_empty_or_inverted_current_control(rect):
    with pytest.raises(StepMaterializationError) as exc:
        _anchor_point({"rect": rect}, {"anchor": {"relativePoint": [0.5, 0.5]}})
    assert exc.value.code == "replay_observation_invalid"


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


_EVIDENCE_ANCESTRY = [
    {"controlType": "Table"},
    {"automationId": "EdrMainWindow.baseWidget.pageWidget.scanPage.VirusScanWindow"
     ".scanDetailWidget.tablePage", "controlType": "Group"},
    {"automationId": "EdrMainWindow.baseWidget.pageWidget.scanPage.VirusScanWindow"
     ".scanDetailWidget", "controlType": "Custom"},
    {"automationId": "EdrMainWindow.baseWidget.pageWidget.scanPage.VirusScanWindow",
     "controlType": "Group"},
    {"automationId": "EdrMainWindow.baseWidget.pageWidget.scanPage", "controlType": "Group"},
    {"automationId": "EdrMainWindow.baseWidget.pageWidget", "controlType": "Custom"},
]


def _evidence_details_rows(ancestry):
    rows = []
    for index in range(6):
        rows.append({
            "process_name": "EDRClient.exe", "window_title": "logo1",
            "control_type": "DataItem", "text": "Details", "ancestry": ancestry,
            "rect": [1056, 403 + index * 40, 1118, 443 + index * 40],
        })
    return rows


def _evidence_selector():
    return ReplaySelector(
        window={"processName": "EDRClient.exe", "rootAutomationId": "EdrMainWindow"},
        control={"controlType": "DataItem", "name": "Details",
                 "ancestry": _EVIDENCE_ANCESTRY},
    )


def test_evidence_shape_now_resolves_to_honest_weak_identity_ambiguity():
    """2026-10-28 evidence (candidate_count=0) is closed: with the ancestry
    threading now emitted by dump_tree, the recorded weak-identity cell
    resolves to six honest candidates and is refused as ambiguous, with the
    missing-stable-id remediation — never a silent pick."""
    runtime, _, calls = _runtime(_evidence_details_rows(_EVIDENCE_ANCESTRY))
    step = RecordedStep("step-0001", "gui.click", {}, _evidence_selector())

    result = replay_golden_trace(runtime, _golden(step))

    assert calls == []
    error = result.case_result.step_results[0].error
    assert error["code"] == "replay_target_ambiguous"
    details = error.get("details") or {}
    assert details.get("candidate_count") == 6
    remediation = details.get("remediation")
    assert isinstance(remediation, str) and "缺少稳定自动化ID" in remediation
    assert not any(banned in remediation for banned in ("x=", "y=", "点击坐标"))


def test_evidence_shape_with_empty_observed_ancestry_still_fails_closed_not_found():
    """The historical 2026-10-27 observation shape (controls carrying no
    ancestry at all) must keep failing as not_found with zero candidates —
    the gap the evidence recorded — rather than pretending the chain matched."""
    runtime, _, calls = _runtime(_evidence_details_rows([]))
    step = RecordedStep("step-0001", "gui.click", {}, _evidence_selector())

    result = replay_golden_trace(runtime, _golden(step))

    assert calls == []
    error = result.case_result.step_results[0].error
    assert error["code"] == "replay_target_not_found"
    assert (error.get("details") or {}).get("candidate_count") == 0


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

    def focus(self, process_name, title_regex, root=""):
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


@pytest.mark.parametrize("assertion_type", ["text_time_range", "window_text_time_range"])
def test_cross_window_assertion_observes_log_window_without_repeating_action(assertion_type):
    observations = _WindowedObservations({
        "^EDRClient$": [{
            "process_name": "EDRClient.exe", "window_title": "EDRClient",
            "automation_id": "btnApply", "control_type": "Button", "text": "10:11:00",
        }],
        "^日志中心$": [{
            "process_name": "EDRClient.exe", "window_title": "日志中心",
            "automation_id": "logTime", "control_type": "Text", "text": "10:10:59",
        }],
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
        "1.0.0", "sha256:catalog", window_focus=observations.focus,
    )
    log_selector = ReplaySelector(
        window={"processName": "EDRClient.exe", "titleRegex": "^日志中心$"},
        control=({"automationId": "logTime", "controlType": "Text"}
                 if assertion_type == "text_time_range" else {}),
    )
    verifier = {"type": assertion_type, "expected": "10:10"}
    if assertion_type == "text_time_range":
        verifier["match"] = {"automation_id": "logTime", "control_type": "Text"}
    golden = _golden(
        RecordedStep("step-0001", "gui.click", {}, _selector()),
        RecordedStep("step-0002", None, {}, log_selector, verifiers=(verifier,)),
    )

    result = replay_golden_trace(runtime, golden)

    assert result.task_success is True
    assert len(calls) == 1
    assert observations.focus_calls == [
        ("EDRClient.exe", "^EDRClient$"), ("EDRClient.exe", "^日志中心$"),
    ]


def test_replay_relocks_new_window_when_click_steals_old_window_foreground():
    class ForegroundObservations(_WindowedObservations):
        def __init__(self, windows):
            super().__init__(windows)
            self.locked = None
            self.foreground = None

        def focus(self, process_name, title_regex, root=""):
            self.locked = title_regex
            self.foreground = title_regex
            return super().focus(process_name, title_regex, root)

        def refresh(self):
            if not self.locked or self.foreground != self.locked:
                raise BackendUnavailable("old window lost foreground")
            return super().refresh()

    observations = ForegroundObservations({
        "^logo1$": [_control("logo1", "logCenterBtn")],
        "^日志中心$": [_control("日志中心", "operationLogBtn")],
    })
    calls = []

    def dispatch(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            observations.foreground = "^日志中心$"
        return ActionReceipt.from_ok(
            action_id=kwargs["action_id"], action_code=kwargs["action_code"],
            request_id=kwargs["request_id"], result={"ok": True},
        )

    runtime = ReplayRuntime(
        AtomicExecutor(dispatch=dispatch, observation_provider=observations),
        "1.0.0", "sha256:catalog", window_focus=observations.focus,
    )
    golden = _golden(
        RecordedStep("step-0001", "gui.click", {}, _window_selector("^logo1$", "logCenterBtn")),
        RecordedStep("step-0002", "gui.click", {}, _window_selector("^日志中心$", "operationLogBtn")),
    )

    result = replay_golden_trace(runtime, golden)

    assert result.task_success is True, [step.error for step in result.case_result.step_results]
    assert len(calls) == 2
    assert observations.focus_calls == [
        ("EDRClient.exe", "^logo1$"), ("EDRClient.exe", "^日志中心$"),
    ]


def test_terminal_window_open_assertion_polls_product_inventory_without_old_lock():
    class InventoryObservations(_WindowedObservations):
        def __init__(self):
            super().__init__({"^logo1$": [_control("logo1", "logCenterBtn")]})
            self.old_lock_lost = False
            self.inventory_reads = 0

        def refresh(self):
            if self.old_lock_lost:
                raise BackendUnavailable("old window lost foreground")
            return super().refresh()

        def refresh_window_inventory(self):
            self.inventory_reads += 1
            self.counter += 1
            snapshot_id = f"OBS-INVENTORY-{self.counter}"
            self.snapshots[snapshot_id] = {
                "snapshot_id": snapshot_id, "controls": [],
                "windows": ([{"process_name": "EDRClient.exe", "title": "日志中心"}]
                            if self.inventory_reads >= 2 else []),
            }
            return snapshot_id

    observations = InventoryObservations()
    calls = []

    def dispatch(**kwargs):
        calls.append(kwargs)
        observations.old_lock_lost = True
        return ActionReceipt.from_ok(
            action_id=kwargs["action_id"], action_code=kwargs["action_code"],
            request_id=kwargs["request_id"], result={"ok": True},
        )

    runtime = ReplayRuntime(
        AtomicExecutor(dispatch=dispatch, observation_provider=observations),
        "1.0.0", "sha256:catalog", window_focus=observations.focus,
    )
    golden = _golden(RecordedStep(
        "step-0001", "gui.click", {}, _window_selector("^logo1$", "logCenterBtn"),
        verifiers=({"type": "window_open", "expected": {
            "exists": True, "processName": "EDRClient.exe",
            "titleRegex": "^日志中心$",
        }, "timeoutSeconds": 1},),
    ))

    result = replay_golden_trace(runtime, golden)

    assert result.task_success is True
    assert len(calls) == 1
    assert observations.inventory_reads >= 2
    assert observations.focus_calls == [("EDRClient.exe", "^logo1$")]


def test_window_inventory_provider_requires_product_scope_and_native_identity():
    class Agent:
        def call_tool(self, name, args, timeout=None):
            assert name == "observe_product_windows"
            assert args == {"run_token": "run-token"}
            return {"ok": True, "windows": [{
                "native_id": "42", "process_id": 101, "generation": 2,
                "role": "log_center", "process_name": "EDRClient.exe",
                "title": "日志中心",
            }]}

    provider = MCPObservationProvider(Agent(), run_token="run-token")
    snapshot = provider.get_snapshot(provider.refresh_window_inventory())
    assert snapshot["windows"] == [{
        "native_id": "42", "process_id": 101, "generation": 2,
        "role": "log_center", "process_name": "EDRClient.exe",
        "title": "日志中心",
    }]
    with pytest.raises(BackendUnavailable, match="product_run_required"):
        MCPObservationProvider(Agent()).refresh_window_inventory()


def test_window_closed_assertion_cannot_pass_while_registered_window_remains():
    class InventoryObservations(_Observations):
        def refresh_window_inventory(self):
            snapshot_id = super().refresh()
            self.snapshots[snapshot_id]["windows"] = [
                {"process_name": "EDRClient.exe", "title": "日志中心"},
            ]
            return snapshot_id

    observations = InventoryObservations([{
        "process_name": "EDRClient.exe", "window_title": "EDRClient",
        "automation_id": "btnApply", "control_type": "Button", "text": "应用",
    }])
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
    )
    golden = _golden(RecordedStep(
        "step-0001", "gui.click", {}, _selector(),
        verifiers=({"type": "window_open", "expected": {
            "exists": False, "processName": "EDRClient.exe",
            "titleRegex": "^日志中心$",
        }, "timeoutSeconds": 0.01},),
    ))

    result = replay_golden_trace(runtime, golden)

    assert result.task_success is False
    assert len(calls) == 1
    assert result.case_result.step_results[0].expectation_results[-1].status is StepStatus.FAILED


def test_terminal_window_closed_passes_from_inventory_after_old_lock_disappears():
    class ClosingObservations(_Observations):
        def __init__(self):
            super().__init__([{
                "process_name": "EDRClient.exe", "window_title": "EDRClient",
                "automation_id": "btnApply", "control_type": "Button", "text": "应用",
            }])
            self.closed = False

        def refresh(self):
            if self.closed:
                raise BackendUnavailable("locked window closed")
            return super().refresh()

        def refresh_window_inventory(self):
            self.counter += 1
            snapshot_id = f"OBS-INVENTORY-{self.counter}"
            self.snapshots[snapshot_id] = {
                "snapshot_id": snapshot_id, "controls": [], "windows": [],
            }
            return snapshot_id

    observations = ClosingObservations()
    calls = []

    def dispatch(**kwargs):
        calls.append(kwargs)
        observations.closed = True
        return ActionReceipt.from_ok(
            action_id=kwargs["action_id"], action_code=kwargs["action_code"],
            request_id=kwargs["request_id"], result={"ok": True},
        )

    runtime = ReplayRuntime(
        AtomicExecutor(dispatch=dispatch, observation_provider=observations),
        "1.0.0", "sha256:catalog",
    )
    golden = _golden(RecordedStep(
        "step-0001", "gui.click", {}, _selector(),
        verifiers=({"type": "window_open", "expected": {
            "exists": False, "processName": "EDRClient.exe",
            "titleRegex": "^EDRClient$",
        }},),
    ))

    result = replay_golden_trace(runtime, golden)

    assert result.task_success is True
    assert len(calls) == 1


def test_replay_does_not_retarget_old_window_control_assertion_to_next_window():
    golden = _golden(
        RecordedStep(
            "step-0001", "gui.click", {}, _window_selector("^logo1$", "logCenterBtn"),
            verifiers=({"type": "text_equals", "expected": "ready"},),
        ),
        RecordedStep("step-0002", "gui.click", {},
                     _window_selector("^日志中心$", "operationLogBtn")),
    )
    focused = []
    materializer = GoldenStepMaterializer(
        golden, window_focus=lambda *args: focused.append(args),
    )

    assert materializer.recover_post_action_observation(
        golden_to_test_case(golden).steps[0]
    ) is False
    assert focused == []


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


def test_same_window_rebuild_relocks_before_next_action_without_repeating_previous_action():
    class RebuiltWindowObservations(_WindowedObservations):
        def __init__(self):
            super().__init__({"^日志中心$": [_control("日志中心", "operationLogBtn")]})
            self.stale = False
            self.stale_after_post = False

        def focus(self, process_name, title_regex, root=""):
            self.stale = False
            return super().focus(process_name, title_regex, root)

        def refresh(self):
            if self.stale:
                raise BackendUnavailable("cached window lock is stale")
            return super().refresh()

        def get_snapshot(self, snapshot_id):
            snapshot = super().get_snapshot(snapshot_id)
            if self.stale_after_post:
                self.stale_after_post = False
                self.stale = True
            return snapshot

    observations = RebuiltWindowObservations()
    calls = []

    def dispatch(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            observations.stale_after_post = True
        return ActionReceipt.from_ok(
            action_id=kwargs["action_id"], action_code=kwargs["action_code"],
            request_id=kwargs["request_id"], result={"ok": True},
        )

    runtime = ReplayRuntime(
        AtomicExecutor(dispatch=dispatch, observation_provider=observations),
        "1.0.0", "sha256:catalog", window_focus=observations.focus,
    )
    golden = _golden(
        RecordedStep("step-0001", "gui.click", {}, _window_selector("^日志中心$", "operationLogBtn")),
        RecordedStep("step-0002", "gui.click", {}, _window_selector("^日志中心$", "operationLogBtn")),
    )

    result = replay_golden_trace(runtime, golden)

    assert result.task_success is True
    assert len(calls) == 2
    assert observations.focus_calls == [
        ("EDRClient.exe", "^日志中心$"), ("EDRClient.exe", "^日志中心$"),
    ]


def test_the_window_root_distinguishes_two_windows_that_share_a_title():
    """HiSec titles both its agent and its client window `logo1`."""
    observations = _WindowedObservations({
        "^logo1$": [_control("logo1", "logCenterBtn")],
    })
    seen = []

    def focus(process_name, title_regex, root=""):
        seen.append(root)
        observations.focused = title_regex
        observations.controls = observations._windows.get(title_regex, [])
        return observations.get_snapshot(observations.refresh())

    def dispatch(**kwargs):
        return ActionReceipt.from_ok(
            action_id=kwargs["action_id"], action_code=kwargs["action_code"],
            request_id=kwargs["request_id"], result={"ok": True},
        )

    runtime = ReplayRuntime(
        AtomicExecutor(dispatch=dispatch, observation_provider=observations),
        "1.0.0", "sha256:catalog", window_focus=focus,
    )

    def _selector(root):
        return ReplaySelector(
            window={
                "processName": "EDRClient.exe", "titleRegex": "^logo1$",
                "rootAutomationId": root,
            },
            control={"automationId": "logCenterBtn"},
        )

    golden = _golden(
        RecordedStep("step-0001", "gui.click", {}, _selector("SafraUIMainWindow")),
        RecordedStep("step-0002", "gui.click", {}, _selector("EdrMainWindow")),
    )

    replay_golden_trace(runtime, golden)

    # Same process, same title, two different windows: only the root separates
    # them, so each one must be focused in its own right.
    assert seen == ["SafraUIMainWindow", "EdrMainWindow"]


def test_focusing_reports_when_the_title_matched_the_wrong_window():
    from agent.recording.mcp_runtime import MCPObservationProvider

    class _Agent(_MCPAgent):
        def call_tool(self, name, arguments, timeout=None):
            if name in {"connect", "lock_window"}:
                return {"ok": True}
            return super().call_tool(name, arguments, timeout=timeout)

    provider = MCPObservationProvider(_Agent())

    with pytest.raises(BackendUnavailable, match="EdrMainWindow"):
        provider.focus_window("EDRClient.exe", "^logo1$", "EdrMainWindow")


def test_rooted_replay_rejects_target_without_exact_focus_before_gui_calls(tmp_path):
    class OldTarget:
        def tools_list(self):
            return {"tools": [{"name": "begin_product_run"}]}

        def call_tool(self, *args, **kwargs):
            pytest.fail("missing exact-focus capability must fail before GUI calls")

    rooted = ReplaySelector(
        window={"processName": "EDRClient.exe", "titleRegex": "^logo1$",
                "rootAutomationId": "LogRoot"},
        control={"automationId": "logTime"},
    )
    golden = _golden(RecordedStep("step-0001", None, {}, rooted,
                                  verifiers=({"type": "text_time_range", "expected": "10:10"},)))

    with pytest.raises(RecordingModelError, match="exact product-window focus"):
        build_replay_runtime(OldTarget(), golden, trace_root=tmp_path)


def test_rooted_entry_replay_defers_lock_to_exact_product_focus(tmp_path):
    calls = []

    class Target:
        state = SimpleNamespace(backend_kind="windows_pywinauto")

        def tools_list(self):
            return {"tools": [{"name": name} for name in (
                "begin_product_run", "end_product_run", "observe_window_bundle",
                "focus_window_role", "focus_window_selector",
            )]}

        def call_tool(self, name, args, timeout=None):
            calls.append(name)
            if name == "begin_product_run":
                return {"ok": True, "run_token": "TOKEN"}
            if name == "end_product_run":
                return {"ok": True}
            pytest.fail(f"entry focus should not use legacy {name}")

    rooted = ReplaySelector(
        window={"processName": "EDRClient.exe", "titleRegex": "^logo1$",
                "rootAutomationId": "LogRoot"},
        control={},
    )
    golden = _golden(RecordedStep("step-0001", None, {}, rooted,
                                  verifiers=({"type": "window_text_time_range",
                                              "expected": "10:10"},)))

    runtime = build_replay_runtime(Target(), golden, profile="windows_hisec",
                                   trace_root=tmp_path)

    assert calls == ["begin_product_run"]
    assert runtime.window_focus is not None
    runtime.end_product_run()


# ---------------------------------------------------------------------------
# Mid-replay interruption -> the on-disk trace must still terminate honestly.
#
# `run_case` converts *typed* failures (BackendUnavailable,
# StepMaterializationError) into blocked steps, so their traces finalise with
# a normal evaluation.  But an *untyped* exception escaping run_case -- e.g.
# an OSError while evidence persistence touches disk, a KeyboardInterrupt
# from an operator cancelling the replay, or any non-typed transport fault --
# propagates out of `_run_golden_trace` and, without the interruption guard,
# leaves the trace directory without any terminal event: evidence files that
# look "in progress" forever, indistinguishable from a machine that died
# mid-write.  The guard appends `trace_aborted` before re-raising, so every
# opened trace directory ends with trace_completed or trace_aborted -- never
# neither.  When the trace already terminated (e.g. the run completed and
# only evaluation.json persistence then failed) the guard must not add a
# second terminal event nor mask the original error.
# ---------------------------------------------------------------------------


class _DyingObservations(_Observations):
    """Observations that refresh normally until armed, then raise untyped.

    BackendUnavailable would be *typed* and absorbed by the executor as a
    blocked step; the plain RuntimeError models the untyped class of failure
    (OSError from evidence persistence, KeyboardInterrupt, an exotic
    transport fault) that escapes run_case's handlers.
    """

    def __init__(self, controls):
        super().__init__(controls)
        self.raise_now = False

    def refresh(self):
        if self.raise_now:
            raise RuntimeError("evidence persistence disk full")
        return super().refresh()


def _traced_runtime(controls, trace_root):
    observations = _DyingObservations(controls)
    store = TraceStore(Path(trace_root))
    store.open()

    def dispatch(**kwargs):
        return ActionReceipt.from_ok(
            action_id=kwargs["action_id"], action_code=kwargs["action_code"],
            request_id=kwargs["request_id"], result={"ok": True},
        )

    executor = AtomicExecutor(dispatch=dispatch, observation_provider=observations)
    return ReplayRuntime(executor, "1.0.0", "sha256:catalog", trace_store=store), observations, store


def _btn_apply_controls():
    return [{
        "process_name": "EDRClient.exe",
        "window_title": "EDRClient",
        "automation_id": "btnApply",
        "control_type": "Button",
        "text": "应用",
    }]


def test_untyped_mid_replay_exception_still_terminates_the_trace(tmp_path):
    step1 = RecordedStep("step-0001", "gui.click", {}, _selector())
    step2 = RecordedStep("step-0002", "gui.click", {}, _selector())
    runtime, observations, store = _traced_runtime(_btn_apply_controls(), tmp_path)

    calls = []

    def dispatch_arms_the_failure(**kwargs):
        calls.append(kwargs["action_id"])
        observations.raise_now = True
        return ActionReceipt.from_ok(
            action_id=kwargs["action_id"], action_code=kwargs["action_code"],
            request_id=kwargs["request_id"], result={"ok": True},
        )

    executor = AtomicExecutor(dispatch=dispatch_arms_the_failure,
                              observation_provider=observations)
    runtime = replace(runtime, executor=executor)

    with pytest.raises(RuntimeError, match="disk full"):
        replay_golden_trace(runtime, _golden(step1, step2))

    assert len(calls) == 1, "the failure arms after the first dispatch"
    events = store.read_events()
    terminal = [event for event in events
                if event.event_type in (EventType.TRACE_COMPLETED, EventType.TRACE_ABORTED)]
    assert len(terminal) == 1, f"exactly one terminal event expected, got {terminal}"
    assert events[-1].event_type is EventType.TRACE_ABORTED
    assert events[-1].payload["reason"] == "replay_interrupted"
    assert events[-1].payload["error_type"] == "RuntimeError"


def test_failure_after_completed_trace_neither_double_terminates_nor_masks(tmp_path):
    step1 = RecordedStep("step-0001", "gui.click", {}, _selector())
    runtime, _, store = _traced_runtime(_btn_apply_controls(), tmp_path)

    def disk_full(*args, **kwargs):
        raise OSError("evaluation.json persistence failed")

    with pytest.MonkeyPatch.context() as patcher:
        patcher.setattr("agent.recording.replay.tempfile.mkstemp", disk_full)
        with pytest.raises(OSError, match="evaluation.json"):
            replay_golden_trace(runtime, _golden(step1))

    events = store.read_events()
    terminal = [event for event in events
                if event.event_type in (EventType.TRACE_COMPLETED, EventType.TRACE_ABORTED)]
    assert len(terminal) == 1, "the completed trace must keep its single terminal event"
    assert events[-1].event_type is EventType.TRACE_COMPLETED


# ---------------------------------------------------------------------------
# 轮次 23(设计 §6 步骤2 + §5):CLI replay v3 的 golden 加载面接单夹
# manifest 发现 — 用户传 trajectories/TRJ-<id> 夹时读 manifest →
# currentRevision → trace.v3.json;v1/v2 平铺路径入口逐字不动(两入口
# 并存)。发现失败如实报错,不猜文件名、不回退平铺。
# ---------------------------------------------------------------------------


def test_replay_cli_v3_golden_via_trajectory_folder_manifest_discovery(
        monkeypatch, tmp_path, capsys):
    """§5 单夹发现入口第一片:replay 传 TRJ 夹 → manifest.json →
    currentRevision → trace.v3.json 加载 golden,照常路由受控链。
    平铺入口(同夹函数)用例逐字未动,本用例只钉发现面。"""
    import json as _json
    from agent.recording.trajectory_layout import TrajectoryLayout

    agent = _MCPAgent()
    _v3_cli_env(monkeypatch, tmp_path, agent)
    golden_path = _compile_v3_golden_same_window(tmp_path)
    # 真实生产写链:同 golden 经 TrajectoryLayout 落 §5 单夹(照 record
    # compile 链 _write_v3_trajectory 的实际形状)。
    record_dir = tmp_path / "record"
    layout = TrajectoryLayout.create(record_dir, "log-center-export")
    layout.write_revision_trace(
        "REV-0001", _json.loads(golden_path.read_text(encoding="utf-8")))
    trace_root = tmp_path / "traces"

    exit_code = cli.main(["--target", "win-dev", "replay",
                          str(layout.root),
                          "--trace-root", str(trace_root)])
    output = _json.loads(capsys.readouterr().out)

    assert exit_code == 0, output
    # 受控链真实跑完两步:发现入口与平铺入口在同一受控链汇合。
    assert output["runId"]
    assert [s["outcome"] for s in output["steps"]] == ["pass", "pass"]
    dispatches = [c for c in agent.calls if c[0] == "focus_bound_window"
                  and c[1].get("action_id")]
    assert len(dispatches) == 2


def test_replay_cli_v3_trajectory_folder_without_manifest_fails_honestly(
        monkeypatch, tmp_path, capsys):
    """发现失败如实报错:夹内无 manifest.json → golden_trace_invalid(
    不猜文件名、不回退平铺路径、不静默成功)。"""
    import json as _json

    agent = _MCPAgent()
    _v3_cli_env(monkeypatch, tmp_path, agent)
    empty = tmp_path / "record" / "trajectories" / "TRJ-0001"
    empty.mkdir(parents=True)
    trace_root = tmp_path / "traces"

    exit_code = cli.main(["--target", "win-dev", "replay", str(empty),
                          "--trace-root", str(trace_root)])
    output = _json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert output["ok"] is False
    assert output["code"] == "golden_trace_invalid"
    assert "manifest" in output["error"]
    # 未触碰目标:零派发。
    assert not [c for c in agent.calls if c[0] == "focus_bound_window"
                and c[1].get("action_id")]


def test_replay_cli_v3_trajectory_folder_pointer_without_trace_fails_closed(
        monkeypatch, tmp_path, capsys):
    """发布≠就绪的发现面语义:manifest 指针指向无 trace 的修订 →
    golden_trace_invalid(fail-closed,不冒充单夹自足)。"""
    import json as _json
    from agent.recording.trajectory_layout import TrajectoryLayout

    agent = _MCPAgent()
    _v3_cli_env(monkeypatch, tmp_path, agent)
    golden_path = _compile_v3_golden_same_window(tmp_path)
    record_dir = tmp_path / "record"
    layout = TrajectoryLayout.create(record_dir, "log-center-export")
    # 只写 REV-0002 的 trace,指针移到 REV-0003(无 trace)。
    (layout.root / "revisions" / "REV-0002").mkdir()
    layout.write_revision_trace(
        "REV-0002", _json.loads(golden_path.read_text(encoding="utf-8")))
    (layout.root / "revisions" / "REV-0003").mkdir()
    layout.publish_revision("REV-0002")
    manifest_path = layout.root / "manifest.json"
    manifest = _json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["currentRevision"] = "revisions/REV-0003"
    manifest_path.write_text(_json.dumps(manifest), encoding="utf-8")
    trace_root = tmp_path / "traces"

    exit_code = cli.main(["--target", "win-dev", "replay", str(layout.root),
                          "--trace-root", str(trace_root)])
    output = _json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert output["ok"] is False
    assert output["code"] == "golden_trace_invalid"
    assert not [c for c in agent.calls if c[0] == "focus_bound_window"
                and c[1].get("action_id")]


def test_replay_cli_v3_visual_golden_via_folder_resolves_and_dispatches(
        monkeypatch, tmp_path, capsys):
    """轮次 24 读侧正例(改写轮次 23 钉子):单夹发现入口 + 声明 visual 的
    golden,模板经 §5 assets/sha256/ 落单夹后,resolver 从 **TRJ 根**解析
    (asset_root=TRJ 根,非 REV 夹——落盘模板名已是 TRJ 根相对路径)→ A023
    派发全绿。钉住单夹自足:整夹复制后仍可定位资源,不再因模板留平铺夹
    而 fail-closed。"""
    import base64
    import dataclasses
    import hashlib
    import io
    import json as _json
    from PIL import Image, ImageDraw
    from agent.recording.models import GoldenTrace
    from agent.recording.trajectory_layout import TrajectoryLayout
    from test_case.test_recording.test_v3_real_server_adapter import (
        _compile_effect_golden)

    agent = _MCPAgent()
    backend = _v3_cli_env(monkeypatch, tmp_path, agent)
    # W2(Log Center)OS 事实 + A→B→A 语义效果接线(照 round-16 正例形状)。
    backend._windows["100"]["extra_controls"].append(
        {"automation_id": "btnBack", "text": "Apply"})
    backend._windows["200"] = {
        "process_id": 42, "title": "Log Center",
        "root_control_id": "LogCenterWindow",
        "visible": False, "foreground": True,
        "process_name": "EDRClient.exe",
        "extra_controls": [
            {"automation_id": "logRow", "text": "Apply"}]}
    original_click = backend.click

    def click_with_effect(**kwargs):
        out = original_click(**kwargs)
        if kwargs.get("automation_id") == "logRow":
            backend._windows["200"]["visible"] = False
        return out

    backend.click = click_with_effect

    def click_window_at(x, y, window_title_re=None):
        backend.dispatch_calls.append({
            "method": "click_window_at", "x": int(x), "y": int(y),
            "window_title_re": window_title_re})
        backend._windows["200"]["visible"] = True
        return {"ok": True, "method": "mouse.click",
                "window_relative": {"x": int(x), "y": int(y)}}

    backend.click_window_at = click_window_at

    # visual-only 模板(红底白十字,60x40)——digest 对齐一个真实 PNG。
    template = Image.new("RGB", (60, 40), (200, 30, 30))
    tdraw = ImageDraw.Draw(template)
    tdraw.line([(30, 5), (30, 35)], fill=(255, 255, 255), width=3)
    tdraw.line([(10, 20), (50, 20)], fill=(255, 255, 255), width=3)
    buf = io.BytesIO()
    template.save(buf, format="PNG")
    template_bytes = buf.getvalue()
    digest_hex = hashlib.sha256(template_bytes).hexdigest()

    golden = _compile_effect_golden()
    visual = {
        "template": f"assets/sha256/{digest_hex}.png",
        "elementSha256": "sha256:" + digest_hex,
        "redacted": True,
        "relativePoint": [0.5, 0.5],
    }
    first = dataclasses.replace(
        golden.steps[0],
        selector=dataclasses.replace(golden.steps[0].selector, control={},
                                     visual=visual))
    golden = GoldenTrace.from_dict(dataclasses.replace(
        golden, steps=(first, *golden.steps[1:])).to_dict())
    # golden 经生产写链落 §5 单夹;模板落单夹 assets/sha256/<hex>.png
    # (整夹自足——resolver 从 TRJ 根即可解析)。
    record_dir = tmp_path / "record"
    layout = TrajectoryLayout.create(record_dir, "log-center-export")
    (layout.root / "assets" / "sha256").mkdir(parents=True, exist_ok=True)
    (layout.root / "assets" / "sha256" / f"{digest_hex}.png").write_bytes(
        template_bytes)
    layout.write_revision_trace(
        "REV-0001", _json.loads(canonical_json(golden.to_dict())))

    # 观测假层(照 round-16 正例):灰底 + 唯一匹配帧,让 focus 权威观测
    # 成功、解析走图像匹配 → A023。
    frame = Image.new("RGB", (800, 600), (90, 90, 90))
    frame.paste(template, (0, 0))
    fbuf = io.BytesIO()
    frame.save(fbuf, format="PNG")
    frame_bytes = fbuf.getvalue()

    def screenshot(_path=None):
        return {"ok": True, "capture_scope": "window",
                "image_b64": base64.b64encode(frame_bytes).decode("ascii"),
                "origin": [0, 0]}

    backend.screenshot = screenshot
    backend.recorder_ui_rectangles = lambda: []
    backend.protected_rectangles = lambda: []
    trace_root = tmp_path / "traces"

    exit_code = cli.main(["--target", "win-dev", "replay", str(layout.root),
                          "--trace-root", str(trace_root),
                          "--replay-mode", "semantic_first"])
    output = _json.loads(capsys.readouterr().out)

    assert exit_code == 0, output
    # 三步全链(visual step-0001 + 语义 step-0002/0003)全 pass、各派发一次。
    assert output["latched"] is False, output["steps"]
    assert [s["outcome"] for s in output["steps"]] == \
        ["pass", "pass", "pass"], output["steps"]
    assert output["dispatchInputs"] == 3, output["steps"]
    # A023 恰一次(visual 步),窗口相对坐标 = 模板中心 relativePoint[0.5,0.5]
    # × 60x40 → 30,20。
    click_calls = [c for c in backend.dispatch_calls
                   if c["method"] == "click_window_at"]
    assert len(click_calls) == 1, backend.dispatch_calls
    assert click_calls[0]["x"] == 30 and click_calls[0]["y"] == 20, \
        click_calls[0]


def test_replay_cli_v3_visual_golden_via_folder_missing_template_fails_closed(
        monkeypatch, tmp_path, capsys):
    """轮次 24 读侧负例:单夹发现入口 + 声明 visual(模板名指向
    assets/sha256/<hex>.png)但**单夹内该模板文件缺失** → resolver 从
    TRJ 根找落空 → fail-closed visual_template_integrity_failed 锁存,零
    派发。不静默回退语义解析、不冒充单夹自足(模板必须真在单夹内)。"""
    import base64
    import dataclasses
    import hashlib
    import io
    import json as _json
    from PIL import Image, ImageDraw
    from agent.recording.models import GoldenTrace
    from agent.recording.trajectory_layout import TrajectoryLayout
    from test_case.test_recording.test_v3_real_server_adapter import (
        _compile_effect_golden)

    agent = _MCPAgent()
    backend = _v3_cli_env(monkeypatch, tmp_path, agent)

    template = Image.new("RGB", (60, 40), (200, 30, 30))
    tdraw = ImageDraw.Draw(template)
    tdraw.line([(30, 5), (30, 35)], fill=(255, 255, 255), width=3)
    tdraw.line([(10, 20), (50, 20)], fill=(255, 255, 255), width=3)
    buf = io.BytesIO()
    template.save(buf, format="PNG")
    template_bytes = buf.getvalue()
    digest_hex = hashlib.sha256(template_bytes).hexdigest()

    golden = _compile_effect_golden()
    visual = {
        "template": f"assets/sha256/{digest_hex}.png",
        "elementSha256": "sha256:" + digest_hex,
        "redacted": True,
        "relativePoint": [0.5, 0.5],
    }
    first = dataclasses.replace(
        golden.steps[0],
        selector=dataclasses.replace(golden.steps[0].selector, control={},
                                     visual=visual))
    golden = GoldenTrace.from_dict(dataclasses.replace(
        golden, steps=(first, *golden.steps[1:])).to_dict())
    # 落单夹 trace,但**不写** assets/sha256/<hex>.png:声明在、文件缺失。
    record_dir = tmp_path / "record"
    layout = TrajectoryLayout.create(record_dir, "log-center-export")
    layout.write_revision_trace(
        "REV-0001", _json.loads(canonical_json(golden.to_dict())))
    assert not (layout.root / "assets" / "sha256" /
                f"{digest_hex}.png").exists()

    frame = Image.new("RGB", (800, 600), (90, 90, 90))
    frame.paste(template, (0, 0))
    fbuf = io.BytesIO()
    frame.save(fbuf, format="PNG")
    frame_bytes = fbuf.getvalue()

    def screenshot(_path=None):
        return {"ok": True, "capture_scope": "window",
                "image_b64": base64.b64encode(frame_bytes).decode("ascii"),
                "origin": [0, 0]}

    backend.screenshot = screenshot
    backend.recorder_ui_rectangles = lambda: []
    backend.protected_rectangles = lambda: []
    trace_root = tmp_path / "traces"

    exit_code = cli.main(["--target", "win-dev", "replay", str(layout.root),
                          "--trace-root", str(trace_root),
                          "--replay-mode", "semantic_first"])
    output = _json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert output["ok"] is False
    # 受控链锁存在 visual 步:fail-closed 码族(模板完整性门),不冒充单夹
    # 自足、不静默回退语义解析。
    assert output["blockedAt"] == "step-0001"
    assert output["latched"] is True
    assert output["steps"][0]["code"] == "visual_template_integrity_failed"
    # 零真实派发(fail-closed 在解析门前)。
    assert not [c for c in agent.calls if c[0] == "focus_bound_window"
                and c[1].get("action_id")]


# ---------------------------------------------------------------------------
# 轮次 30(设计 §5 runs/ 面):CLI replay 单夹入口的受控执行账本入
# TRJ-<id>/runs/RUN-<id>/ — §5 "runs/: 每次执行账本"的最小落盘接线。
# 单夹入口:run 实例 id 由 CLI 生成(execution_ledger 公开别名 new_run_id),
# ledger root = TRJ 根/runs/<run实例id>(per-run 目录,受控链零改动)。
# 账本事件内 runId 保持 server 真实 run_id(1129 一致性,被适配器用例
# test_authoritative_state_run_id_matches_server_run 钉死)——目录名与事件
# runId 是两个如实报告的面(stdout ledgerRoot + runId/serverRunId),不为
# 接线破坏一致性契约。平铺入口:trace_root/v3-attempts 逐字不动(两入口
# 并存纪律)。
# ---------------------------------------------------------------------------


def test_replay_cli_v3_folder_entry_lands_attempt_ledger_in_trajectory_runs(
        monkeypatch, tmp_path, capsys):
    """单夹入口:replay 后账本事件落 TRJ 根/runs/RUN-<实例id>/EVT-*.json
    (目录名 RUN-<hex32>,§5 形状);事件内 runId == serverRunId(1129
    一致性不破坏);AttemptLedger(该目录).rebuild_history(serverRunId)
    可从盘上事件重建本次 run。"""
    import json as _json
    from agent.recording.trajectory_layout import TrajectoryLayout
    from target.windowing.attempt_ledger import AttemptLedger

    agent = _MCPAgent()
    _v3_cli_env(monkeypatch, tmp_path, agent)
    golden_path = _compile_v3_golden_same_window(tmp_path)
    record_dir = tmp_path / "record"
    layout = TrajectoryLayout.create(record_dir, "log-center-export")
    layout.write_revision_trace(
        "REV-0001", _json.loads(golden_path.read_text(encoding="utf-8")))
    trace_root = tmp_path / "traces"

    exit_code = cli.main(["--target", "win-dev", "replay",
                          str(layout.root),
                          "--trace-root", str(trace_root)])
    output = _json.loads(capsys.readouterr().out)

    assert exit_code == 0, output
    assert [s["outcome"] for s in output["steps"]] == ["pass", "pass"]
    # 受控链 runId 一致性(1129):输出 runId == serverRunId(server
    # begin_product_run 的真实 run),不以 stage 自建 ID 冒充。
    run_id = output["runId"]
    assert run_id and run_id == output["serverRunId"]
    # stdout 诚实反映实际落点:单夹入口 ledgerRoot = TRJ 根/runs/<RUN-*>,
    # 不再是指定的 trace_root/v3-attempts。
    ledger_root = output["ledgerRoot"]
    assert ledger_root.startswith(str(layout.root / "runs" / "RUN-"))
    run_dir = layout.root / "runs"
    run_dirs = [p for p in run_dir.iterdir() if p.is_dir()]
    assert len(run_dirs) == 1, "one replay lands exactly one run directory"
    assert str(run_dirs[0]) == ledger_root
    event_files = list(run_dirs[0].glob("EVT-*.json"))
    assert event_files, "attempt events must persist under the run directory"
    # 事件内 runId 是 server 真实 run:rebuild_history 按它可重建本次 run。
    history = AttemptLedger(run_dirs[0]).rebuild_history(run_id=run_id)
    assert history, "rebuild_history must reconstruct the run from disk"
    for event_file in event_files:
        assert _json.loads(event_file.read_text(encoding="utf-8"))["runId"] \
            == run_id
    # 账本不进指定 trace_root 的平铺受控面(单夹入口不两落)。
    assert not (trace_root / "v3-attempts").exists()
    # §5 "新的执行 attempt 仍有独立记录":同夹第二次 replay 落第二个
    # 独立 run 目录(实例 id 唯一,不覆盖第一次)。
    exit_code = cli.main(["--target", "win-dev", "replay",
                          str(layout.root),
                          "--trace-root", str(trace_root)])
    output2 = _json.loads(capsys.readouterr().out)
    assert exit_code == 0, output2
    assert output2["ledgerRoot"] != ledger_root, \
        "a new execution attempt gets its own run directory"
    run_dirs = [p for p in run_dir.iterdir() if p.is_dir()]
    assert sorted(p.name for p in run_dirs) == sorted([
        Path(ledger_root).name, Path(output2["ledgerRoot"]).name])
    assert list(Path(output2["ledgerRoot"]).glob("EVT-*.json")), \
        "second run directory carries its own events"


def test_replay_cli_v3_flat_entry_keeps_flat_ledger_root(
        monkeypatch, tmp_path, capsys):
    """平铺入口不回退、不进任何 TRJ 夹:ledger 仍落
    trace_root/v3-attempts(run_id 缺省自生成、runId 字段与事件落点
    解耦,平铺现状逐字不动)。"""
    import json as _json

    agent = _MCPAgent()
    _v3_cli_env(monkeypatch, tmp_path, agent)
    path = _compile_v3_golden_same_window(tmp_path)
    trace_root = tmp_path / "traces"

    exit_code = cli.main(["--target", "win-dev", "replay", str(path),
                          "--trace-root", str(trace_root)])
    output = _json.loads(capsys.readouterr().out)

    assert exit_code == 0, output
    assert output["ledgerRoot"] == str(trace_root / "v3-attempts")
    event_files = list((trace_root / "v3-attempts").glob("EVT-*.json"))
    assert event_files, "flat entry keeps the existing ledger landing"
    assert list((tmp_path / "record" / "trajectories").glob("TRJ-*")) == [], \
        "flat entry must not create any trajectory folder"


# ---------------------------------------------------------------------------
# 轮次 31(设计 §5 runs/ 面第二片):单夹入口受控执行 summary/report 落盘 —
# §5 "runs/: 每次执行账本、summary 与报告"剩余两个落盘点。summary.json =
# 受控链顶层 result 字段子集(ok/runId/serverRunId/outcome/latched/blockedAt/
# dispatchInputs/observations/steps 计数),与 stdout 投影同源同值;report.json
# = build_run_report 整份产物(受控链 result["report"],含 failedStep/steps
# 全条目)。两文件落 ledgerRoot(RUN 实例目录),write-once 原子(§7 报告审计
# 件不覆盖);blocked/begin-refused 零副作用路径同样落盘(诚实报告失败 run);
# 无 run 目录(账本未构造,如 role 推导在账本构造前失败)不落。平铺入口零
# 变化:runs/ 面是 §5 单夹宣告,平铺 ledgerRoot 不新增文件。
# ---------------------------------------------------------------------------


def test_replay_cli_v3_folder_entry_lands_summary_and_report_in_run_dir(
        monkeypatch, tmp_path, capsys):
    """pass run:summary.json(受控链顶层字段子集,与 stdout 逐键一致)+
    report.json(build_run_report 整份,含 steps 全条目)落 RUN 目录。"""
    import json as _json
    from agent.recording.trajectory_layout import TrajectoryLayout

    agent = _MCPAgent()
    _v3_cli_env(monkeypatch, tmp_path, agent)
    golden_path = _compile_v3_golden_same_window(tmp_path)
    record_dir = tmp_path / "record"
    layout = TrajectoryLayout.create(record_dir, "log-center-export")
    layout.write_revision_trace(
        "REV-0001", _json.loads(golden_path.read_text(encoding="utf-8")))
    trace_root = tmp_path / "traces"

    exit_code = cli.main(["--target", "win-dev", "replay", str(layout.root),
                          "--trace-root", str(trace_root),
                          "--replay-mode", "semantic_first"])
    output = _json.loads(capsys.readouterr().out)

    assert exit_code == 0, output
    assert [s["outcome"] for s in output["steps"]] == ["pass", "pass"]
    run_dir = Path(output["ledgerRoot"])
    assert run_dir.is_dir()
    # summary.json:受控链顶层 result 字段子集,与 stdout 同源同值。
    summary = _json.loads((run_dir / "summary.json").read_text(
        encoding="utf-8"))
    assert summary["ok"] == output["ok"] is True
    assert summary["runId"] == output["runId"] == output["serverRunId"]
    assert summary["outcome"] == "pass"
    assert summary["latched"] is False
    assert summary["blockedAt"] is None
    assert summary["dispatchInputs"] == output["dispatchInputs"] == 2
    assert summary["observations"] == output["observations"]
    assert summary["stepCount"] == len(output["steps"]) == 2
    assert summary["ledgerRoot"] == str(run_dir)
    # report.json:build_run_report 整份产物(受控链 report 键原样)。
    report = _json.loads((run_dir / "report.json").read_text(
        encoding="utf-8"))
    assert report["outcome"] == "pass"
    assert report["failedStep"] is None
    assert [s["stepId"] for s in report["steps"]] == \
        ["step-0001", "step-0002"]
    assert report["steps"][0]["outcome"] == "pass"


def test_replay_cli_v3_folder_entry_begin_refused_still_lands_honest_summary(
        monkeypatch, tmp_path, capsys):
    """begin-refused(租约冲突)零副作用路径同样落盘:summary 如实报
    blocked/runId null,report 如实 blocked——不只在 pass 时落。"""
    import json as _json
    from agent.recording.trajectory_layout import TrajectoryLayout
    from target import product_policy

    agent = _MCPAgent()
    _v3_cli_env(monkeypatch, tmp_path, agent)
    # 预置 active run 占用租约 → 本次 begin_product_run 冲突拒绝。
    from target.product_profiles import get_capability
    product_policy.begin_product_run(
        "windows_hisec",
        get_capability("windows_hisec", "windows_pywinauto").digest,
        lease_seconds=300, backend_kind="windows_pywinauto")
    golden_path = _compile_v3_golden_same_window(tmp_path)
    record_dir = tmp_path / "record"
    layout = TrajectoryLayout.create(record_dir, "log-center-export")
    layout.write_revision_trace(
        "REV-0001", _json.loads(golden_path.read_text(encoding="utf-8")))
    trace_root = tmp_path / "traces"

    exit_code = cli.main(["--target", "win-dev", "replay", str(layout.root),
                          "--trace-root", str(trace_root)])
    output = _json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert output["ok"] is False
    assert output["latched"] is True
    assert output["steps"][0]["reason"] == "begin refused"
    # 零真实派发。
    assert not [c for c in agent.calls if c[0] == "focus_bound_window"
                and c[1].get("action_id")]
    # 失败 run 仍诚实落盘(目录沿用 stdout ledgerRoot)。
    run_dir = Path(output["ledgerRoot"])
    assert run_dir.is_dir()
    summary = _json.loads((run_dir / "summary.json").read_text(
        encoding="utf-8"))
    assert summary["ok"] is False
    assert summary["runId"] is None
    assert summary["serverRunId"] is None
    assert summary["outcome"] == "blocked"
    assert summary["latched"] is True
    report = _json.loads((run_dir / "report.json").read_text(
        encoding="utf-8"))
    assert report["outcome"] == "blocked"
    assert report["steps"] == []
    # 账本事件面:begin 拒绝无 hook(受控链在构造 hook 前早退),RUN
    # 目录内零 EVT-*.json——落盘的是 summary/report 两个报告面。
    assert list(run_dir.glob("EVT-*.json")) == []


def test_replay_cli_v3_role_derivation_failure_lands_nothing_in_trajectory(
        monkeypatch, tmp_path, capsys):
    """role 推导在账本构造前失败 → 无 run 目录 → 不落 summary/report
    (无 run 目录不落;stdout 照旧 v3_role_derivation_failed)。"""
    import json as _json
    from agent.recording.trajectory_layout import TrajectoryLayout

    agent = _MCPAgent()
    _v3_cli_env(monkeypatch, tmp_path, agent)
    golden_path = _compile_v3_golden_same_window(tmp_path)
    # 篡改 title 到无角色匹配的值(与既有 role-derivation 用例同法:
    # selector/identity/entryWindow 同步改,仍通过 load 一致性校验,
    # 仅角色派生无解),走真实 load 路径。
    import json as _json
    record_dir = tmp_path / "record"
    layout = TrajectoryLayout.create(record_dir, "log-center-export")
    doc = _json.loads(golden_path.read_text(encoding="utf-8"))
    doc["windowSequence"]["windows"][0]["identity"]["titleRegex"] = \
        "^Totally-Unknown-Title$"
    for step in doc["steps"].values():
        if isinstance(step, dict) and (step.get("selector") or {}).get("window"):
            step["selector"]["window"]["titleRegex"] = \
                "^Totally-Unknown-Title$"
    if doc.get("entryWindow"):
        doc["entryWindow"]["titleRegex"] = "^Totally-Unknown-Title$"
    layout.write_revision_trace("REV-0001", doc)
    trace_root = tmp_path / "traces"

    exit_code = cli.main(["--target", "win-dev", "replay", str(layout.root),
                          "--trace-root", str(trace_root)])
    output = _json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert output["ok"] is False
    assert output["code"] == "v3_role_derivation_failed"
    # 无 run 目录(账本从未构造)→ 无 summary/report 落点:runs/ 下
    # 零 RUN-* 目录、零报告文件(不预建占位)。
    runs_dir = layout.root / "runs"
    assert not list(runs_dir.glob("RUN-*"))
    assert not (runs_dir / "summary.json").exists()
    assert not (runs_dir / "report.json").exists()


def test_replay_cli_v3_run_summary_report_write_once(
        monkeypatch, tmp_path):
    """write-once 直调反例(同夹二写经公开链不可达——RUN 目录恒新):
    已有 summary.json/report.json 字节不同预置 → 落盘函数不覆盖、
    字节保持。"""
    from agent.cli import _persist_run_summary_report

    run_dir = tmp_path / "RUN-a"
    run_dir.mkdir(parents=True)
    summary_path = run_dir / "summary.json"
    report_path = run_dir / "report.json"
    summary_path.write_bytes(b'{"existing": "summary"}\n')
    report_path.write_bytes(b'{"existing": "report"}\n')

    _persist_run_summary_report(
        run_dir, {"ok": True, "runId": "R", "serverRunId": "R",
                  "steps": [], "latched": False, "blockedAt": None,
                  "dispatchInputs": 0, "observations": 0,
                  "report": {"outcome": "pass", "failedStep": None,
                             "steps": []}},
        ledger_root_str=str(run_dir))

    assert summary_path.read_bytes() == b'{"existing": "summary"}\n'
    assert report_path.read_bytes() == b'{"existing": "report"}\n'


def test_replay_cli_v3_flat_entry_adds_no_summary_or_report_files(
        monkeypatch, tmp_path, capsys):
    """平铺入口零新增:§5 runs/ 面是单夹宣告——trace_root/v3-attempts
    内不落 summary.json/report.json(两入口并存纪律不回退)。"""
    import json as _json

    agent = _MCPAgent()
    _v3_cli_env(monkeypatch, tmp_path, agent)
    path = _compile_v3_golden_same_window(tmp_path)
    trace_root = tmp_path / "traces"

    exit_code = cli.main(["--target", "win-dev", "replay", str(path),
                          "--trace-root", str(trace_root)])
    output = _json.loads(capsys.readouterr().out)

    assert exit_code == 0, output
    assert output["ledgerRoot"] == str(trace_root / "v3-attempts")
    flat = trace_root / "v3-attempts"
    assert list(flat.glob("EVT-*.json")), "flat ledger events unchanged"
    assert not (flat / "summary.json").exists()
    assert not (flat / "report.json").exists()


def test_replay_cli_v3_folder_move_with_old_root_unreachable_stays_consumable(
        monkeypatch, tmp_path, capsys):
    """收尾包①反例(轮次 38 链级验证的执行面收口):整夹复制后**旧目录
    不可访问**(record_dir 整体改名移走,旧路径不存在)→ CLI 从新根经
    manifest 发现入口完整回放——visual 模板从新夹 assets/sha256/ 解析、
    A023 派发全绿、summary/report 落新夹 runs/。钉住"不能只验证路径
    解析 helper":消费走真实 CLI 链,旧根消失后任何对旧路径的依赖都会
    在此红。"""
    import base64
    import hashlib
    import io
    import json as _json
    import shutil
    from PIL import Image, ImageDraw
    from agent.recording.models import GoldenTrace
    from agent.recording.trajectory_layout import TrajectoryLayout
    from test_case.test_recording.test_v3_real_server_adapter import (
        _compile_effect_golden)

    agent = _MCPAgent()
    backend = _v3_cli_env(monkeypatch, tmp_path, agent)
    backend._windows["100"]["extra_controls"].append(
        {"automation_id": "btnBack", "text": "Apply"})
    backend._windows["200"] = {
        "process_id": 42, "title": "Log Center",
        "root_control_id": "LogCenterWindow",
        "visible": False, "foreground": True,
        "process_name": "EDRClient.exe",
        "extra_controls": [
            {"automation_id": "logRow", "text": "Apply"}]}
    original_click = backend.click

    def click_with_effect(**kwargs):
        out = original_click(**kwargs)
        if kwargs.get("automation_id") == "logRow":
            backend._windows["200"]["visible"] = False
        return out

    backend.click = click_with_effect

    def click_window_at(x, y, window_title_re=None):
        backend.dispatch_calls.append({
            "method": "click_window_at", "x": int(x), "y": int(y),
            "window_title_re": window_title_re})
        backend._windows["200"]["visible"] = True
        return {"ok": True, "method": "mouse.click",
                "window_relative": {"x": int(x), "y": int(y)}}

    backend.click_window_at = click_window_at

    # visual-only 模板(红底白十字,60x40,digest 对齐真实 PNG)。
    template = Image.new("RGB", (60, 40), (200, 30, 30))
    tdraw = ImageDraw.Draw(template)
    tdraw.line([(30, 5), (30, 35)], fill=(255, 255, 255), width=3)
    tdraw.line([(10, 20), (50, 20)], fill=(255, 255, 255), width=3)
    buf = io.BytesIO()
    template.save(buf, format="PNG")
    template_bytes = buf.getvalue()
    digest_hex = hashlib.sha256(template_bytes).hexdigest()

    golden = _compile_effect_golden()
    visual = {
        "template": f"assets/sha256/{digest_hex}.png",
        "elementSha256": "sha256:" + digest_hex,
        "redacted": True,
        "relativePoint": [0.5, 0.5],
    }
    first = replace(
        golden.steps[0],
        selector=replace(golden.steps[0].selector, control={},
                         visual=visual))
    golden = GoldenTrace.from_dict(replace(
        golden, steps=(first, *golden.steps[1:])).to_dict())
    # 生产写链落 §5 单夹 + 模板落单夹(整夹自足)。
    record_dir = tmp_path / "record"
    layout = TrajectoryLayout.create(record_dir, "log-center-export")
    (layout.root / "assets" / "sha256").mkdir(parents=True, exist_ok=True)
    (layout.root / "assets" / "sha256" / f"{digest_hex}.png").write_bytes(
        template_bytes)
    layout.write_revision_trace(
        "REV-0001", _json.loads(canonical_json(golden.to_dict())))

    # 整夹复制到新根,然后旧目录整体改名移走(旧路径不可访问)。
    moved_record = tmp_path / "moved-record"
    moved_record.mkdir()
    shutil.copytree(layout.root, moved_record / layout.trajectory_id)
    old_path = layout.root
    record_dir.rename(tmp_path / "record-gone")
    assert not old_path.exists(), "场景前提:旧目录已不可访问"

    # 观测假层(照 round-16 正例):灰底 + 唯一匹配帧。
    frame = Image.new("RGB", (800, 600), (90, 90, 90))
    frame.paste(template, (0, 0))
    fbuf = io.BytesIO()
    frame.save(fbuf, format="PNG")
    frame_bytes = fbuf.getvalue()

    def screenshot(_path=None):
        return {"ok": True, "capture_scope": "window",
                "image_b64": base64.b64encode(frame_bytes).decode("ascii"),
                "origin": [0, 0]}

    backend.screenshot = screenshot
    backend.recorder_ui_rectangles = lambda: []
    backend.protected_rectangles = lambda: []
    trace_root = tmp_path / "traces"

    exit_code = cli.main(["--target", "win-dev", "replay",
                          str(moved_record / layout.trajectory_id),
                          "--trace-root", str(trace_root),
                          "--replay-mode", "semantic_first"])
    output = _json.loads(capsys.readouterr().out)

    assert exit_code == 0, output
    assert output["latched"] is False, output["steps"]
    assert [s["outcome"] for s in output["steps"]] == \
        ["pass", "pass", "pass"], output["steps"]
    assert output["dispatchInputs"] == 3, output["steps"]
    # visual 模板从新夹解析(旧根不可访问仍唯一匹配派发)。
    click_calls = [c for c in backend.dispatch_calls
                   if c["method"] == "click_window_at"]
    assert len(click_calls) == 1, backend.dispatch_calls
    assert click_calls[0]["x"] == 30 and click_calls[0]["y"] == 20
    # summary/report 落新夹 runs/(执行面落点也随新夹)。
    run_dir = Path(output["ledgerRoot"])
    assert run_dir.is_dir()
    assert run_dir.relative_to(moved_record / layout.trajectory_id) \
        .parts[0] == "runs"
    summary = _json.loads((run_dir / "summary.json").read_text(
        encoding="utf-8"))
    assert summary["ok"] is True and summary["outcome"] == "pass"
    assert (run_dir / "report.json").is_file()
