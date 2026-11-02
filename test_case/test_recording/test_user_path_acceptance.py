"""Independent acceptance counterexamples; no business UI or remote writes.

Assertions describe required behavior, so an unresolved defect fails visibly.
Fake widgets model Tk callbacks without opening or controlling any desktop.
"""
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import open_recorder
from target.recording.assertion_draft import validate_assertion_draft
from target.recording.indicator import (
    IndicatorCallbacks, NullRecordingIndicator, TkRecordingIndicator,
)
from target.recording.models import CaptureScope, ObservedTarget
from target.recording.recorder_panel import RecorderPanel
from target.recording.result_store import RecordingResultStore
from target.recording.service import RecordingService
from target.recording.session import RecordingSession
from target.recording.ui_controller import RecorderPanelHost, RecorderUIController


class Variable:
    def __init__(self, value=None):
        self.value = value
    def get(self):
        return self.value
    def set(self, value):
        self.value = value
    def trace_add(self, mode, callback):
        self.trace_callback = callback


class Widget:
    def __init__(self, parent=None, **kwargs):
        self.kwargs = kwargs
        self.visible = False
        self.handlers = {}
        self.value = ""
    def grid(self, **kwargs):
        self.visible = True
    def grid_remove(self):
        self.visible = False
    def configure(self, **kwargs):
        self.kwargs.update(kwargs)
    def bind(self, event, fn):
        self.handlers[event] = fn
    def current(self, index):
        self.kwargs["textvariable"].set(self.kwargs["values"][index])
    def insert(self, index, value):
        self.value = value
    def get(self):
        return self.value
    def title(self, *args):
        pass
    def attributes(self, *args):
        pass
    def destroy(self):
        self.destroyed = True


def new_session():
    session = RecordingSession("REC-" + "a" * 32, "review", CaptureScope(
        "fixture", "windows_pywinauto", "Fixture.exe", "^Fixture$"))
    session.start()
    return session


def target():
    return ObservedTarget("OBS-review", "T1", "sha256:review",
                          automation_id="field", control_type="Edit",
                          text="label", value="123", protected=False)


def form():
    session = new_session()
    draft = session._freeze_editor_draft(target())
    sent, widgets = [], []
    def make(*args, **kwargs):
        widget = Widget(*args, **kwargs)
        widgets.append(widget)
        return widget
    indicator = TkRecordingIndicator("review", session.scope, IndicatorCallbacks(
        pause=lambda: None, resume=lambda: None, stop=lambda: None,
        add_assertion=lambda payload: (sent.append(payload) or {"ok": True})))
    indicator._root = Widget()
    indicator._tk = SimpleNamespace(Toplevel=make, StringVar=Variable, BooleanVar=Variable)
    indicator._ttk = SimpleNamespace(
        Label=make, Combobox=make, Entry=make, Button=make, Checkbutton=make)
    indicator._messagebox = SimpleNamespace(
        showerror=lambda *a, **k: None, showinfo=lambda *a, **k: None,
        askyesno=lambda *a, **k: True)
    indicator._assertion_target = draft
    indicator._async = lambda fn, *args: fn(*args)
    indicator._show_assertion_editor()
    return session, indicator, widgets, sent


