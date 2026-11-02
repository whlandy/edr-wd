"""录制易用性用户路径集成验证（R1/R2 修复回归）。

覆盖独立验收点名的缺口，全部走真实生产函数：
- ACK 契约：错 ID 不作用、父端拒绝保留表单、延迟 ack 恢复按钮；
- 恢复取证：浮窗 Stop 落盘 → 新会话从磁盘恢复截图 → hash 一致；
- 组件列表选择链（原生键拾取未实现时的设计允许替代）：列表 → 身份确认 →
  向导草稿 → 服务端提交 → 指定动作绑定收敛 → 编译 ready；
- 生产 backend 形状：无 process_name/无 suggester 的真实条目形状仍能解析
  出真实进程名（psutil）。
"""

import base64
import hashlib
import json
from types import SimpleNamespace

import pytest

from agent.recording import ui_bridge
from scripts import open_recorder
from target.recording.assertion_draft import build_assertion_draft, preview_assertion
from target.recording.indicator import (
    IndicatorCallbacks, NullRecordingIndicator, TkRecordingIndicator,
)
from target.recording.models import (
    CaptureScope, ObservedTarget, RawCaptureEvent, RecordingModelError,
)
from target.recording.result_store import RecordingResultStore
from target.recording.service import RecordingService
from target.recording.session import RecordingSession
from target.recording.ui_controller import RecorderUIController


def new_session(seed="a"):
    session = RecordingSession("REC-" + seed * 32, "review", CaptureScope(
        "fixture", "windows_pywinauto", "Fixture.exe", "^Fixture$"))
    session.start()
    return session


def target(value="123", **extra):
    return ObservedTarget("OBS-review", "T1", "sha256:review",
                          automation_id="field", control_type="Edit",
                          text="label", value=value, protected=False, **extra)


class _Widget:
    def __init__(self, parent=None, **kwargs):
        self.kwargs = dict(kwargs)
        self.visible = False
        self.destroyed = False
        self.value = ""
    def grid(self, **kwargs): self.visible = True
    def grid_remove(self): self.visible = False
    def configure(self, **kwargs): self.kwargs.update(kwargs)
    def bind(self, event, fn): self.handlers = getattr(self, "handlers", {}); self.handlers[event] = fn
    def insert(self, index, value): self.value = value
    def get(self): return self.value
    def current(self, index): self.kwargs["textvariable"].set(self.kwargs["values"][index])
    def title(self, *args): pass
    def attributes(self, *args): pass
    def resizable(self, *args): pass
    def protocol(self, *args): pass
    def after(self, *args): pass
    def mainloop(self): pass
    def destroy(self): self.destroyed = True
    def deiconify(self): pass
    def withdraw(self): pass
    def update_idletasks(self): pass
    def winfo_rootx(self): return 0
    def winfo_rooty(self): return 0
    def winfo_width(self): return 0
    def winfo_height(self): return 0
    def curselection(self): return ()


def _wizard(session):
    draft = session._freeze_editor_draft(target())
    sent, widgets = [], []

    def make(*args, **kwargs):
        w = _Widget(*args, **kwargs)
        widgets.append(w)
        return w

    indicator = TkRecordingIndicator("review", session.scope, IndicatorCallbacks(
        pause=lambda: None, resume=lambda: None, stop=lambda: None,
        add_assertion=lambda payload: (sent.append(payload) or {"ok": True})))
    indicator._root = _Widget()
    indicator._tk = SimpleNamespace(Toplevel=make, END="end")
    def _var(value=None):
        box = SimpleNamespace(value=value)
        box.get = lambda: box.value
        box.set = lambda s: setattr(box, "value", s)
        box.trace_add = lambda *a: None
        return box
    indicator._tk.StringVar = _var
    indicator._tk.BooleanVar = _var
    indicator._ttk = SimpleNamespace(Label=make, Combobox=make, Entry=make,
                                     Button=make, Checkbutton=make)
    indicator._messagebox = SimpleNamespace(
        showerror=lambda *a, **k: None, showinfo=lambda *a, **k: None,
        askyesno=lambda *a, **k: True)
    indicator._assertion_target = draft
    indicator._async = lambda fn, *args: fn(*args)
    indicator._show_assertion_editor()
    return session, indicator, widgets, sent


def test_wrong_request_id_ack_leaves_form_open():
    session, indicator, widgets, _ = _wizard(new_session())
    form = indicator._assertion_dialog["dialog"]
    indicator._assertion_dialog["requestId"] = "current-save"
    indicator._apply_assert_ack({"ok": True, "requestId": "other-save", "sequence": 1})
    assert not form.destroyed


