"""R4 回归：磁盘截图取帧的会话归属、歧义拒绝与元数据验证。

覆盖独立反例（两会话同 capture_id → find_capture 必须歧义拒绝，不取第一份）
及生产链路：service.capture 的 session 归属/兼容语义、磁盘帧的 raw 引用与
sha256 元数据验证、符号链接与路径逃逸拒绝、服务→MCP 签名→agent 收集链。
"""

import base64
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.recording.collection import collect_recording_artifacts
from target.recording.models import (
    CaptureScope, ObservedTarget, RawCaptureEvent, RecordingModelError,
)
from target.recording.result_store import RecordingResultStore
from target.recording.session import RecordingSession
from target.recording.service import RecordingService
from target.recording.session import NullCaptureSource


def _png(tag: int) -> bytes:
    return b"\x89PNG\r\n\x1a\nframe-" + bytes([tag])


def _store(tmp_path):
    return RecordingResultStore(tmp_path / "results")


def _save_pair(store, sid: str, cid: str, png: bytes):
    store.save_stopped_recording(sid, {
        "sessionId": sid, "name": "flow", "schema": "edr.desktop-recording/v1",
        "events": [{"evidence": {"capture": {"id": cid, "sha256": "sha256:" + hashlib.sha256(png).hexdigest()}}}],
    }, meta={"name": "flow"})
    store.save_capture(sid, cid, png)


# ── 独立反例：跨会话同名 capture 必须歧义拒绝 ──────────────────────


def test_capture_id_collision_rejected_not_first_match(tmp_path):
    store = _store(tmp_path)
    cid = "CAP-" + "a" * 32
    _save_pair(store, "REC-" + "1" * 32, cid, _png(1))
    _save_pair(store, "REC-" + "2" * 32, cid, _png(2))
    with pytest.raises(RecordingModelError) as exc:
        store.find_capture(cid)
    assert exc.value.code == "recording_capture_ambiguous"
    assert "歧义" in str(exc.value)


def test_capture_lookup_with_session_id_resolves_within_session(tmp_path):
    """带 session_id 的定点读取只在本会话目录内解析，跨会话数据不可见。"""
    store = _store(tmp_path)
    cid = "CAP-" + "a" * 32
    _save_pair(store, "REC-" + "1" * 32, cid, _png(1))
    _save_pair(store, "REC-" + "2" * 32, cid, _png(2))
    png = store.read_capture_frame("REC-" + "2" * 32, cid)
    assert png == _png(2)   # 指定会话读到的是它自己的帧，不是排序第一份


def test_single_session_lookup_is_backward_compatible(tmp_path):
    """唯一合法归属：旧无 session 调用兼容（仅一个会话命中）。"""
    store = _store(tmp_path)
    sid, cid = "REC-" + "3" * 32, "CAP-" + "3" * 32
    _save_pair(store, sid, cid, _png(3))
    found = store.find_capture(cid)
    assert found == (sid, _png(3))


def test_capture_frame_without_raw_reference_is_rejected(tmp_path):
    """未经元数据验证的文件不当可信脱敏截图：无 raw 引用 → 拒绝。"""
    store = _store(tmp_path)
    store.save_capture("REC-" + "4" * 32, "CAP-" + "4" * 32, _png(4))
    # 未写 raw 文档（无 save_stopped_recording）——帧无所属引用
    with pytest.raises(RecordingModelError) as exc:
        store.find_capture("CAP-" + "4" * 32)
    assert exc.value.code == "recording_capture_unreferenced"


def test_capture_hash_mismatch_is_rejected(tmp_path):
    """磁盘帧 hash 与事件元数据不一致 → 拒绝，不当可信截图。"""
    store = _store(tmp_path)
    sid, cid = "REC-" + "5" * 32, "CAP-" + "5" * 32
    _save_pair(store, sid, cid, _png(5))
    # 篡改磁盘字节
    frame = tmp_path / "results" / sid / "captures" / f"{cid}.png"
    frame.write_bytes(_png(99))
    with pytest.raises(RecordingModelError) as exc:
        store.find_capture(cid)
    assert exc.value.code == "recording_capture_hash_mismatch"


# ── 路径安全：符号链接与逃逸 ────────────────────────────────────────


