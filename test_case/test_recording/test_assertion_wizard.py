"""U2 回归：组件拾取与中文类型化断言向导（录制易用性批次）。

对应设计 §3.2/§3.3 与 U2 验收门：默认界面无需 ID/JSON；选组件不触发业务
点击；文本 123/true 保持字符串；false/空值区别 unknown；读取异常不得试验
通过；ACK 后才计数；拾取期间业务事件不落 trace；绑定显式 causalId。
"""

import pytest

from target.recording.assertion_draft import (
    AssertionDraft, available_assertion_types, build_assertion_draft,
    preview_assertion, render_assertion_summary, validate_assertion_draft,
)
from target.recording.models import (
    CaptureScope, RawCaptureEvent, RecordingModelError,
)
from target.recording.picker import PickerController
from target.recording.session import RecordingSession


# ── 类型化草稿：文本/布尔按类型进门 ────────────────────────────────


def test_text_expectation_123_stays_string():
    draft = build_assertion_draft({"automationId": "box"}, "text_equals", "123")
    assert draft.typed_expected == "123" and isinstance(draft.typed_expected, str)
    payload = validate_assertion_draft(draft)
    assert payload["expected"] == "123" and isinstance(payload["expected"], str)


def test_text_true_stays_string_and_bool_type_rejects_non_bool():
    draft = build_assertion_draft({"automationId": "x"}, "text_contains", "true")
    assert draft.typed_expected == "true"
    with pytest.raises(RecordingModelError):
        build_assertion_draft(None, "text_equals", 123)          # 数字不能混进文本
    with pytest.raises(RecordingModelError):
        build_assertion_draft(None, "checked", "yes")            # 布尔必须真布尔
    assert build_assertion_draft(None, "checked", False).typed_expected is False


def test_available_types_require_control_and_respect_capabilities():
    with_control = {"automationId": "btn", "controlType": "Button"}
    types = {t["type"] for t in available_assertion_types(with_control)}
    assert {"text_equals", "value_equals", "visible", "checked", "enabled",
            "window_open"} <= types
    # 无组件身份：只开放窗口类
    free = {t["type"] for t in available_assertion_types(None)}
    assert free == {"window_open", "window_text_contains"}
    # 能力未知 → 布尔开放并带提示；能力缺失 → 隐藏
    hinted = {t["type"]: t for t in available_assertion_types(with_control, read_capabilities=None)}
    assert hinted["checked"]["capabilityNote"]
    caps = {t["type"]: t for t in
            available_assertion_types(with_control, read_capabilities={"checked"})}
    assert "checked" in caps and "enabled" not in caps


def test_summary_is_plain_chinese_and_read_failure_is_disclosed():
    draft = build_assertion_draft(
        {"name": "操作日志", "automationId": "cbLog"}, "checked", True,
        read_status="unknown", timeout_seconds=10,
    )
    summary = render_assertion_summary(draft)
    assert "等待最多 10 秒" in summary and "已勾选" in summary
    assert "未能读取" in summary          # 读取失败必须写进摘要，不冒充观测


def test_preview_unknown_or_raising_reader_never_passes():
    draft = build_assertion_draft({"name": "开关"}, "checked", True)
    assert preview_assertion(draft, lambda: ("unknown", None))["result"] == "undetermined"
    assert preview_assertion(
        draft, lambda: (_ for _ in ()).throw(RuntimeError("UIA hang")))["result"] == "undetermined"
    # false 是合法 actual：期望 True → fail；期望 False → pass
    assert preview_assertion(draft, lambda: ("ok", False))["result"] == "fail"
    off = build_assertion_draft({"name": "开关"}, "checked", False)
    assert preview_assertion(off, lambda: ("ok", False))["result"] == "pass"


def test_preview_text_equality_is_exact_and_empty_string_is_a_value():
    draft = build_assertion_draft({"name": "状态"}, "text_equals", "")
    assert preview_assertion(draft, lambda: ("ok", ""))["result"] == "pass"
    assert preview_assertion(draft, lambda: ("ok", "0"))["result"] == "fail"
    contains = build_assertion_draft({"name": "日志"}, "text_contains", "失败")
    assert preview_assertion(contains, lambda: ("ok", "0 条失败记录"))["result"] == "pass"


