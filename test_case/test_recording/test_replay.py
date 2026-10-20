from __future__ import annotations

import base64
import hashlib
from dataclasses import replace
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
