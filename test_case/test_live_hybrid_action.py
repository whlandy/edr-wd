"""Live semantic and visual paths materialize through one locator/executor."""

import pytest

from agent.execution import StepMaterializationError
from agent.interaction.runtime import execute_live_action
from agent.locator import LocatorPolicy, LocatorRequest
from agent.recording.visual import SafeVisualResolver, VisualCandidate


class CapturingExecutor:
    def __init__(self, observation):
        self.observation = observation
        self.step = None

    def run_case(self, case, *, step_materializer, execution_context):
        self.step = step_materializer(case.steps[0], self.observation)
        return self.step


def _bundle(controls):
    return {
        "observation_id": "OBS-live", "snapshot_id": "OBS-live",
        "window_identity": {"native_id": "7", "rect": [20, 30, 220, 230]},
        "active_window": {"process_name": "Demo", "title": "Main", "rect": [20, 30, 220, 230]},
        "screenshot_scope": "window", "screenshot_redacted": True,
        "controls": controls,
    }


def test_live_semantic_click_uses_current_target_reference():
    executor = CapturingExecutor(_bundle([{
        "target_id": "T1", "process_name": "Demo", "window_title": "Main",
        "text": "Open", "rect": [40, 50, 80, 90],
    }]))
    step = execute_live_action(
        LocatorRequest("gui.click", {"processName": "Demo"}, {"name": "Open"}),
        executor=executor, profile="macos_generic",
    )
    assert step.action_id == "gui.click"
    assert step.target_ref.target_id == "T1"


def test_live_visual_click_is_window_relative_and_guarded_action():
    executor = CapturingExecutor(_bundle([]))
    step = execute_live_action(
        LocatorRequest("gui.click", {"processName": "Demo"}, {"name": "Open"}),
        executor=executor, profile="macos_generic",
        policy=LocatorPolicy(mode="visual_only"),
        visual_candidates=({"rect": [40, 50, 80, 90], "confidence": 0.99,
                            "observation_id": "OBS-live", "window_native_id": "7"},),
    )
    assert step.action_id == "pointer.click_window"
    assert step.args["x"] == 40 and step.args["y"] == 40
    assert step.target_ref is None


def test_live_action_collects_template_evidence_from_fresh_window_observation():
    executor = CapturingExecutor(_bundle([]))
    resolver = SafeVisualResolver(
        lambda template, observation: [VisualCandidate((40, 50, 80, 90), 0.99)],
        template_verifier=lambda template, visual: True,
    )
    step = execute_live_action(
        LocatorRequest("gui.click", {"processName": "Demo"}, {"name": "Open"}),
        executor=executor, profile="macos_generic",
        visual_selector={"template": "reviewed/open.png", "redacted": True,
                         "elementSha256": "sha256:" + "a" * 64},
        visual_resolver=resolver,
    )
    assert step.action_id == "pointer.click_window"
    assert step.args["x"] == 40 and step.args["y"] == 40


def test_live_image_only_click_rejects_sole_control_when_template_has_no_hit():
    executor = CapturingExecutor(_bundle([{
        "target_id": "T1", "process_name": "Demo", "window_title": "Main",
        "text": "Cancel", "rect": [40, 50, 80, 90],
    }]))
    resolver = SafeVisualResolver(
        lambda _template, _observation: [],
        template_verifier=lambda _template, _visual: True,
    )

    with pytest.raises(StepMaterializationError) as exc:
        execute_live_action(
            LocatorRequest("gui.click", {"processName": "Demo"}, {}),
            executor=executor, profile="macos_generic",
            visual_selector={"template": "reviewed/open.png", "redacted": True,
                             "elementSha256": "sha256:" + "a" * 64},
            visual_resolver=resolver,
        )

    assert exc.value.code == "locator_low_confidence"
    assert executor.step is None