# ── 拾取状态机：不触发业务点击、限频、取消恢复 ─────────────────────


class _FakeSource:
    def __init__(self, observed):
        self.observed = observed
        self.picking_on = False
        self.hit_calls = 0

    def begin_pick(self): self.picking_on = True
    def end_pick(self): self.picking_on = False
    def current_observed_target(self):
        self.hit_calls += 1
        return self.observed


class _Clock:
    def __init__(self): self.t = 100.0
    def __call__(self): return self.t
    def advance(self, s): self.t += s


def test_picker_begin_confirm_freezes_draft_and_restores_state():
    clock = _Clock()
    restored = []
    picker = PickerController(
        _FakeSource({"automationId": "cbLog", "name": "操作日志", "value": "true"}),
        previous_state_provider=lambda: "recording",
        restore_state=lambda s: restored.append(s),
        monotonic=clock,
    )
    assert picker.begin()["previousState"] == "recording"
    clock.advance(0.2)
    sample = picker.update_pointer((10, 10))
    assert sample["readStatus"] == "ok"
    draft = picker.confirm()
    assert draft["draft"]["observed"]["automationId"] == "cbLog"
    assert picker.picking is False and restored == ["recording"]


def test_picker_rate_limits_pointer_sampling_to_10hz():
    clock = _Clock()
    source = _FakeSource({"automationId": "x"})
    picker = PickerController(source, lambda: "recording", lambda s: None, monotonic=clock)
    picker.begin()
    first = picker.update_pointer((1, 1))
    second = picker.update_pointer((2, 2))       # 同一时钟瞬间 → 限频跳过
    assert first["sampled"] is True and second["sampled"] is False
    clock.advance(0.2)
    assert picker.update_pointer((3, 3))["sampled"] is True
    assert source.hit_calls == 2


def test_picker_confirm_without_readable_target_is_refused():
    picker = PickerController(
        _FakeSource(None), lambda: "recording", lambda s: None, monotonic=_Clock())
    picker.begin()
    with pytest.raises(RecordingModelError) as exc:
        picker.confirm()
    assert exc.value.code == "recording_pick_target_unresolved"


def test_picker_cancel_restores_previous_state():
    restored = []
    picker = PickerController(
        _FakeSource({"automationId": "x"}), lambda: "paused",
        lambda s: restored.append(s), monotonic=_Clock())
    picker.begin()
    assert picker.cancel()["cancelled"] is True
    assert restored == ["paused"] and picker.picking is False


# ── 会话集成：拾取挂起业务持久化、草稿提交、pending_verifiers ──────


class _NoDriver:
    def start(self, emit): ...
    def stop(self): ...


def test_picking_suspends_business_persistence_in_source():
    """拾取模式（begin_pick→end_pick）期间，correlate 出的业务事件不落 trace。"""
    from target.recording.source import HookPacket, QueuedCaptureSource
    ingested: list[RawCaptureEvent] = []
    scope = CaptureScope("win", "windows_pywinauto", "EDRClient.exe", "^EDR$")

    class _Correlator:
        out_of_scope_events = 0

        def correlate(self, packet, scope, next_sequence):
            return RawCaptureEvent(
                sequence=next_sequence, wall_time="2026-10-22T00:00:00.000Z",
                monotonic_ms=int(packet.monotonic_ms), type="pointer_click",
                scope=scope, input={"button": packet.button},
                causal_id="CAUSE-pick",
            )

        def current_target(self, scope):
            return None

    source = QueuedCaptureSource(scope=scope, sink=ingested.append,
                                 driver=_NoDriver(), correlator=_Correlator())
    packet = HookPacket(kind="pointer_click", monotonic_ms=5,
                        wall_time="2026-10-22T00:00:00.000Z", button="left")
    # 拾取中：事件被 correlate 但不持久化
    source.begin_pick()
    source._enqueue(packet)
    source._enqueue(source._STOP)
    source._run()
    assert ingested == [] and source.picking_dropped_events == 1
    # 拾取结束：同样的包正常落 trace
    source.begin_pick(); source.end_pick()
    ingested.clear()
    source2 = QueuedCaptureSource(scope=scope, sink=ingested.append,
                                  driver=_NoDriver(), correlator=_Correlator())
    source2._enqueue(packet)
    source2._enqueue(source2._STOP)
    source2._run()
    assert len(ingested) == 1 and source2.picking_dropped_events == 0


