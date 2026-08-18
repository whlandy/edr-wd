from __future__ import annotations

import runpy

import pytest

from action_dispatcher import ActionReceipt
from agent.execution import AtomicExecutor
from agent.recording.artifacts import write_compilation_artifacts
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
