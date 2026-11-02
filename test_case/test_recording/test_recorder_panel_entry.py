"""U1 回归：可见入口、窗口定位与 Agent 收集桥（录制易用性批次）。

对应设计 §3.1/§6 U1 验收：不经 LLM 能开面板完成录制；重复打开不重复钩子；
歧义窗口要求人工选择；旧 HWND 不误聚焦（销毁重建报 stale）；停止并收集后
显示真实路径。Tk 面板的 live 交互在 macOS 桌面人工验收，本文件覆盖全部
路由与桥逻辑（离线可测部分）。
"""

import io
import json
import socket
import threading
from pathlib import Path

import pytest

from agent.recording.ui_bridge import UIBridgeError, run_recording_ui_bridge
from target.recording.models import RecordingModelError
from target.recording.service import RecordingService
from target.recording.ui_controller import RecorderUIController, RecorderPanelHost

REPO = Path(__file__).resolve().parents[2]


class _FakeBackend:
    backend = "windows_pywinauto"

    def __init__(self, windows):
        self.windows = windows          # list[dict]
        self.activated: list[str] = []

    def list_windows(self):
        return {"ok": True, "backend": self.backend, "windows": self.windows}

    def activate_app(self, app_name=None, bundle_id=None):
        self.activated.append(app_name)
        return {"ok": True, "activated": app_name}

    def process_name_for_pid(self, pid):
        return {2156: "HiSecEndpointAgent.exe", 999: "Other.exe"}.get(pid, f"pid:{pid}")


class _FakeSource:
    seeded_scope = ()
    dropped_packets = 0
    out_of_scope_events = 0

    def start(self): ...
    def stop(self): ...
    def pause(self): ...
    def resume(self): ...
    def status(self): return {}
    def heartbeat(self): return {}


def _service(tmp_path):
    return RecordingService(
        target_name="win-dev", backend="windows_pywinauto",
        source_factory=lambda scope, sink: _FakeSource(),
    )


def _controller(tmp_path, windows):
    backend = _FakeBackend(windows)
    controller = RecorderUIController(_service(tmp_path), backend=backend)
    return controller, backend


def _windows():
    return [
        {"title": "logo1", "process_id": 2156, "class_name": "Dialog",
         "handle": 525054, "visible": True, "rect": [17, 53, 920, 600]},
        {"title": "EDR-WD 录制面板 [recorder_ui=true]", "process_id": 1,
         "class_name": "TkTopLevel", "handle": 1, "visible": True},
        {"title": "hidden", "process_id": 999, "class_name": "X",
         "handle": 2, "visible": False},
        {"title": "Program Manager", "process_id": 999, "class_name": "Pane",
         "handle": 3, "visible": True},
    ]


# ── 窗口列表：排除自身/不可见，候选带稳定 id ────────────────────────


def test_window_list_excludes_recorder_ui_and_invisible(tmp_path):
    controller, _ = _controller(tmp_path, _windows())
    result = controller.list_recordable_windows()
    titles = [w["title"] for w in result["windows"]]
    assert "logo1" in titles and "Program Manager" in titles
    assert all("[recorder_ui=true]" not in t for t in titles)
    assert "hidden" not in titles
    ids = [w["selectionId"] for w in result["windows"]]
    assert len(ids) == len(set(ids)) and all(ids)


def test_window_entries_carry_process_name_from_suggester(tmp_path):
    controller, _ = _controller(tmp_path, _windows())
    result = controller.list_recordable_windows()
    logo = next(w for w in result["windows"] if w["title"] == "logo1")
    assert logo["processName"] == "HiSecEndpointAgent.exe"
    other = next(w for w in result["windows"] if w["title"] == "Program Manager")
    assert other["processName"] == "Other.exe"


# ── 定位窗口：身份复核，销毁重建报 stale ───────────────────────────


def _pick(controller, title):
    return next(w for w in controller.list_recordable_windows()["windows"]
                if w["title"] == title)["selectionId"]


def test_focus_verifies_identity_then_activates(tmp_path):
    controller, backend = _controller(tmp_path, _windows())
    selection_id = _pick(controller, "logo1")
    result = controller.focus_selected_window(selection_id)
    assert result["ok"] is True
    assert backend.activated == ["HiSecEndpointAgent.exe"]


def test_focus_refuses_when_handle_disappears(tmp_path):
    controller, _ = _controller(tmp_path, _windows())
    selection_id = _pick(controller, "logo1")
    controller._backend.windows = [w for w in controller._backend.windows
                                   if w["handle"] != 525054]
    with pytest.raises(RecordingModelError) as exc:
        controller.focus_selected_window(selection_id)
    assert exc.value.code == "recording_window_stale"