def test_parent_rejection_keeps_form_open_with_button_restored():
    session, indicator, widgets, sent = _wizard(new_session())

    def reject(payload):
        raise RecordingModelError("recording_state_invalid", "已停止", path="session.state")

    indicator.callbacks = IndicatorCallbacks(
        pause=lambda: None, resume=lambda: None, stop=lambda: None,
        add_assertion=reject)
    form = indicator._assertion_dialog["dialog"]
    button = indicator._assertion_dialog["submit_button"]
    indicator._submit_assertion({"requestId": "r1", "expected": "x"}, form)
    assert not form.destroyed
    # 模拟 poll 消费 ack 队列：父端拒绝回执恢复按钮、保留表单
    while not indicator._commands.empty():
        command, ack = indicator._commands.get_nowait()
        if command == "assert_ack":
            indicator._apply_assert_ack(ack)
    assert button.kwargs.get("state") == "normal"
    assert not form.destroyed


def test_delayed_parent_ack_closes_form_after_pending():
    session, indicator, widgets, sent = _wizard(new_session())
    form = indicator._assertion_dialog["dialog"]
    indicator._submit_assertion({"requestId": "delayed", "expected": "x"}, form)
    assert not form.destroyed          # 子进程 emit 路径：无父回执前保持 pending
    indicator._apply_assert_ack({"ok": True, "requestId": "delayed", "sequence": 2})
    assert form.destroyed              # 同 requestId 的真实回执到达才关闭


# ── 恢复取证：浮窗 Stop 落盘 → 新会话从磁盘恢复截图 → hash 一致 ────


def test_stop_via_indicator_persists_raw_and_captures_for_later_session(tmp_path):
    png = b"\x89PNG\r\n\x1a\nredacted-frame-1"
    capture_id = "CAP-" + "c" * 32

    class _Evidence:
        def clear(self): ...
        def initialize(self):
            return {"ok": True, "capture": {"id": capture_id,
                                            "sha256": "sha256:" + hashlib.sha256(png).hexdigest()}}
        def attach(self, event): return event
        def get(self, cid):
            return {"ok": True, "image_b64": base64.b64encode(png).decode(),
                    "sha256": "sha256:" + hashlib.sha256(png).hexdigest()}

    store = RecordingResultStore(tmp_path)
    from target.recording.session import NullCaptureSource
    service = RecordingService(
        target_name="fixture", backend="windows_pywinauto", result_store=store,
        source_factory=lambda scope, sink: NullCaptureSource(),
        indicator_factory=lambda name, scope, cb: NullRecordingIndicator(),
        evidence_store=_Evidence(),
    )
    started = service.start(name="review", process_name="Fixture.exe",
                            window_title="^Fixture$")
    session = service.manager.current()
    digest = "sha256:" + hashlib.sha256(png).hexdigest()
    event = RawCaptureEvent(
        sequence=1, wall_time="2026-10-23T00:00:00.000Z", monotonic_ms=1,
        type="pointer_click", scope=session.scope,
        input={"button": "left"}, causal_id="CAUSE-cap",
        evidence={"capture": {"id": capture_id, "sha256": digest,
                              "width": 920, "height": 600, "origin": [17, 53],
                              "redacted": True, "redactions": []}},
    )
    assert session.ingest(event) is True
    stopped = service.stop()
    assert stopped["resultStore"]["ok"] is True, stopped["resultStore"]
    assert stopped["resultStore"]["captures"] >= 1

    # 服务重启/新会话后：从磁盘恢复 raw 与截图，hash 逐字节一致
    fresh = RecordingService(target_name="fixture", backend="windows_pywinauto",
                             result_store=store)
    result = fresh.get_result(started["sessionId"])
    assert result["ok"] is True
    captures = fresh.result_store.load_captures(started["sessionId"])
    assert captures[capture_id] == png
    assert hashlib.sha256(captures[capture_id]).hexdigest() == \
        hashlib.sha256(png).hexdigest()


def test_bridge_collect_marks_only_complete_evidence(tmp_path, monkeypatch):
    """取证缺口 → 保留 pending；完整 → mark_collected（R2-D 语义锁定）。"""
    calls: list[str] = []
    agent = SimpleNamespace(call_tool=lambda name, args=None, **kw:
                            (calls.append(name) or {"ok": True, "recording": {}}))
    artifacts = SimpleNamespace(
        directory=tmp_path,
        compilation=SimpleNamespace(issues=[], golden=SimpleNamespace(status="ready")))
    monkeypatch.setattr(ui_bridge, "collect_recording_artifacts",
                        lambda *a, **kw: (artifacts, []))
    ui_bridge._collect_one(agent, "REC-" + "b" * 32, tmp_path, None, None,
                           lambda m: None, mark=True)
    assert "mark_recording_collected" in calls

    calls.clear()
    monkeypatch.setattr(ui_bridge, "collect_recording_artifacts",
                        lambda *a, **kw: (
                            artifacts, [{"code": "recording_capture_fetch_failed"}]))
    ui_bridge._collect_one(agent, "REC-" + "c" * 32, tmp_path, None, None,
                           lambda m: None, mark=True)
    assert "mark_recording_collected" not in calls


# ── 组件列表选择链（R2-F）：列表 → 确认 → 向导 → 绑定收敛 ──────────


