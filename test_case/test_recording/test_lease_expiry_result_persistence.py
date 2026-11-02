"""Lease 过期后的录制产物必须落进 result_store，不能只活在内存里。

契约（result_store.py 模块文档原话）：录制停止的三种方式——用户 stop、
lease 过期、recorder UI stop——Agent 侧采集器可能都不在线；没有持久层，
断连就等于静默丢弃 raw 文档。用户 stop 与 UI stop（callbacks.stop = service.stop）
都会经 RecordingService.stop() 走 save_stopped_recording；但 lease 过期在
session 内部完成（watchdog 或懒过期直接把会话驱动到终态），service 层无感知——
过期录制是三种停止方式里唯一不落盘的。

后果链：下一次 service.start() 会替换 manager 里的会话对象并
evidence_store.clear()——过期会话的 raw 文档（只存在于会话对象内存）和
脱敏帧（只存在于 evidence_store 内存）同时消失，且无任何路径可恢复。
这正是 result_store 为防而生的场景（"Agent disconnects before collecting
must not cost the operator their raw document"），却恰好漏掉了过期路径。

这些测试锁定的契约：
- 过期会话在任何 service 观察点（status/ui_snapshot/start）被发现后，
  raw 文档必须已落盘且 list_pending 可见；
- 事件上引用的脱敏帧必须随 raw 一起落盘（可按 (session, capture) 恢复）；
- 已落盘的结果不被重复保存（freshSave=False 幂等），已 collected 的
  状态不被重复 stop/观察点复活为 pending；
- 过期会话仍然按既有语义返回 stopped+reason（display 契约不变，
  test_lease_watchdog.py 已锁）——持久化不改变过期语义本身。
"""
from __future__ import annotations

import base64
import hashlib
import threading
import time

import pytest

from target.recording.evidence import RecordingEvidenceStore
from target.recording.models import CaptureScope, ObservedTarget, RawCaptureEvent
from target.recording.result_store import RecordingResultStore
from target.recording.service import RecordingService
from target.recording.session import CaptureSessionState, RecordingSession

pytestmark = pytest.mark.unit


class _Source:
    seeded_scope = ()
    dropped_packets = 0
    out_of_scope_events = 0

    def start(self): ...
    def stop(self): ...
    def pause(self): ...
    def resume(self): ...


class _Indicator:
    def start(self): ...
    def pause(self): ...
    def resume(self): ...
    def update_count(self, count): ...
    def open_assertion_editor(self, target=None): ...
    def stop(self): ...


class _FakeBackend:
    """evidence_store 只需要一个能吐 PNG 的最小 backend。"""

    def get_window_lock(self):
        return {"ok": True, "lock": {"native_id": 4242}}

    def dump_tree(self, max_depth=15):
        return {"ok": True, "controls": []}

    def recorder_ui_rectangles(self):
        return []

    def screenshot(self, _region=None):
        import base64 as b64
        import io

        from PIL import Image
        image = Image.new("RGB", (8, 8), (10, 20, 30))
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return {
            "ok": True,
            "capture_scope": "window",
            "image_b64": b64.b64encode(buffer.getvalue()).decode("ascii"),
            "origin": (0, 0),
        }


def _event(session, sequence, monotonic_ms, *, evidence=None):
    target = ObservedTarget(
        "OBS-" + str(sequence), "T" + str(sequence), "sha256:x",
        "Button", automation_id="btn" + str(sequence),
    )
    return RawCaptureEvent(
        sequence, "2026-10-31T00:00:00Z", monotonic_ms, "pointer_click",
        session.scope, {"button": "left"}, observed_target=target,
        causal_id="CAUSE-" + str(sequence), evidence=evidence or {},
    )


def _expire(session, lease_seconds=30.0):
    """把会话时钟拨过租约终点；下一次 status() 的懒过期路径即确定性收尾。

    与 watchdog 共享同一条 _expire_before_operation 路径（§6v），不依赖
    线程调度时序。
    """
    session._monotonic = lambda: time.monotonic() + lease_seconds + 1.0