def test_focus_refuses_same_title_rebuilt_window(tmp_path):
    """同名窗口重建（新 handle）必须重选，不能按标题静默跟随。"""
    controller, _ = _controller(tmp_path, _windows())
    selection_id = _pick(controller, "logo1")
    controller._backend.windows[0]["handle"] = 777   # 同名重建
    with pytest.raises(RecordingModelError) as exc:
        controller.focus_selected_window(selection_id)
    assert exc.value.code == "recording_window_stale"


# ── 从选择启动：转义精确标题 + 唯一会话门 ──────────────────────────


def test_start_from_selection_binds_exact_escaped_title(tmp_path):
    controller, _ = _controller(tmp_path, _windows())
    selection_id = _pick(controller, "logo1")
    result = controller.start_from_selection(selection_id, "policy flow")
    assert result["ok"] is True
    assert result["scope"]["windowTitle"] == "^logo1$"
    assert result["scope"]["processName"] == "HiSecEndpointAgent.exe"


def test_start_from_selection_requires_name(tmp_path):
    controller, _ = _controller(tmp_path, _windows())
    selection_id = _pick(controller, "logo1")
    with pytest.raises(RecordingModelError) as exc:
        controller.start_from_selection(selection_id, "   ")
    assert exc.value.code == "recording_name_required"


def test_start_from_unknown_selection_is_clean_error(tmp_path):
    controller, _ = _controller(tmp_path, _windows())
    with pytest.raises(RecordingModelError) as exc:
        controller.start_from_selection("deadbeef", "x")
    assert exc.value.code == "recording_selection_unknown"


# ── 面板请求路由与 requestId 去重 ──────────────────────────────────


def test_apply_ui_request_routes_pause_stop_and_unknown(tmp_path):
    controller, _ = _controller(tmp_path, _windows())
    controller.start_from_selection(_pick(controller, "logo1"), "flow")
    assert controller.apply_ui_request("pause", {})["state"] == "paused"
    assert controller.apply_ui_request("resume", {})["state"] == "recording"
    stop = controller.apply_ui_request("stop", {})
    assert stop["ok"] is True and "recording" not in stop   # 精简输出，无 raw 文档
    with pytest.raises(RecordingModelError):
        controller.apply_ui_request("explode", {})


def test_host_deduplicates_requests_by_request_id(tmp_path):
    """双击「开始录制」两个 requestId 之外，同 id 重试不得开出第二个会话。"""
    controller, _ = _controller(tmp_path, _windows())
    service = controller._service
    selection_id = _pick(controller, "logo1")
    host = RecorderPanelHost(controller)
    first = host.complete_ui_request("req-1", "start",
                                     {"selectionId": selection_id, "name": "flow"})
    second = host.complete_ui_request("req-1", "start",
                                      {"selectionId": selection_id, "name": "flow"})
    assert first["ok"] is True and second.get("duplicate") is True
    # 只开了一个会话：再来一个不同 id 的 start 会撞唯一会话门，
    # 错误按设计回表单（ok=False + code），不向上抛、更不会双开。
    second_start = host.complete_ui_request("req-2", "start",
                                            {"selectionId": selection_id, "name": "flow"})
    assert second_start["ok"] is False
    assert second_start["code"] == "recording_session_active"


@pytest.mark.parametrize("child_error", [None, "Tk initialization failed"])
def test_host_new_launch_does_not_reuse_ready_or_hide_error(tmp_path, monkeypatch, child_error):
    import subprocess
    import threading
    controller, _ = _controller(tmp_path, _windows())
    host = RecorderPanelHost(controller, startup_timeout=0)
    host._ready.set()
    host._last_error = "old child failure"

    class Process:
        stdin = io.StringIO()
        def poll(self): return None

    class Worker:
        def __init__(self, target, **kwargs): self.target = target
        def start(self):
            if self.target == host._consume and child_error:
                host._last_error = child_error
                host._ready.set()

    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: Process())
    monkeypatch.setattr(threading, "Thread", Worker)
    monkeypatch.setattr(host, "stop", lambda: None)
    with pytest.raises(RecordingModelError) as error:
        host.start()
    assert "old child failure" not in str(error.value)
    assert (child_error or "startup timed out") in str(error.value)


