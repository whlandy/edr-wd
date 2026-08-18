from __future__ import annotations

import json
import base64
import hashlib
import io

import pytest
from PIL import Image

from agent import cli
from target.recording.models import CaptureScope, ObservedTarget, RawCaptureEvent
from target.recording.service import RecordingService, error_result
from target.recording.session import NullCaptureSource

pytestmark = pytest.mark.unit


class _FakeSource(NullCaptureSource):
    def __init__(self) -> None:
        self.calls: list[str] = []

    def start(self) -> None:
        self.calls.append("start")

    def pause(self) -> None:
        self.calls.append("pause")

    def resume(self) -> None:
        self.calls.append("resume")

    def stop(self) -> None:
        self.calls.append("stop")


class _FakeAgent:
    def __init__(
        self,
        *,
        stop_recording: dict | None = None,
        captures: dict | None = None,
        recording_status: dict | None = None,
    ) -> None:
        self.calls: list[tuple[str, dict, float | None]] = []
        self.stop_recording = stop_recording
        self.captures = captures or {}
        self.recording_status = recording_status or {
            "ok": False, "code": "recording_session_missing",
        }

    def ensure_ready(self) -> dict:
        return {"ok": True}

    def call_tool(self, name: str, arguments: dict | None = None, timeout: float | None = None) -> dict:
        self.calls.append((name, arguments or {}, timeout))
        if name == "recording_status":
            return self.recording_status
        if name == "stop_recording" and self.stop_recording is not None:
            return self.stop_recording
        if name == "get_recording_capture":
            return self.captures.get(arguments.get("capture_id"), {
                "ok": False, "code": "recording_capture_missing",
            })
        return {"ok": True, "state": "recording"}


class _FakeIndicator:
    def __init__(self):
        self.calls = []

    def start(self): self.calls.append("start")
    def pause(self): self.calls.append("pause")
    def resume(self): self.calls.append("resume")
    def update_count(self, count): self.calls.append(("count", count))
    def open_assertion_editor(self, target=None): self.calls.append(("assert", target) if target else "assert")
    def stop(self): self.calls.append("stop")


class _FakeEvidenceStore:
    def __init__(self):
        self.calls = []

    def clear(self): self.calls.append("clear")
    def initialize(self):
        self.calls.append("initialize")
        return {"ok": True, "capture": {"id": "CAP-" + "0" * 32}}
    def attach(self, event): return event
    def get(self, capture_id): return {"ok": False, "captureId": capture_id}


def test_recording_service_exposes_complete_lifecycle_and_raw_document():
    sources: list[_FakeSource] = []

    def source_factory(scope: CaptureScope, _sink) -> _FakeSource:
        assert scope.process_name == "EDRClient.exe"
        source = _FakeSource()
        sources.append(source)
        return source

    service = RecordingService(
        target_name="win-dev",
        backend="windows_pywinauto",
        source_factory=source_factory,
    )

    started = service.start(
        name="policy flow",
        process_name="EDRClient.exe",
        window_title="^EDRClient$",
        lease_seconds=30,
    )
    assert started["ok"] is True and started["state"] == "recording"
    assert service.status(heartbeat=True)["sessionId"] == started["sessionId"]
    assert service.pause()["state"] == "paused"
    assert service.resume()["state"] == "recording"
    stopped = service.stop()
    assert stopped["state"] == "stopped"
    assert stopped["recording"]["schema"] == "edr.desktop-recording/v1"
    assert sources[0].calls == ["start", "pause", "resume", "stop"]


def test_recording_service_initializes_evidence_once_and_active_restart_preserves_it():
    evidence = _FakeEvidenceStore()
    service = RecordingService(
        target_name="win-dev",
        backend="windows_pywinauto",
        source_factory=lambda _scope, _sink: _FakeSource(),
        evidence_store=evidence,
    )

    started = service.start(
        name="flow", process_name="EDRClient.exe", window_title="^EDRClient$",
    )
    assert started["evidenceInitialization"]["ok"] is True
    assert evidence.calls == ["clear", "initialize"]

    with pytest.raises(Exception) as exc:
        service.start(
            name="second", process_name="EDRClient.exe", window_title="^EDRClient$",
        )
    assert getattr(exc.value, "code", None) == "recording_session_active"
    assert evidence.calls == ["clear", "initialize"]
    service.stop()


def test_recording_service_returns_stable_unavailable_error_by_default():
    service = RecordingService(target_name="mac", backend="macos_accessibility")
    result = error_result(
        pytest.raises(
            Exception,
            service.start,
            name="flow",
            process_name="EDRClient",
            window_title="^EDRClient$",
        ).value
    )
    assert result["ok"] is False
    assert result["code"] == "recording_capture_unavailable"


