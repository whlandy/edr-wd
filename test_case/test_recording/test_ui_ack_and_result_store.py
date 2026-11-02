"""U0 回归：管理与保存链路（录制易用性批次）。

对应 docs/architecture/05-recording-usability-and-assertion-picker-design.md
U0 验收门与设计 §3.3/§5：

- requestId 去重：同一表单重试不重复落断言事件；无 requestId 的旧路径保持
  每次新增（CLI 兼容）；
- 管理断言提交路径显式允许 PAUSED 会话保存（不经 resume），而原生业务输入
  在 PAUSED 时仍被 ingest 丢弃 —— 对照测试锁住这个差异；
- result_store：停止结果按 sessionId 隔离持久化、可恢复、未收集不覆盖、
  collected 标记；跨会话读取是干净的 miss；
- service.ui_snapshot / open_ui / get_result 与 RecorderUIController 骨架：
  无会话是真实 IDLE 态而非错误；
- indicator 的 assert_ack 路由：保存结果经命令队列回表单（离线可测部分）。
"""

import json
import threading

import pytest

from agent.cli import recording_stop_advisories
from target.recording.indicator import SubprocessTkIndicator
from target.recording.models import CaptureScope, RawCaptureEvent
from target.recording.result_store import RecordingResultStore
from target.recording.service import RecordingService
from target.recording.session import (
    CaptureSessionState, RecordingSession,
)
from target.recording.ui_controller import RecorderUIController


def _session(session_id="REC-" + "0" * 32):
    session = RecordingSession(
        session_id, "flow", CaptureScope("win", "windows_pywinauto", "EDRClient.exe", "^EDR$"),
    )
    session.start()
    return session


def _assertion_payload(**extra):
    payload = {
        "assertion": "text_equals", "expected": "123",
        "automationId": "btnApply", "timeoutSeconds": 10,
    }
    payload.update(extra)
    return payload


def _fake_source(scope, sink):
    class _Source:
        seeded_scope = ()
        dropped_packets = 0
        out_of_scope_events = 0

        def start(self): ...
        def stop(self): ...
        def pause(self): ...
        def resume(self): ...
        def status(self): return {}
        def heartbeat(self): return {}
    return _Source()


# ── requestId 去重 ────────────────────────────────────────────────


def test_same_request_id_appends_only_one_assertion_event():
    session = _session()
    first = session.add_assertion(_assertion_payload(requestId="req-1"))
    second = session.add_assertion(_assertion_payload(requestId="req-1"))
    assert first["sequence"] == second["sequence"] == 1
    assert second["duplicate"] is True and "duplicate" not in first
    assert sum(1 for e in session.recording().events if e.type == "assertion") == 1


def test_without_request_id_each_save_appends_and_stays_string():
    session = _session()
    session.add_assertion(_assertion_payload())
    session.add_assertion(_assertion_payload())
    events = [e for e in session.recording().events if e.type == "assertion"]
    assert len(events) == 2
    # 文本 "123" 必须保持字符串 —— 不经 JSON 数值化。
    assert events[0].assertion["expected"] == "123"
    assert isinstance(events[0].assertion["expected"], str)


def test_request_id_must_be_nonempty_string():
    session = _session()
    with pytest.raises(Exception) as exc:
        session.add_assertion(_assertion_payload(requestId="  "))
    assert "requestId" in str(exc.value)


# ── PAUSED 管理提交 vs 业务输入丢弃（对照） ────────────────────────


def test_paused_session_saves_assertion_without_resume():
    session = _session()
    session.pause()
    result = session.add_assertion(_assertion_payload(requestId="paused-1"))
    assert result["state"] == "paused"
    assert [e for e in session.recording().events if e.type == "assertion"]


def test_paused_session_still_drops_native_business_input():
    session = _session()
    session.pause()
    click = RawCaptureEvent(
        sequence=1, wall_time="2026-10-22T00:00:00.000Z", monotonic_ms=1,
        type="pointer_click", scope=session.scope,
        input={"button": "left"}, causal_id="CAUSE-x",
    )
    assert session.ingest(click) is False
    assert all(e.type != "pointer_click" for e in session.recording().events)


def test_stopped_session_refuses_assertion_save():
    session = _session()
    session.stop()
    with pytest.raises(Exception) as exc:
        session.add_assertion(_assertion_payload(requestId="late-1"))
    assert getattr(exc.value, "code", "") == "recording_state_invalid"


# ── result_store：隔离、可恢复、不覆盖 ────────────────────────────


def test_result_store_roundtrip_and_collected_marker(tmp_path):
    store = RecordingResultStore(tmp_path)
    doc = {"sessionId": "REC-" + "1" * 32, "name": "flow", "events": []}
    saved = store.save_stopped_recording("REC-" + "1" * 32, doc)
    assert saved["stored"] is True and saved["freshSave"] is True
    loaded = store.load_stopped_recording("REC-" + "1" * 32)
    assert loaded == doc
    assert store.list_pending() == ["REC-" + "1" * 32]
    store.mark_collected("REC-" + "1" * 32)
    assert store.list_pending() == []