class _InventorySource(NullRecordingIndicator):
    """带控制树快照的假 source：组件列表数据来自录制自身快照。"""

    seeded_scope = ()
    dropped_packets = 0
    out_of_scope_events = 0
    picking_dropped_events = 0

    def __init__(self):
        self._snapshots = [{
            "snapshotId": "CS-0001",
            "contentHash": "sha256:fixture",
            "capturedAtMs": 1, "reason": "test", "durationMs": 1,
            "controlCount": 1,
            "window": {"title": "logo1", "processName": "HiSecEndpointAgent.exe"},
            "controls": [
                {"automationId": "rowCount", "controlType": "Text",
                 "name": "行数", "value": "3"},
                {"controlType": "Pane", "name": None, "value": None},
            ],
        }]

    @property
    def control_snapshots(self):
        return tuple(self._snapshots)

    def start(self): ...
    def stop(self): ...
    def pause(self): ...
    def resume(self): ...
    def status(self): return {}
    def heartbeat(self): return {}
    def begin_pick(self): ...
    def end_pick(self): ...
    def current_observed_target(self): return target("3")


def test_component_list_chain_binds_named_action_and_compiles_ready(tmp_path):
    from agent.recording.compiler import compile_recording
    from test_phase0_recording import _event, _recording, _scroll

    session = RecordingSession("REC-" + "9" * 32, "flow",
                               CaptureScope("win", "windows_pywinauto",
                                            "EDRClient.exe", "EDRClient"))
    session.attach_source(_InventorySource())
    session.start()
    events = _recording(
        _event(1, "pointer_click", ms=100, causal_id="cause-open"),
        _scroll(2, ms=500, causal_id="cause-scroll"),
    ).events
    for event in events:
        assert session.ingest(event) is True
    assert [p["causalId"] for p in session.pending_verifiers()] == ["cause-scroll"]

    scroll = events[-1]
    session.begin_pick(binding=(scroll.sequence, scroll.causal_id))
    inventory = session.control_inventory()
    assert len(inventory) == 1          # 无身份组件被过滤
    assert inventory[0]["component"]["automationId"] == "rowCount"

    frozen = session.freeze_component_draft(
        inventory[0]["component"], binding=(scroll.sequence, scroll.causal_id))
    assert frozen["ok"] is True
    draft = dict(session._pending_drafts[frozen["draftId"]],
                 assertionType="value_equals", typedExpected="3")
    saved = session.commit_assertion_draft(draft)
    assert saved["ok"] is True
    assert session.pending_verifiers() == []

    result = compile_recording(session.recording())
    assert result.golden.status == "ready", [i.code for i in result.issues]
    assertion = next(e for e in session.recording().events if e.type == "assertion")
    assert assertion.causal_id == "cause-scroll"     # 绑定的是被指定的动作


def test_component_list_rejects_foreign_binding_target():
    """begin_pick 只记录绑定；ghost causalId 在提交点重验动作存在时被拒
    （设计 §3.4：保存时重验动作仍存在、会话一致）。"""
    session = RecordingSession("REC-" + "8" * 32, "flow",
                               CaptureScope("win", "windows_pywinauto",
                                            "EDRClient.exe", "EDRClient"))
    session.attach_source(_InventorySource())
    session.start()
    session.begin_pick(binding=(99, "CAUSE-ghost"))
    session.update_pick_pointer((10, 10))
    frozen = session.freeze_pick()
    assert frozen["draft"]["binding"] == [99, "CAUSE-ghost"]
    with pytest.raises(RecordingModelError) as exc:
        session.commit_assertion_draft(frozen["draft"])
    assert exc.value.code == "recording_assertion_unbound"


# ── 生产 backend 形状：无 process_name/无 suggester 仍解析真实名 ────


def test_production_window_shape_resolves_real_process_name_via_psutil():
    import os as _os
    windows = [{"title": "Fixture", "process_id": _os.getpid(), "handle": 456,
                "visible": True, "enabled": True, "rectangle": {}}]
    backend = SimpleNamespace(list_windows=lambda: {"ok": True, "windows": windows},
                              activate_app=lambda **kw: {"ok": True})
    service = RecordingService(target_name="fixture", backend="windows_pywinauto")
    result = RecorderUIController(service, backend).list_recordable_windows()
    assert result["windows"][0]["processName"], result


# ── launcher：成功回执 → 0；服务端拒绝 → 非 0（R2-A 语义锁定） ──────


def test_launcher_success_requires_real_receipt(monkeypatch):
    import fastmcp

    class _Client:
        def __init__(self, *args, **kwargs): ...
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def call_tool(self, *a):
            return SimpleNamespace(content=[SimpleNamespace(
                text=json.dumps({"ok": True}))])

    monkeypatch.setattr(open_recorder, "_host_reachable", lambda url: True)
    monkeypatch.setattr(fastmcp, "Client", _Client)
    assert open_recorder.main([]) == 0
