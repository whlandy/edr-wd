"""Second independent review: downstream contracts, not UI/remote automation."""
import json
import os
import runpy
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import open_recorder
from target.recording.assertion_draft import build_assertion_draft, preview_assertion
from target.recording.result_store import RecordingResultStore
from target.recording.service import RecordingService
from target.recording.ui_controller import RecorderUIController

REPO = Path(__file__).resolve().parents[2]   # 复制进正式回归后的仓库根
HELPERS = runpy.run_path(str(REPO / "result-report/2026-10-22_23-20-15/recording-usability-review/test_independent_acceptance.py"))


@pytest.mark.parametrize("failure", ["unreachable", "rejected", "exception"])
def test_launcher_failure_returns_nonzero(monkeypatch, failure):
    import fastmcp
    monkeypatch.setattr(open_recorder, "_host_reachable", lambda url: failure != "unreachable")
    class Client:
        def __init__(self, *args, **kwargs):
            pass
        async def __aenter__(self):
            if failure == "exception":
                raise ConnectionError("fixture: connection refused")
            return self
        async def __aexit__(self, *args):
            pass
        async def call_tool(self, *args):
            return SimpleNamespace(content=[SimpleNamespace(text=json.dumps({
                "ok": False, "error": "fixture: no interactive desktop"}))])
    monkeypatch.setattr(fastmcp, "Client", Client)
    assert open_recorder.main([]) != 0


def backend(activate_ok=True):
    # Exact fields returned by WindowsGUI._win_info: no process_name/helper.
    windows = [{"title": "Fixture", "process_id": os.getpid(), "handle": 456,
                "visible": True, "enabled": True, "rectangle": {}}]
    return SimpleNamespace(list_windows=lambda: {"ok": True, "windows": windows},
                           activate_app=lambda **kw: {"ok": activate_ok})


def test_production_window_shape_resolves_real_process_name():
    service = RecordingService(target_name="fixture", backend="windows_pywinauto")
    result = RecorderUIController(service, backend()).list_recordable_windows()
    assert result["windows"][0]["processName"], result


def test_failed_focus_prevents_starting_capture():
    calls = []
    service = SimpleNamespace(start=lambda **kwargs: (calls.append(kwargs) or {"ok": True}))
    actual_backend = backend(activate_ok=False)
    actual_backend.process_name_for_pid = lambda pid: "Fixture.exe"
    controller = RecorderUIController(service, actual_backend)
    selection = controller.list_recordable_windows()["windows"][0]["selectionId"]
    result = controller.start_from_selection(selection, "fixture")
    assert not calls and result["ok"] is False, {"result": result, "starts": calls}


def test_subprocess_emit_is_not_a_successful_save_ack():
    from dataclasses import replace
    _, indicator, _, _ = HELPERS["form"]()
    sent = []
    # Actual _indicator_child_main callback emits JSON then returns None.
    indicator.callbacks = replace(indicator.callbacks,
                                  add_assertion=lambda payload: sent.append(payload))
    form = indicator._assertion_dialog["dialog"]
    indicator._submit_assertion({"requestId": "pending-save"}, form)
    while not indicator._commands.empty():
        command, payload = indicator._commands.get_nowait()
        if command == "assert_ack":
            indicator._apply_assert_ack(payload)
    assert sent and not getattr(form, "destroyed", False), "表单在父进程确认前已关闭"


def test_ack_for_other_request_does_not_close_current_form():
    _, indicator, _, _ = HELPERS["form"]()
    form = indicator._assertion_dialog["dialog"]
    indicator._assertion_dialog["requestId"] = "current-save"
    indicator._apply_assert_ack({"ok": True, "requestId": "other-save", "sequence": 1})
    assert not getattr(form, "destroyed", False)


def test_source_redacted_png_can_be_persisted_and_loaded(tmp_path):
    store = RecordingResultStore(tmp_path)
    session_id, capture_id = "REC-" + "a" * 32, "CAP-" + "b" * 32
    source_redacted_png = b"\x89PNG\r\n\x1a\nfixture-redacted"
    store.save_capture(session_id, capture_id, source_redacted_png)
    assert store.load_captures(session_id)[capture_id] == source_redacted_png


def test_missing_boolean_read_is_not_a_passing_trial():
    draft = build_assertion_draft({"automationId": "checkbox", "value": None},
                                  "checked", False, read_status="ok")
    result = preview_assertion(draft, lambda: ("ok", None))
    assert result["result"] == "undetermined", result


def test_missing_captures_do_not_mark_result_collected(monkeypatch, tmp_path):
    from agent.recording import ui_bridge
    calls = []
    agent = SimpleNamespace(call_tool=lambda name, args, **kwargs: (
        calls.append(name) or {"ok": True, "recording": {}}))
    artifacts = SimpleNamespace(directory=tmp_path, compilation=SimpleNamespace(
        issues=[], golden=SimpleNamespace(status="incomplete")))
    monkeypatch.setattr(ui_bridge, "collect_recording_artifacts", lambda *a, **kw: (
        artifacts, [{"code": "recording_capture_fetch_failed", "captureId": "missing"}]))
    ui_bridge._collect_one(agent, "REC-" + "a" * 32, tmp_path, None, None,
                           lambda line: None, mark=True)
    assert "mark_recording_collected" not in calls, calls