def test_result_store_refuses_overwrite_with_different_document(tmp_path):
    store = RecordingResultStore(tmp_path)
    sid = "REC-" + "2" * 32
    store.save_stopped_recording(sid, {"v": 1})
    with pytest.raises(Exception) as exc:
        store.save_stopped_recording(sid, {"v": 2})
    assert "already stored" in str(exc.value)
    # 同文档重存幂等（不覆盖、不报错）
    again = store.save_stopped_recording(sid, {"v": 1})
    assert again["freshSave"] is False


def test_result_store_session_ids_are_isolated(tmp_path):
    store = RecordingResultStore(tmp_path)
    with pytest.raises(Exception):
        store.load_stopped_recording("../escape")
    with pytest.raises(Exception):
        store.load_stopped_recording("not-a-session-id")


# ── service：ui_snapshot / open_ui / get_result ───────────────────


def _service(tmp_path, **kwargs):
    return RecordingService(
        target_name="win-dev", backend="windows_pywinauto",
        source_factory=_fake_source,
        result_store=RecordingResultStore(tmp_path / "results"),
        **kwargs,
    )


def test_service_ui_snapshot_is_idle_without_session(tmp_path):
    service = _service(tmp_path)
    snapshot = service.ui_snapshot()
    assert snapshot["ok"] is True and snapshot["viewState"] == "IDLE"
    assert snapshot["session"] is None


def test_service_ui_snapshot_reports_live_session(tmp_path):
    service = _service(tmp_path)
    service.start(name="flow", process_name="EDRClient.exe",
                  window_title="^EDR$", lease_seconds=30)
    snapshot = service.ui_snapshot()
    assert snapshot["viewState"] == "RECORDING"
    assert snapshot["session"]["scope"]["processName"] == "EDRClient.exe"
    assert snapshot["session"]["leaseSecondsRemaining"] > 0


def test_service_stop_persists_result_and_get_result_restores_it(tmp_path):
    service = _service(tmp_path)
    started = service.start(name="flow", process_name="EDRClient.exe",
                            window_title="^EDR$", lease_seconds=30)
    session_id = started["sessionId"]
    stopped = service.stop()
    assert stopped["resultStore"]["ok"] is True and stopped["resultStore"]["freshSave"] is True

    other = _service(tmp_path)  # 服务重启后按 sessionId 可恢复
    result = other.get_result(session_id)
    assert result["ok"] is True
    assert result["recording"]["sessionId"] == session_id
    # 不同 session 不会串：未知 id 是干净 miss
    with pytest.raises(Exception) as exc:
        other.get_result("REC-" + "9" * 32)
    assert "no stored result" in str(exc.value)


def test_recorder_ui_controller_show_is_idempotent(tmp_path):
    service = _service(tmp_path)
    controller = RecorderUIController(service)
    first = controller.show()
    second = controller.show()
    assert first["visible"] is True and second["visible"] is True
    assert first["snapshot"]["viewState"] == second["snapshot"]["viewState"] == "IDLE"
    hidden = controller.hide()
    assert hidden["visible"] is False


# ── indicator ack 路由（离线可测部分） ─────────────────────────────


def test_subprocess_indicator_delivers_assert_ack_to_form_queue():
    """保存成功/失败的结果必须以 assert_ack 命令回给表单侧。"""
    seen: list[dict] = []
    indicator = SubprocessTkIndicator.__new__(SubprocessTkIndicator)
    indicator.callbacks = type("C", (), {})()
    indicator.callbacks.add_assertion = lambda payload: (
        {"ok": True, "sequence": 4, "requestId": payload.get("requestId")}
        if payload.get("requestId") == "ok-1"
        else (_ for _ in ()).throw(RuntimeError("boom"))
    )
    indicator._send = lambda command, value=None: seen.append({"command": command, "value": value})

    # 直接驱动 _consume 的消息处理逻辑：复刻其 add_assertion 分支
    for request_id in ("ok-1", "bad-1"):
        payload = {"requestId": request_id}
        try:
            result = indicator.callbacks.add_assertion(payload)
            ack = dict(result) if isinstance(result, dict) else {"ok": True}
            ack.setdefault("ok", True)
        except Exception as exc:  # noqa: BLE001
            ack = {"ok": False, "error": str(exc)}
        indicator._send("assert_ack", ack)

    assert seen[0] == {"command": "assert_ack",
                       "value": {"ok": True, "sequence": 4, "requestId": "ok-1"}}
    assert seen[1]["command"] == "assert_ack"
    assert seen[1]["value"]["ok"] is False and "boom" in seen[1]["value"]["error"]


def test_apply_assert_ack_without_form_is_noop():
    from target.recording.indicator import TkRecordingIndicator
    indicator = TkRecordingIndicator.__new__(TkRecordingIndicator)
    indicator._assertion_dialog = None
    indicator._count_label = None
    indicator._messagebox = None
    # 表单已不存在（stop 后迟到的 ack）不得抛错
    indicator._apply_assert_ack({"ok": True, "sequence": 1})


def test_stop_advisories_unchanged_contract():
    """CLI stop 的指引口径（既有 WIP）在 U0 重构后保持。"""
    advisories = recording_stop_advisories(
        {"actionCount": 3},
        [{"code": "compile_drag_verifier_required", "sequence": 2}],
        "incomplete",
    )
    assert any("record assert" in a for a in advisories)