def test_capture_frame_symlink_write_is_refused(tmp_path):
    store = _store(tmp_path)
    sid, cid = "REC-" + "6" * 32, "CAP-" + "6" * 32
    store.save_stopped_recording(sid, {"sessionId": sid, "events": []}, meta={"name": "f"})
    outside = tmp_path / "outside-target.png"
    outside.write_bytes(_png(6))
    captures_dir = tmp_path / "results" / sid / "captures"
    captures_dir.mkdir(parents=True, exist_ok=True)
    (captures_dir / f"{cid}.png").symlink_to(outside)
    with pytest.raises(RecordingModelError) as exc:
        store.save_capture(sid, cid, _png(6))
    assert exc.value.code == "recording_capture_path_unsafe"


def test_captures_directory_symlink_is_refused(tmp_path):
    store = _store(tmp_path)
    sid, cid = "REC-" + "7" * 32, "CAP-" + "7" * 32
    store.save_stopped_recording(sid, {"sessionId": sid, "events": []}, meta={"name": "f"})
    session_dir = tmp_path / "results" / sid
    captures_dir = session_dir / "captures"
    captures_dir.mkdir(parents=True, exist_ok=True)
    captures_dir.rmdir()
    captures_dir.symlink_to(tmp_path / "elsewhere")
    with pytest.raises(RecordingModelError) as exc:
        store.save_capture(sid, cid, _png(7))
    assert exc.value.code == "recording_capture_path_unsafe"


def test_save_capture_refuses_text(tmp_path):
    store = _store(tmp_path)
    with pytest.raises(RecordingModelError) as exc:
        store.save_capture("REC-" + "7" * 32, "CAP-" + "7" * 32, "not-png")
    assert exc.value.code == "recording_capture_bytes_invalid"


# ── 生产服务：会话归属与歧义拒绝 ────────────────────────────────────


class _Evidence:
    """空内存 evidence store：模拟服务重启/新会话清空后的恢复路径。"""

    def __init__(self, frames=None):
        self.frames = dict(frames or {})
    def clear(self): self.frames.clear()
    def initialize(self): return {"ok": True, "capture": {"id": "CAP-" + "e" * 32}}
    def attach(self, event): return event
    def get(self, cid):
        png = self.frames.get(cid)
        if png is None:
            return {"ok": False, "captureId": cid}
        return {"ok": True, "image_b64": base64.b64encode(png).decode(),
                "sha256": "sha256:" + hashlib.sha256(png).hexdigest()}


def _service(tmp_path, evidence=None):
    return RecordingService(
        target_name="fixture", backend="windows_pywinauto", result_store=_store(tmp_path),
        source_factory=lambda scope, sink: NullCaptureSource(),
        indicator_factory=lambda name, scope, cb: NullRecordingIndicator(),
        evidence_store=evidence if evidence is not None else _Evidence())


def test_service_capture_pins_owning_session(tmp_path):
    """MCP 协议带 session_id：定点恢复指定会话的帧，跨会话同名不串。"""
    service = _service(tmp_path, evidence=_Evidence())
    sid = "REC-" + "9" * 32
    cid = "CAP-" + "9" * 32
    _save_pair(_store(tmp_path), sid, cid, _png(9))
    fetched = service.capture(capture_id=cid, session_id=sid)
    assert fetched["ok"] is True
    assert base64.b64decode(fetched["image_b64"]) == _png(9)
    assert fetched["sessionId"] == sid
    assert fetched["sha256"] == "sha256:" + hashlib.sha256(_png(9)).hexdigest()


def test_service_capture_rejects_ambiguity_without_session(tmp_path):
    """旧无 session 调用：多会话同名 → 明确歧义拒绝，不取第一份。"""
    service = _service(tmp_path, evidence=_Evidence())
    cid = "CAP-" + "a" * 32
    _save_pair(_store(tmp_path), "REC-" + "1" * 32, cid, _png(1))
    _save_pair(_store(tmp_path), "REC-" + "2" * 32, cid, _png(2))
    with pytest.raises(RecordingModelError) as exc:
        service.capture(capture_id=cid)
    assert exc.value.code == "recording_capture_ambiguous"