def _session_with_pick_source(session_id, observed):
    class _PickSource:
        def start(self): ...
        def stop(self): ...
        def pause(self): ...
        def resume(self): ...
        def status(self): return {}
        def heartbeat(self): return {}
        def begin_pick(self): ...
        def end_pick(self): ...
        def current_observed_target(self):
            from target.recording.models import ObservedTarget
            return ObservedTarget(
                snapshot_id="OBS-pick", target_id="T1",
                fingerprint="sha256:" + "0" * 64,
                automation_id=observed.get("automationId"),
                control_type=observed.get("controlType"),
                text=observed.get("name"), value=observed.get("value"),
            )
    session = RecordingSession(session_id, "flow",
                               CaptureScope("win", "windows_pywinauto", "EDRClient.exe", "^EDR$"))
    session.attach_source(_PickSource())
    session.start()
    return session


def test_freeze_then_commit_uses_draft_id_and_dedups():
    session = _session_with_pick_source(
        "REC-" + "3" * 32, {"automationId": "cbLog", "name": "操作日志", "value": "true"})
    session.begin_pick()
    session.update_pick_pointer((10, 10))
    frozen = session.freeze_pick()
    draft_id = frozen["draft"]["draftId"]
    first = session.commit_assertion_draft(frozen["draft"])
    again = session.commit_assertion_draft(frozen["draft"])   # 同 draftId 重试
    assert first["sequence"] == again["sequence"] and again.get("duplicate") is True
    assert first["requestId"] == draft_id
    events = [e for e in session.recording().events if e.type == "assertion"]
    assert len(events) == 1


def test_commit_draft_from_other_session_is_refused():
    session = RecordingSession("REC-" + "4" * 32, "flow",
                               CaptureScope("win", "windows_pywinauto", "EDRClient.exe", "^EDR$"))
    session.start()
    with pytest.raises(RecordingModelError) as exc:
        session.commit_assertion_draft({"draftId": "foreign", "assertionType": "checked",
                                        "typedExpected": True})
    assert exc.value.code == "recording_draft_unknown"


def test_pending_verifiers_lists_unbound_scroll_and_drag():
    session = RecordingSession("REC-" + "5" * 32, "flow",
                               CaptureScope("win", "windows_pywinauto", "EDRClient.exe", "^EDR$"))
    session.start()
    for seq, (etype, causal) in enumerate(
            [("scroll_commit", "C1"), ("drag_commit", "C2"), ("pointer_click", "C3")], 1):
        event = RawCaptureEvent(
            sequence=seq, wall_time="2026-10-22T00:00:00.000Z", monotonic_ms=seq,
            type=etype, scope=session.scope, input={}, causal_id=causal,
        )
        assert session.ingest(event) is True
    pending = session.pending_verifiers()
    assert [(p["sequence"], p["type"]) for p in pending] == [(1, "scroll_commit"), (2, "drag_commit")]
    # 显式绑定 C1 后 pending 只剩 C2（绑定按 causalId，不按 last）
    session.add_assertion({"assertion": "value_equals", "expected": "3",
                           "automationId": "rows", "requestId": "bind-1",
                           "bindCausalId": "C1"})
    remaining = session.pending_verifiers()
    assert [p["causalId"] for p in remaining] == ["C2"]


def test_binding_to_unknown_causal_id_is_refused():
    session = RecordingSession("REC-" + "6" * 32, "flow",
                               CaptureScope("win", "windows_pywinauto", "EDRClient.exe", "^EDR$"))
    session.start()
    with pytest.raises(RecordingModelError) as exc:
        session.add_assertion({"assertion": "value_equals", "expected": "3",
                               "automationId": "rows", "requestId": "bind-x",
                               "bindCausalId": "CAUSE-ghost"})
    assert exc.value.code == "recording_assertion_unbound"