def test_host_start_is_idempotent_when_process_alive(tmp_path):
    controller, _ = _controller(tmp_path, _windows())
    host = RecorderPanelHost(controller)

    class _Alive:
        def __init__(self):
            self.stdin = io.StringIO()   # R3a：alreadyRunning 分支会下发 restore
        def poll(self):
            return None

    host._process = _Alive()
    result = host.start()
    assert result["alreadyRunning"] is True
    # 恢复推送必须真实下发（收起后的面板才能回到屏幕）
    assert "restore" in host._process.stdin.getvalue()


# ── Agent 收集桥：面板停止 → 拉结果 → 落盘真实路径 ─────────────────


class _ScriptedAgent:
    """call_tool 脚本：open → recording → stopped → result → capture miss。"""

    def __init__(self, session_id, recording_doc):
        self.session_id = session_id
        self.recording_doc = recording_doc
        self.calls: list[tuple[str, dict]] = []

    def call_tool(self, name, arguments=None, timeout=None):
        self.calls.append((name, dict(arguments or {})))
        if name == "open_recording_ui":
            return {"ok": True}
        if name == "recording_status":
            if len([c for c in self.calls if c[0] == "recording_status"]) == 1:
                return {"ok": True, "sessionId": self.session_id,
                        "state": "recording", "actionCount": 2}
            return {"ok": True, "sessionId": self.session_id, "state": "stopped"}
        if name == "get_recording_result":
            return {"ok": True, "sessionId": self.session_id,
                    "recording": self.recording_doc}
        if name == "get_recording_capture":
            return {"ok": False, "code": "recording_capture_missing"}
        raise AssertionError(f"unexpected tool {name}")


def test_bridge_collects_after_panel_stop_and_prints_real_path(tmp_path, capsys):
    doc = json.loads((REPO / "recordings" / "endcloud_demo_20261022_r2" /
                      "recording.json").read_text(encoding="utf-8"))
    session_id = doc["sessionId"]
    agent = _ScriptedAgent(session_id, doc)
    result = run_recording_ui_bridge(
        agent, output_root=tmp_path, profile=None, interval=0.01, log=lambda m: None)
    assert result["ok"] is True
    assert result["sessionId"] == session_id
    assert Path(result["directory"]).exists()
    assert (Path(result["directory"]) / "golden-trace.json").exists()
    # 续租只发生在同一会话：bridge 每 status 都带 heartbeat=True；
    # U3 后 bridge 启动还会 list/mark 历史 pending 会话（收集链的一部分）。
    assert all("recording_status" == c[0] or c[0] in
               {"open_recording_ui", "get_recording_result", "get_recording_capture",
                "list_recording_results", "mark_recording_collected"}
               for c in agent.calls)
    out = capsys.readouterr().out  # 未使用默认 log，不产生输出
    assert out == ""


def test_bridge_reconnects_on_transient_errors(tmp_path):
    doc = json.loads((REPO / "recordings" / "endcloud_demo_20261022_r2" /
                      "recording.json").read_text(encoding="utf-8"))
    session_id = doc["sessionId"]

    class _FlakyAgent(_ScriptedAgent):
        def call_tool(self, name, arguments=None, timeout=None):
            if name == "recording_status":
                self.calls.append((name, {}))
                if len([c for c in self.calls if c[0] == "recording_status"]) <= 2:
                    raise ConnectionError("tunnel down")
                return super().call_tool(name, arguments, timeout)
            return super().call_tool(name, arguments, timeout)

    agent = _FlakyAgent(session_id, doc)
    result = run_recording_ui_bridge(
        agent, output_root=tmp_path, interval=0.01, log=lambda m: None)
    assert result["ok"] is True


def test_bridge_gives_up_after_reconnect_budget(tmp_path):
    class _DeadAgent:
        def call_tool(self, name, arguments=None, timeout=None):
            if name == "open_recording_ui":
                return {"ok": True}
            raise ConnectionError("down")

    with pytest.raises(UIBridgeError):
        run_recording_ui_bridge(_DeadAgent(), interval=0.01,
                                reconnect_tries=3, log=lambda m: None)


# ── open_recorder 入口的可达性探测 ────────────────────────────────


def test_open_recorder_probe_detects_listening_port(tmp_path):
    import socket

    sys_path = str(REPO / "scripts")
    if sys_path not in __import__("sys").path:
        __import__("sys").path.insert(0, sys_path)
    from open_recorder import _host_reachable

    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]
    try:
        assert _host_reachable(f"http://127.0.0.1:{port}/mcp") is True
    finally:
        server.close()
    free = socket.socket()
    free.bind(("127.0.0.1", 0))
    free_port = free.getsockname()[1]
    free.close()
    assert _host_reachable(f"http://127.0.0.1:{free_port}/mcp") is False