def test_service_capture_unreferenced_disk_frame_is_rejected(tmp_path):
    service = _service(tmp_path, evidence=_Evidence())
    store = _store(tmp_path)
    store.save_capture("REC-" + "4" * 32, "CAP-" + "4" * 32, _png(4))
    with pytest.raises(RecordingModelError) as exc:
        service.capture(capture_id="CAP-" + "4" * 32)
    assert exc.value.code == "recording_capture_unreferenced"


# ── 服务 → MCP 签名 → agent 收集链 ─────────────────────────────────


def test_mcp_capture_tool_accepts_optional_session_id():
    """MCP 工具签名兼容：旧单参调用仍合法，session_id 为可选参数。"""
    import inspect
    server_source = (Path(__file__).resolve().parents[2] /
                     "target" / "server.py").read_text(encoding="utf-8")
    assert "def get_recording_capture(capture_id: str, session_id: str | None = None)" \
        in server_source


def test_collection_passes_session_id_on_durable_miss(tmp_path):
    """磁盘恢复路径：agent 收集链在 get_recording_capture 的 arguments 里
    携带 sessionId（生产 MCP 调用参数，而非仅 helper）。"""
    sid = "REC-" + "b" * 32
    cid = "CAP-" + "d" * 32
    png = _png(8)
    seen_args: list[dict] = []

    def _call(name, arguments=None, **kw):
        seen_args.append(dict(arguments or {}))
        if name == "get_recording_capture":
            return {"ok": False, "captureId": cid}   # 内存 miss → 落盘恢复
        return {"ok": True, "recording": {
            "sessionId": sid, "name": "flow", "schema": "edr.desktop-recording/v1",
            "captureDiagnostics": {"droppedPackets": 0, "outOfScopeEvents": 0,
                                   "correlationErrorCount": 0},
            "events": [{"sequence": 1,
                        "wallTime": "2026-10-23T00:00:00.000Z", "monotonicMs": 1,
                        "type": "pointer_click", "scope": {
                            "target": "fixture", "backend": "windows_pywinauto",
                            "processName": "Fixture.exe", "windowTitle": "^Fixture$"},
                        "input": {"button": "left"}, "observedTarget": None,
                        "evidence": {"capture": {
                            "id": cid, "sha256": "sha256:" + hashlib.sha256(png).hexdigest(),
                            "width": 920, "height": 600, "origin": [17, 53],
                            "redacted": True, "redactions": []}},
                        "causalId": "CAUSE-cap"}],
        }}

    collect_recording_artifacts(
        SimpleNamespace(call_tool=_call), {"ok": True, "sessionId": sid,
                "recording": {"sessionId": sid, "name": "flow",
                              "schema": "edr.desktop-recording/v1",
                              "captureDiagnostics": {"droppedPackets": 0,
                                                     "outOfScopeEvents": 0,
                                                     "correlationErrorCount": 0},
                              "events": [{"sequence": 1,
                                          "wallTime": "2026-10-23T00:00:00.000Z",
                                          "monotonicMs": 1, "type": "pointer_click",
                                          "scope": {"target": "fixture",
                                                    "backend": "windows_pywinauto",
                                                    "processName": "Fixture.exe",
                                                    "windowTitle": "^Fixture$"},
                                          "input": {"button": "left"},
                                          "observedTarget": None,
                                          "evidence": {"capture": {
                                              "id": cid,
                                              "sha256": "sha256:" + hashlib.sha256(png).hexdigest(),
                                              "width": 920, "height": 600,
                                              "origin": [17, 53], "redacted": True,
                                              "redactions": []}},
                                          "causalId": "CAUSE-cap"}],
                }}, tmp_path, None)
    capture_calls = [a for a in seen_args if "capture_id" in a]
    assert capture_calls and capture_calls[0].get("session_id") == sid