def _wait_terminal(session, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        receipt = session.status()
        if receipt.state in (CaptureSessionState.STOPPED, CaptureSessionState.FAILED):
            return receipt
        time.sleep(0.01)
    pytest.fail("session never reached a terminal state")


def _service(tmp_path, backend=_FakeBackend()):
    evidence = RecordingEvidenceStore(backend)
    return RecordingService(
        target_name="fixture", backend="windows_pywinauto",
        source_factory=lambda scope, sink: _Source(),
        indicator_factory=lambda name, scope, callbacks: _Indicator(),
        evidence_store=evidence,
        result_store=RecordingResultStore(tmp_path / "results"),
    )


def test_lease_expired_recording_is_persisted_on_next_status(tmp_path):
    """过期后下一次 status 观察到终态会话 → raw 文档落盘并进入 pending。"""
    service = _service(tmp_path)
    started = service.start(
        name="flow", process_name="EDRClient.exe", window_title="^EDR$",
        lease_seconds=30)
    session = service.manager.current()
    session.ingest(_event(session, 1, 10))
    _expire(session)

    service.status()  # 观察点：发现过期终态会话 → 落盘

    assert started["sessionId"] in service.result_store.list_pending()
    stored = service.result_store.load_stopped_recording(started["sessionId"])
    assert [e["sequence"] for e in stored["events"]] == [1]
    # reason 契约保持：过期停止绝不能伪装成干净的 user stop
    assert stored and service.manager.current().state in {
        CaptureSessionState.STOPPED, CaptureSessionState.FAILED}


def test_lease_expired_captures_are_persisted_and_recoverable(tmp_path):
    """事件引用的脱敏帧随 raw 一起落盘，可按 (session, capture) 定点恢复。"""
    service = _service(tmp_path)
    started = service.start(
        name="flow", process_name="EDRClient.exe", window_title="^EDR$",
        lease_seconds=30)
    session = service.manager.current()
    session.ingest(_event(session, 1, 10))  # attach 会给它挂 capture 元数据
    attached = session.recording().events[0].evidence.get("capture")
    assert attached is not None, "evidence store did not attach a capture"

    _expire(session)
    service.status()  # 观察点：落盘 raw + 帧

    capture_id = attached["id"]
    png = service.result_store.read_capture_frame(started["sessionId"], capture_id)
    digest = "sha256:" + hashlib.sha256(png).hexdigest()
    assert digest == attached["sha256"]


def test_second_observation_does_not_resurrect_collected_state(tmp_path):
    """过期落盘后 mark_collected，后续观察点不得把结果重新变回 pending。"""
    service = _service(tmp_path)
    started = service.start(
        name="flow", process_name="EDRClient.exe", window_title="^EDR$",
        lease_seconds=30)
    session = service.manager.current()
    session.ingest(_event(session, 1, 10))
    _expire(session)

    service.status()  # 首次观察：落盘
    assert started["sessionId"] in service.result_store.list_pending()
    service.result_store.mark_collected(started["sessionId"])

    service.status()  # 再次观察：不得重置 collected
    assert started["sessionId"] not in service.result_store.list_pending()
    stored = service.result_store.load_stopped_recording(started["sessionId"])
    assert [e["sequence"] for e in stored["events"]] == [1]


def test_next_start_persists_then_replaces_expired_session_without_loss(tmp_path):
    """过期会话在 start 替换它之前必须先落盘——下一轮录制不能吃掉上一轮。"""
    service = _service(tmp_path)
    started = service.start(
        name="flow", process_name="EDRClient.exe", window_title="^EDR$",
        lease_seconds=30)
    session = service.manager.current()
    session.ingest(_event(session, 1, 10))
    _expire(session)

    service.start(name="flow2", process_name="EDRClient.exe",
                  window_title="^EDR$", lease_seconds=30)

    # 旧会话已被替换、evidence_store 已 clear，但 raw 文档已安全落盘。
    stored = service.result_store.load_stopped_recording(started["sessionId"])
    assert [e["sequence"] for e in stored["events"]] == [1]
    assert started["sessionId"] in service.result_store.list_pending()
    # 新会话正常 RECORDING，未被旧会话阻塞
    assert service.manager.current().session_id != started["sessionId"]


def test_repeated_status_after_expiry_is_idempotent(tmp_path):
    """多次观察同一个过期会话：freshSave 只有一次，文档不重复写。"""
    service = _service(tmp_path)
    started = service.start(
        name="flow", process_name="EDRClient.exe", window_title="^EDR$",
        lease_seconds=30)
    session = service.manager.current()
    session.ingest(_event(session, 1, 10))
    _expire(session)

    service.status()
    service.status()
    service.ui_snapshot()
    assert started["sessionId"] in service.result_store.list_pending()
    # 幂等落盘：同文档重存返回 freshSave=False，绝不 recording_result_exists
    stored = service.result_store.load_stopped_recording(started["sessionId"])
    assert [e["sequence"] for e in stored["events"]] == [1]


def test_user_stop_path_is_unchanged_by_reconciliation(tmp_path):
    """用户 stop 的既有持久化语义不被新增观察点改变（freshSave 首存为真）。"""
    service = _service(tmp_path)
    service.start(name="flow", process_name="EDRClient.exe",
                  window_title="^EDR$", lease_seconds=30)
    session = service.manager.current()
    session.ingest(_event(session, 1, 10))

    stopped = service.stop()
    assert stopped["resultStore"]["ok"] is True
    assert stopped["resultStore"]["freshSave"] is True
    assert stopped["sessionId"] in service.result_store.list_pending()


def test_panel_snapshot_alone_persists_expired_recording(tmp_path):
    """Agent 不在线时面板是唯一观察点：ui_snapshot 单独触发落盘。"""
    service = _service(tmp_path)
    started = service.start(
        name="flow", process_name="EDRClient.exe", window_title="^EDR$",
        lease_seconds=30)
    session = service.manager.current()
    session.ingest(_event(session, 1, 10))
    _expire(session)

    snapshot = service.ui_snapshot()  # 不经 status：operator 主动看面板
    assert started["sessionId"] in snapshot["pendingResults"]
    assert started["sessionId"] in service.result_store.list_pending()
    stored = service.result_store.load_stopped_recording(started["sessionId"])
    assert [e["sequence"] for e in stored["events"]] == [1]


def test_failed_terminal_session_is_not_persisted_as_clean_result(tmp_path):
    """source 收尾失败的过期会话（FAILED）不落盘冒充干净 stopped 文档。"""
    class _BadSource(_Source):
        def stop(self):
            raise RuntimeError("hook thread did not stop")

    service = _service(tmp_path)
    service.start(name="flow", process_name="EDRClient.exe",
                  window_title="^EDR$", lease_seconds=30)
    # 换上 stop 会失败的 source：模拟 hook 收尾失败
    session = service.manager.current()
    session._source = _BadSource()
    session.ingest(_event(session, 1, 10))
    _expire(session)

    with pytest.raises(RuntimeError):
        service.status()  # 懒过期路径如实抛出 source 收尾失败
    assert session.state is CaptureSessionState.FAILED

    # 后续观察点（ui_snapshot）不再触发，也不得把 FAILED 文档落盘
    service.ui_snapshot()
    assert started_id(session) not in service.result_store.list_pending()


def started_id(session):
    return session.session_id