def test_record_start_connects_and_locks_before_starting(monkeypatch, capsys):
    agent = _FakeAgent()
    monkeypatch.setattr(cli, "_target_agent", lambda *_args: agent)

    exit_code = cli.main([
        "--target", "win-dev", "record", "start",
        "--name", "policy flow",
        "--process-name", "EDRClient.exe",
        "--window-title", "^EDRClient$",
        "--lease-seconds", "15",
    ])
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 0 and output["ok"] is True
    assert [call[0] for call in agent.calls] == [
        "recording_status", "connect", "lock_window", "verify_window_lock",
        "start_recording",
    ]
    assert agent.calls[-1][1] == {
        "name": "policy flow",
        "process_name": "EDRClient.exe",
        "window_title": "^EDRClient$",
        "lease_seconds": 15.0,
    }


def test_record_status_renews_lease_by_default(monkeypatch, capsys):
    agent = _FakeAgent(recording_status={"ok": True, "state": "recording"})
    monkeypatch.setattr(cli, "_target_agent", lambda *_args: agent)

    assert cli.main(["--target", "win-dev", "record", "status"]) == 0
    capsys.readouterr()
    assert agent.calls == [("recording_status", {"heartbeat": True}, None)]


def test_record_start_rejects_an_active_session_before_connecting(monkeypatch, capsys):
    agent = _FakeAgent(recording_status={"ok": True, "state": "paused"})
    monkeypatch.setattr(cli, "_target_agent", lambda *_args: agent)

    exit_code = cli.main([
        "--target", "win-dev", "record", "start",
        "--name", "policy flow",
        "--process-name", "EDRClient.exe",
        "--window-title", "^EDRClient$",
    ])
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert output["code"] == "recording_session_active"
    assert [call[0] for call in agent.calls] == ["recording_status"]


def test_record_stop_persists_and_compiles_agent_side_artifacts(monkeypatch, tmp_path, capsys):
    raw = {
        "schema": "edr.desktop-recording/v1",
        "sessionId": "REC-1",
        "name": "policy flow",
        "captureDiagnostics": {"droppedPackets": 0, "correlationErrorCount": 0},
        "events": [],
    }
    agent = _FakeAgent(stop_recording={"ok": True, "state": "stopped", "recording": raw})
    monkeypatch.setattr(cli, "_target_agent", lambda *_args: agent)

    exit_code = cli.main([
        "--target", "win-dev", "record", "stop", "--output-root", str(tmp_path),
        "--profile", "windows_hisec",
    ])
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert output["directory"] == str(tmp_path / "policy_flow")
    assert (tmp_path / "policy_flow" / "recording.json").exists()
    assert (tmp_path / "policy_flow" / "golden-trace.json").exists()
    golden = json.loads(
        (tmp_path / "policy_flow" / "golden-trace.json").read_text()
    )
    assert golden["environment"]["profile"] == "windows_hisec"


def test_record_stop_fetches_redacted_captures_and_builds_visual_assets(monkeypatch, tmp_path, capsys):
    output = io.BytesIO()
    Image.new("RGB", (120, 80), "blue").save(output, format="PNG")
    payload = output.getvalue()
    digest = "sha256:" + hashlib.sha256(payload).hexdigest()
    capture_id = "CAP-" + "1" * 32
    before_capture_id = "CAP-" + "0" * 32
    raw = {
        "schema": "edr.desktop-recording/v1",
        "sessionId": "REC-1",
        "name": "visual flow",
        "captureDiagnostics": {"droppedPackets": 0, "correlationErrorCount": 0},
        "events": [{
            "sequence": 1,
            "wallTime": "2026-08-17T00:00:00Z",
            "monotonicMs": 10,
            "type": "pointer_click",
            "scope": {
                "target": "win-dev", "backend": "windows_pywinauto",
                "processName": "EDRClient.exe", "windowTitle": "^EDRClient$",
            },
            "input": {"screenPoint": [30, 30]},
            "observedTarget": {
                "snapshotId": "OBS-1", "targetId": "T0001",
                "fingerprint": "sha256:button", "controlType": "Button",
                "automationId": "btnApply", "text": "应用",
                "rect": [10, 10, 60, 50], "protected": False,
            },
            "evidence": {"beforeCapture": {
                "id": before_capture_id, "sha256": digest,
                "width": 120, "height": 80, "origin": [0, 0],
                "redacted": True, "redactions": [],
            }, "capture": {
                "id": capture_id, "sha256": digest, "width": 120, "height": 80,
                "origin": [0, 0], "redacted": True, "redactions": [],
            }},
        }],
    }
    agent = _FakeAgent(
        stop_recording={"ok": True, "state": "stopped", "recording": raw},
        captures={item: {
            "ok": True, "captureId": item, "sha256": digest,
            "image_b64": base64.b64encode(payload).decode("ascii"),
        } for item in (before_capture_id, capture_id)},
    )
    monkeypatch.setattr(cli, "_target_agent", lambda *_args: agent)

    exit_code = cli.main([
        "--target", "win-dev", "record", "stop", "--output-root", str(tmp_path),
    ])
    result = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert result["captureIssues"] == []
    capture_calls = [call for call in agent.calls if call[0] == "get_recording_capture"]
    assert [call[1]["capture_id"] for call in capture_calls] == [
        before_capture_id, capture_id,
    ]
    assert len(list((tmp_path / "visual_flow/assets/observations").glob("*.png"))) == 2
    golden = json.loads((tmp_path / "visual_flow/golden-trace.json").read_text())
    assert golden["steps"]["step-0001"]["selector"]["visual"]["redacted"] is True