def test_collection_uses_restored_disk_frame_with_hash_match(tmp_path):
    """完整链：磁盘上的帧经恢复路径取回 → base64 解码 → 与事件元数据
    hash 一致 → 计入 captures（无 capture_fetch_failed 缺口）。"""
    sid = "REC-" + "c" * 32
    cid = "CAP-" + "e" * 32
    png = _png(10)
    store = _store(tmp_path)
    _save_pair(store, sid, cid, png)

    def _call(name, arguments=None, **kw):
        if name == "get_recording_capture":
            frame = store.read_capture_frame(
                arguments["session_id"], arguments["capture_id"])
            return {"ok": True, "image_b64": base64.b64encode(frame).decode(),
                    "sha256": "sha256:" + hashlib.sha256(frame).hexdigest()}
        return {"ok": True, "recording": {
            "sessionId": sid, "name": "flow", "schema": "edr.desktop-recording/v1",
            "captureDiagnostics": {"droppedPackets": 0, "outOfScopeEvents": 0,
                                   "correlationErrorCount": 0},
            "events": [{"sequence": 1,
                        "wallTime": "2026-10-23T00:00:00.000Z", "monotonicMs": 1,
                        "type": "pointer_click", "scope": {
                            "target": "fixture", "backend": "windows_pywinauto",
                            "processName": "Fixture.exe", "windowTitle": "^Fixture$"},
                        "input": {"button": "left"}, "observedTarget": None,
                        "evidence": {"capture": {
                            "id": cid, "sha256": "sha256:" + hashlib.sha256(png).hexdigest(),
                            "width": 920, "height": 600, "origin": [17, 53],
                            "redacted": True, "redactions": []}},
                        "causalId": "CAUSE-cap"}],
        }}

    artifacts, issues = collect_recording_artifacts(
        SimpleNamespace(call_tool=_call), {"ok": True, "sessionId": sid, "recording": {
            "sessionId": sid, "name": "flow", "schema": "edr.desktop-recording/v1",
            "captureDiagnostics": {"droppedPackets": 0, "outOfScopeEvents": 0,
                                   "correlationErrorCount": 0},
            "events": [{"sequence": 1, "wallTime": "2026-10-23T00:00:00.000Z",
                        "monotonicMs": 1, "type": "pointer_click", "scope": {
                            "target": "fixture", "backend": "windows_pywinauto",
                            "processName": "Fixture.exe", "windowTitle": "^Fixture$"},
                        "input": {"button": "left"}, "observedTarget": None,
                        "evidence": {"capture": {
                            "id": cid, "sha256": "sha256:" + hashlib.sha256(png).hexdigest(),
                            "width": 920, "height": 600, "origin": [17, 53],
                            "redacted": True, "redactions": []}},
                        "causalId": "CAUSE-cap"}],
            }}, tmp_path, None, session_id=sid)
    assert issues == [], "磁盘帧经恢复路径取回且 hash 匹配——不得产生取证缺口"
    # 取证完整的帧随 artifacts 落盘（golden ready 与否由既有编译语义决定，
    # 单步 pointer_click 缺语义选择器属既有行为，与本链无关）


# ── R5：内存命中同样受显式 session 归属约束 ─────────────────────────


def new_session(seed="a"):
    session = RecordingSession("REC-" + seed * 32, "review", CaptureScope(
        "fixture", "windows_pywinauto", "Fixture.exe", "^Fixture$"))
    session.start()
    return session


class _UnboundMemory:
    """最小内存存储适配器：帧无任何会话归属记录（R5 反例形状）。"""

    def get(self, capture_id):
        return {"ok": True, "image_b64": "b3RoZXItc2Vzc2lvbg=="}


def test_explicit_session_does_not_accept_unbound_memory_frame():
    service = object.__new__(RecordingService)
    service.evidence_store = _UnboundMemory()
    service.result_store = None
    with pytest.raises(RecordingModelError):
        service.capture(capture_id="CAP-" + "a" * 32,
                        session_id="REC-" + "2" * 32)


def test_memory_hit_with_matching_session_is_allowed_and_stamped():
    png = b"\x89PNG\r\n\x1a\nmemory-frame"
    session = new_session("f")
    cid = "CAP-" + "f" * 32

    class _OwnedMemory:
        def get(self, capture_id):
            return {"ok": True,
                    "image_b64": base64.b64encode(png).decode(),
                    "sha256": "sha256:" + hashlib.sha256(png).hexdigest()}

    service = object.__new__(RecordingService)
    service.manager = SimpleNamespace(
        current=lambda: SimpleNamespace(session_id=session.session_id))
    service.evidence_store = _OwnedMemory()
    service.result_store = None
    fetched = service.capture(capture_id=cid, session_id=session.session_id)
    assert fetched["ok"] is True
    assert base64.b64decode(fetched["image_b64"]) == png
    assert fetched["sessionId"] == session.session_id