def test_launcher_connects_using_installed_fastmcp_api(monkeypatch, capsys):
    # R2 裁定：reachable=True 且调用失败时返回 0 是假绿——成功必须有真实
    # 打开回执。旧断言（真实连接失败也 0）已被第二次独立验收否决；此处
    # 保留成功语义：注入一个返回 ok=True 的 Client stub，断言 0。
    import json as _json
    from types import SimpleNamespace as _NS
    import fastmcp
    monkeypatch.setattr(open_recorder, "_host_reachable", lambda url: True)

    class _Client:
        def __init__(self, *args, **kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            pass
        async def call_tool(self, *a):
            return _NS(content=[_NS(text=_json.dumps({"ok": True}))])

    monkeypatch.setattr(fastmcp, "Client", _Client)
    assert open_recorder.main([]) == 0


def test_refresh_ack_populates_panel_window_choices():
    backend = SimpleNamespace(list_windows=lambda: {"ok": True, "windows": [
        {"title": "Fixture", "process_id": 123, "handle": 456, "visible": True}]})
    service = RecordingService(target_name="fixture", backend="windows_pywinauto")
    host = RecorderPanelHost(RecorderUIController(service, backend))
    result = host.complete_ui_request("refresh", "list_windows", {})
    panel = RecorderPanel("fixture", None, lambda *a: None)
    panel._window_var, panel._window_box = Variable(), Widget()
    panel._status_label = Widget()
    # This is the actual parent -> child message shape from _consume().
    panel.render({"ack": "refresh", "result": result})
    assert len(panel._windows) == 1, {"result": result, "panelChoices": panel._windows}


def test_direct_window_push_configures_combobox_values():
    panel = RecorderPanel("fixture", None, lambda *a: None)
    panel._window_var, panel._window_box = Variable(), Widget()
    panel.render({"windows": [{"selectionId": "one", "label": "one"},
                              {"selectionId": "two", "label": "two"}]})
    assert panel._window_box.kwargs.get("values") == ["one", "two"]


def test_native_backend_contract_yields_real_process_name():
    backend = SimpleNamespace(list_windows=lambda: {"ok": True, "windows": [
        {"title": "Fixture", "process_id": 123, "process_name": "Fixture.exe",
         "handle": 456, "visible": True}]})
    service = RecordingService(target_name="fixture", backend="windows_pywinauto")
    listed = RecorderUIController(service, backend).list_recordable_windows()
    assert listed["windows"][0]["processName"] == "Fixture.exe", listed


def test_focus_failure_is_not_reported_as_success():
    backend = SimpleNamespace(
        list_windows=lambda: {"ok": True, "windows": [{
            "title": "Fixture", "process_id": 123, "handle": 456, "visible": True}]},
        process_name_for_pid=lambda pid: "Fixture.exe",
        activate_app=lambda **kwargs: {"ok": False, "code": "registered_app_invalid"})
    service = RecordingService(target_name="fixture", backend="windows_pywinauto")
    controller = RecorderUIController(service, backend)
    selected = controller.list_recordable_windows()["windows"][0]["selectionId"]
    result = controller.focus_selected_window(selected)
    assert result["ok"] is False, result


def test_boolean_choice_displays_boolean_editor():
    _, _, widgets, _ = form()
    combo = next(w for w in widgets if "组件文字等于 (text_equals)" in w.kwargs.get("values", []))
    boolean = next(w for w in widgets if w.kwargs.get("values") == ["是", "否"])
    combo.kwargs["textvariable"].set("组件已勾选 (checked)")
    combo.handlers["<<ComboboxSelected>>"]()
    assert boolean.visible, "选择 checked 后仍显示文本框，布尔控件未显示"


def test_wizard_save_has_callable_submission_path():
    _, _, widgets, sent = form()
    button = next(w for w in widgets if w.kwargs.get("text") == "保存")
    button.kwargs["command"]()
    assert sent


def test_visible_binding_checkbox_affects_saved_payload():
    _, indicator, widgets, sent = form()
    # Isolate the downstream checkbox defect after the missing method above;
    # this stub captures payload only and does not replace the real form.
    indicator._submit_assertion = lambda payload, dialog: sent.append(payload)
    checkbox = next(w for w in widgets if w.kwargs.get("text") == "作为上一个动作的结果验证")
    checkbox.kwargs["variable"].set(True)
    button = next(w for w in widgets if w.kwargs.get("text") == "保存")
    button.kwargs["command"]()
    assert sent and sent[0]["bindPrevious"] is True, sent


def test_invalid_draft_can_be_corrected_and_resubmitted():
    session = new_session()
    draft = session._freeze_editor_draft(target())
    with pytest.raises(Exception):
        session.commit_assertion_draft({**draft, "assertionType": "checked",
                                        "typedExpected": "invalid boolean"})
    result = session.commit_assertion_draft(draft)
    assert result["ok"] is True


def test_indicator_stop_persists_result_without_cli_stop(tmp_path):
    captured = []
    def factory(name, scope, callbacks):
        captured.append(callbacks)
        return NullRecordingIndicator()
    store = RecordingResultStore(tmp_path)
    from target.recording.session import NullCaptureSource
    service = RecordingService(
        target_name="fixture", backend="windows_pywinauto", result_store=store,
        source_factory=lambda scope, sink: NullCaptureSource(), indicator_factory=factory)
    started = service.start(name="review", process_name="Fixture.exe", window_title="^Fixture$")
    captured[0].stop()  # The real indicator callback, without CLI or panel stop.
    assert started["sessionId"] in store.list_pending(), store.list_pending()


def test_stop_wins_against_pending_assertion_append():
    session = new_session()
    reached, release = threading.Event(), threading.Event()
    original = session._append_assertion_event
    responses = []
    def gated_append(*args, **kwargs):
        reached.set()
        assert release.wait(3)
        return original(*args, **kwargs)
    session._append_assertion_event = gated_append
    def save():
        try:
            responses.append(session.add_assertion({
                "assertion": "text_equals", "expected": "123", "automationId": "field",
                "requestId": "racy-save"}))
        except Exception as exc:
            responses.append({"ok": False, "error": str(exc)})
    thread = threading.Thread(target=save)
    thread.start()
    assert reached.wait(3)
    _, stopped_document = session.stop()
    release.set()
    thread.join(3)
    assert responses and responses[0]["ok"] is False, {
        "response": responses, "stoppedEvents": len(stopped_document.events),
        "eventsAfterStop": len(session.recording().events)}
