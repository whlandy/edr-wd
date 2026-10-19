from __future__ import annotations

import runpy

import pytest

from action_dispatcher import ActionReceipt
from agent.execution import AtomicExecutor
from agent.recording.artifacts import write_compilation_artifacts
from agent.recording.generate_pytest import render_conftest
from agent.recording.models import GoldenTrace, RecordedStep, ReplaySelector
from agent.recording.replay import load_golden_trace
from agent.recording.compiler import compile_recording
from agent.recording.replay import ReplayRuntime
from target.action_catalog import CATALOG_VERSION, catalog_digest
from target.recording.models import RawRecording


pytestmark = pytest.mark.unit


class _FreshSessionObservations:
    def __init__(self, session: str) -> None:
        self.session = session
        self.counter = 0
        self.snapshots = {}

    def refresh(self) -> str:
        self.counter += 1
        snapshot_id = f"OBS-{self.session}-{self.counter}"
        self.snapshots[snapshot_id] = {
            "snapshot_id": snapshot_id,
            "controls": [{
                "target_id": f"T-{self.session}-{self.counter}",
                "process_name": "EDRClient.exe",
                "window_title": "EDRClient",
                "automation_id": "btnApply",
                "control_type": "Button",
                "text": "应用",
            }],
            "windows": [{"process_name": "EDRClient.exe", "title": "EDRClient"}],
            "active_window": {
                "process_name": "EDRClient.exe",
                "title": "EDRClient",
                "rect": [0, 0, 800, 600],
            },
        }
        return snapshot_id

    def latest_snapshot_id(self):
        return f"OBS-{self.session}-{self.counter}" if self.counter else None

    def get_snapshot(self, snapshot_id):
        return self.snapshots[snapshot_id]


def _runtime(session: str):
    observations = _FreshSessionObservations(session)
    calls = []

    def dispatch(**kwargs):
        calls.append(kwargs)
        return ActionReceipt.from_ok(
            action_id=kwargs["action_id"],
            action_code=kwargs["action_code"],
            request_id=kwargs["request_id"],
            result={"ok": True},
        )

    return ReplayRuntime(
        executor=AtomicExecutor(
            dispatch=dispatch,
            observation_provider=observations,
        ),
        catalog_version=CATALOG_VERSION,
        catalog_digest=catalog_digest(),
    ), calls


def test_generated_pytest_executes_twice_in_distinct_fresh_sessions(tmp_path):
    recording = RawRecording.from_dict({
        "schema": "edr.desktop-recording/v1",
        "sessionId": "REC-PROJECTION",
        "name": "generated projection",
        "captureDiagnostics": {"droppedPackets": 0, "correlationErrorCount": 0},
        "events": [{
            "sequence": 1,
            "wallTime": "2026-08-17T00:00:00Z",
            "monotonicMs": 10,
            "type": "pointer_click",
            "scope": {
                "target": "win-dev",
                "backend": "windows_pywinauto",
                "processName": "EDRClient.exe",
                "windowTitle": "^EDRClient$",
            },
            "input": {"button": "left", "clickCount": 1, "screenPoint": [40, 30]},
            "observedTarget": {
                "snapshotId": "OBS-RECORDED",
                "targetId": "T-RECORDED",
                "fingerprint": "sha256:recorded",
                "controlType": "Button",
                "automationId": "btnApply",
                "text": "应用",
                "protected": False,
            },
            "evidence": {},
            "causalId": "CAUSE-1",
            "assertion": None,
        }],
    })
    artifacts = write_compilation_artifacts(
        tmp_path, recording, compile_recording(recording),
    )
    namespace = runpy.run_path(str(artifacts.generated_test))
    generated_test = namespace["test_generated_projection"]
    golden = load_golden_trace(artifacts.golden_trace)

    all_snapshot_ids = []
    for session in ("A", "B"):
        runtime, calls = _runtime(session)
        generated_test(runtime, golden)
        assert len(calls) == 1
        all_snapshot_ids.append(calls[0]["target_ref"]["snapshot_id"])

    assert all_snapshot_ids[0].startswith("OBS-A-")
    assert all_snapshot_ids[1].startswith("OBS-B-")
    assert all_snapshot_ids[0] != all_snapshot_ids[1]


@pytest.mark.parametrize("image_only,expected_mode", [
    (False, "semantic_only"), (True, "semantic_first"),
])
def test_generated_fixture_selects_a_replay_mode_that_can_run_its_trace(
    tmp_path, monkeypatch, image_only, expected_mode,
):
    import agent.recording.runtime as replay_runtime
    import agent.subagent.target_agent as target_agent_module
    import agent.target_config as target_config_module

    class FakeConfig:
        def __init__(self, _path):
            pass

        def get_default_target(self):
            return "win-dev"

        def get_target_app_profile(self, target):
            assert target == "win-dev"
            return "windows_hisec"

    class FakeAgent:
        def __init__(self, _target, config):
            pass

        def ensure_ready(self):
            return {"ok": True}

    called = []

    def fake_build(_agent, _golden, **kwargs):
        called.append(kwargs)
        return object()

    monkeypatch.setattr(target_config_module, "TargetConfig", FakeConfig)
    monkeypatch.setattr(target_agent_module, "TargetSubAgent", FakeAgent)
    monkeypatch.setattr(replay_runtime, "build_replay_runtime", fake_build)
    monkeypatch.delenv("EDR_WD_REPLAY_MODE", raising=False)
    fixture_file = tmp_path / "conftest.py"
    fixture_file.write_text(render_conftest(), encoding="utf-8")
    fixture = runpy.run_path(str(fixture_file))["edr_wd_target"].__wrapped__
    selector = ReplaySelector(
        window={"processName": "EDRClient.exe", "titleRegex": "^EDRClient$"},
        control={} if image_only else {"automationId": "btnApply"},
        visual={"template": "assets/apply.png", "redacted": True,
                "elementSha256": "sha256:" + "a" * 64} if image_only else None,
    )
    golden = GoldenTrace(
        name="generated fixture", source_recording={"schema": "edr.desktop-recording/v1",
                                                    "sha256": "sha256:raw"},
        catalog={"version": "1.0.0", "digest": "sha256:catalog"},
        environment={"backend": "windows_pywinauto", "target": "win-dev"},
        steps=(RecordedStep("step-0001", "gui.click", {}, selector),), status="ready",
    )

    fixture(golden)

    assert called[0]["replay_mode"] == expected_mode
    assert called[0]["profile"] == "windows_hisec"