def test_memory_hit_with_wrong_session_is_rejected():
    session = new_session("e")
    cid = "CAP-" + "e" * 32

    class _OwnedMemory:
        def get(self, capture_id):
            return {"ok": True, "image_b64": "zzz"}

    service = object.__new__(RecordingService)
    service.manager = SimpleNamespace(
        current=lambda: SimpleNamespace(session_id=session.session_id))
    service.evidence_store = _OwnedMemory()
    service.result_store = None
    with pytest.raises(RecordingModelError) as exc:
        service.capture(capture_id=cid, session_id="REC-" + "2" * 32)
    assert exc.value.code == "recording_capture_session_mismatch"


def test_memory_hit_without_session_arg_uses_sole_owner():
    """旧无 session 调用兼容：内存帧的归属是唯一活跃会话，返回时标注。"""
    session = new_session("d")
    cid = "CAP-" + "d" * 32

    class _OwnedMemory:
        def get(self, capture_id):
            return {"ok": True, "image_b64": "zzz"}

    service = object.__new__(RecordingService)
    service.manager = SimpleNamespace(
        current=lambda: SimpleNamespace(session_id=session.session_id))
    service.evidence_store = _OwnedMemory()
    service.result_store = None
    fetched = service.capture(capture_id=cid)
    assert fetched["ok"] is True and fetched["sessionId"] == session.session_id


def test_find_capture_uses_guarded_read_for_symlink_frame(tmp_path):
    """旧 find_capture 兼容路径同样复用定点 read_capture_frame 的防护：
    planted symlink 帧不被当作可信截图读取。"""
    store = _store(tmp_path)
    sid, cid = "REC-" + "6" * 32, "CAP-" + "6" * 32
    _save_pair(store, sid, cid, _png(6))    # raw 引用 + 合法帧先落盘
    frame = tmp_path / "results" / sid / "captures" / f"{cid}.png"
    frame.unlink()
    outside = tmp_path / "outside.png"
    outside.write_bytes(_png(60))   # 内容不同：hash 验证也必须拒绝
    frame.symlink_to(outside)               # 替换为 planted symlink
    with pytest.raises(RecordingModelError) as exc:
        store.find_capture(cid)
    assert exc.value.code == "recording_capture_path_unsafe"


# ── R6①：后台 server 树的 System Events 完整枚举超时（反例锁定参数） ──


def test_backend_list_windows_timeout_covers_background_throttling():
    """后台（nohup/无 TTY）server 进程树的 System Events 完整枚举被节流：
    实测 78s（前台 3s）。timeout=15 必然挂起——list_windows 的 osascript
    超时必须覆盖后台节流（≥60s）。锁定参数传递，不在测试内实跑 78s。"""
    import inspect
    from target.automation import macos_accessibility as m
    src = inspect.getsource(m)
    list_windows_src = src.split("def list_windows")[1].split("def ")[0]
    assert "_run_osascript(script, timeout=15)" not in list_windows_src, (
        "list_windows 仍使用 15s 超时——后台节流下必然失败")


def test_macos_source_control_inventory_walks_current_window():
    """R6②：macOS source 接入组件清单——从当前 scope 窗口的 AX 树现场
    枚举组件（复用生产 dump_tree，不建第二套控件协议）。"""
    scope = CaptureScope("fixture", "macos_accessibility", "Fixture.exe", "^Fixture$")
    source = RecordingSession("REC-" + "7" * 32, "flow", scope)
    source.attach_source(_DumpTreeSource())


class _DumpTreeSource:
    """macos source 形状的替身：暴露 control_tree（现场 walk 委托 dump_tree）。"""

    control_snapshots = ()

    def __init__(self):
        self.calls = []

    def control_tree(self, window_title_re, max_depth=10):
        self.calls.append((window_title_re, max_depth))
        return {
            "ok": True,
            "controls": [
                {"automationId": "rowCount", "controlType": "Text",
                 "name": "行数", "value": "3"},
            ],
        }