def test_record_stop_persists_capture_transfer_failures_in_compile_report(
    monkeypatch, tmp_path, capsys,
):
    capture_id = "CAP-" + "1" * 32
    raw = {
        "schema": "edr.desktop-recording/v1",
        "sessionId": "REC-1",
        "name": "missing capture",
        "captureDiagnostics": {
            "droppedPackets": 0, "correlationErrorCount": 0,
        },
        "events": [{
            "sequence": 1,
            "wallTime": "2026-08-17T00:00:00Z",
            "monotonicMs": 10,
            "type": "pointer_click",
            "scope": {
                "target": "win-dev", "backend": "windows_pywinauto",
                "processName": "EDRClient.exe", "windowTitle": "^EDRClient$",
            },
            "input": {"button": "left"},
            "observedTarget": {
                "snapshotId": "OBS-1", "targetId": "T0001",
                "fingerprint": "sha256:button", "controlType": "Button",
                "automationId": "btnApply", "protected": False,
            },
            "evidence": {"capture": {
                "id": capture_id,
                "sha256": "sha256:" + "a" * 64,
                "width": 100, "height": 50, "origin": [0, 0],
                "redacted": True, "redactions": [],
            }},
        }],
    }
    agent = _FakeAgent(
        stop_recording={"ok": True, "state": "stopped", "recording": raw},
    )
    monkeypatch.setattr(cli, "_target_agent", lambda *_args: agent)

    assert cli.main([
        "--target", "win-dev", "record", "stop",
        "--output-root", str(tmp_path),
    ]) == 0
    output = json.loads(capsys.readouterr().out)
    report = json.loads(
        (tmp_path / "missing_capture" / "compile-report.json").read_text()
    )

    assert output["captureIssues"][0]["captureId"] == capture_id
    assert report["artifactIssues"] == output["captureIssues"]


def test_visible_indicator_and_explicit_assertion_are_part_of_session_lifecycle():
    indicator = _FakeIndicator()
    captured = {}

    def indicator_factory(_name, _scope, callbacks):
        captured["callbacks"] = callbacks
        return indicator

    service = RecordingService(
        target_name="win-dev",
        backend="windows_pywinauto",
        source_factory=lambda _scope, _sink: _FakeSource(),
        indicator_factory=indicator_factory,
    )
    service.start(
        name="flow", process_name="EDRClient.exe", window_title="^EDRClient$",
    )
    assert service.assertion()["ok"] is True
    captured["callbacks"].add_assertion({
        "assertion": "visible", "expected": False,
        "automationId": "btnApply", "controlType": "Button", "name": "应用",
    })
    stopped = service.stop()
    assertion = stopped["recording"]["events"][0]
    assert assertion["assertion"]["expected"] is False
    assert assertion["assertion"]["timeoutSeconds"] == 10.0
    assert assertion["observedTarget"]["automationId"] == "btnApply"
    assert indicator.calls == ["start", "assert", ("count", 1), "stop"]


def test_record_assert_cli_opens_target_local_editor(monkeypatch, capsys):
    agent = _FakeAgent()
    monkeypatch.setattr(cli, "_target_agent", lambda *_args: agent)
    assert cli.main(["--target", "win-dev", "record", "assert"]) == 0
    capsys.readouterr()
    assert agent.calls == [("add_recording_assertion", {}, None)]


def test_assertion_shortcut_opens_editor_without_becoming_business_event():
    indicator = _FakeIndicator()
    service = RecordingService(
        target_name="win-dev",
        backend="windows_pywinauto",
        source_factory=lambda _scope, _sink: _FakeSource(),
        indicator_factory=lambda _name, _scope, _callbacks: indicator,
    )
    service.start(name="flow", process_name="EDRClient.exe", window_title="^EDRClient$")
    session = service.manager.current()
    accepted = session.ingest(RawCaptureEvent(
        sequence=1,
        wall_time="2026-08-17T00:00:00Z",
        monotonic_ms=10,
        type="key_command",
        scope=session.scope,
        input={"key": "A", "modifiers": ["CTRL", "SHIFT"]},
    ))
    assert accepted is False
    assert indicator.calls == ["start", "assert"]
    assert session.recording().events == ()
    service.stop()


def test_assertion_contract_validates_expected_type_and_timeout():
    service = RecordingService(
        target_name="win-dev", backend="windows_pywinauto",
        source_factory=lambda _scope, _sink: _FakeSource(),
    )
    service.start(name="flow", process_name="EDRClient.exe", window_title="^EDRClient$")
    session = service.manager.current()

    with pytest.raises(Exception) as exc:
        session.add_assertion({
            "assertion": "checked", "expected": "true",
            "automationId": "backupEnabled", "timeoutSeconds": 10,
        })
    assert getattr(exc.value, "code", None) == "recording_assertion_invalid"
    with pytest.raises(Exception) as exc:
        session.add_assertion({
            "assertion": "visible", "expected": True,
            "automationId": "backupEnabled", "timeoutSeconds": 0,
        })
    assert getattr(exc.value, "code", None) == "recording_assertion_invalid"
    session.add_assertion({
        "assertion": "window_open",
        "expected": {"exists": False, "titleRegex": "^Dialog$"},
        "timeoutSeconds": "15",
    })
    event = session.recording().events[-1]
    assert event.assertion == {
        "type": "window_open",
        "expected": {"exists": False, "titleRegex": "^Dialog$"},
        "timeoutSeconds": 15.0,
    }
    service.stop()


def test_assertion_editor_prefills_last_semantic_target():
    indicator = _FakeIndicator()
    source = _FakeSource()
    source.last_observed_target = ObservedTarget(
        "OBS-1", "T0001", "sha256:button", "Button", "btnApply",
        text="应用", protected=False,
    )
    service = RecordingService(
        target_name="win-dev", backend="windows_pywinauto",
        source_factory=lambda _scope, _sink: source,
        indicator_factory=lambda _name, _scope, _callbacks: indicator,
    )
    service.start(name="flow", process_name="EDRClient.exe", window_title="^EDRClient$")
    service.assertion()
    assert indicator.calls[-1] == ("assert", {
        "automationId": "btnApply", "identifier": None,
        "controlType": "Button", "name": "应用",
    })
    service.stop()


def test_assertion_editor_prefers_current_hover_target_over_last_event():
    indicator = _FakeIndicator()
    source = _FakeSource()
    source.last_observed_target = ObservedTarget(
        "OBS-1", "T0001", "sha256:old", "Button", "oldButton",
        text="旧目标", protected=False,
    )
    source.current_observed_target = lambda: ObservedTarget(
        "OBS-2", "T0002", "sha256:new", "CheckBox", "backupEnabled",
        text="启用备份", protected=False,
    )
    service = RecordingService(
        target_name="win-dev", backend="windows_pywinauto",
        source_factory=lambda _scope, _sink: source,
        indicator_factory=lambda _name, _scope, _callbacks: indicator,
    )
    service.start(name="flow", process_name="EDRClient.exe", window_title="^EDRClient$")

    service.assertion()

    assert indicator.calls[-1] == ("assert", {
        "automationId": "backupEnabled", "identifier": None,
        "controlType": "CheckBox", "name": "启用备份",
    })
    service.stop()


def test_start_reports_which_windows_the_scope_admitted():
    """An empty seed beside a running application explains dropped input."""
    from target.recording.service import RecordingService

    class _Source:
        seeded_scope = ("logo1", "日志中心")

        def start(self): pass
        def pause(self): pass
        def resume(self): pass
        def stop(self): pass

    service = RecordingService(
        target_name="win", backend="windows_pywinauto",
        source_factory=lambda scope, sink: _Source(),
    )

    result = service.start(
        name="flow", process_name="EDRClient.exe", window_title="^logo1$",
    )

    assert result["seededScope"] == ["logo1", "日志中心"]
