"""批次 5 恢复轮（续）：CLI ``record stop`` 收集链路的断连恢复回归。

背景（2026-10-30 晚轮审计）：§6y 修复了 ``run_recording_ui_bridge`` 收集链
（UI 停止路径）的裸 ``call_tool`` 缺口——``_RetryingAgent`` 给每个收集调用
独立连续失败预算。但 ``agent/cli.py`` 的 ``record stop`` 分支（人肉 CLI 停止
路径）同样调用 ``collect_recording_artifacts``——逐帧 ``get_recording_capture``
在收集链路上裸执行：一条隧道抖动把整个 ``record stop`` 打死，操作者只看到
``recording_persistence_failed``，产物全部留在目标端 pending（无人 mark，
也不产生任何重试）。同一故障在 UI 桥路径设计为可恢复；CLI 主路径应当一致。

契约：
- 收集链路出现 ≤ budget 次瞬态异常 → 重试后成功收集，落盘产物与无抖动
  时一致（目录、recording.json、golden-trace.json）。
- 收集链路持续失败 → ``record stop`` 返回结构化失败（ok=False），异常
  不能以裸 traceback 冒出 CLI；有限次尝试后放弃，不无限重试。
- 修复不得改变 UI 桥路径的既有语义（§6y 回归继续绿）。
"""

import base64
import hashlib
import io
import json
from pathlib import Path

import pytest
from PIL import Image

from agent import cli


def _png(color):
    output = io.BytesIO()
    Image.new("RGB", (8, 8), color).save(output, format="PNG")
    return output.getvalue()


def _recording_doc():
    payload = _png("blue")
    digest = "sha256:" + hashlib.sha256(payload).hexdigest()
    capture_id = "CAP-" + "1" * 32
    return {
        "schema": "edr.desktop-recording/v1",
        "sessionId": "REC-CLI-RECONNECT",
        "name": "cli reconnect flow",
        "captureDiagnostics": {"droppedPackets": 0, "correlationErrorCount": 0},
        "events": [{
            "sequence": 1,
            "wallTime": "2026-10-30T00:00:00Z",
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
            "evidence": {"capture": {
                "id": capture_id, "sha256": digest, "width": 8, "height": 8,
                "origin": [0, 0], "redacted": True, "redactions": [],
            }},
        }],
    }


class _CliStopFlakyAgent:
    """stop_recording 正常；收集链路前 N 次调用 raise，之后放行。

    CLI stop 路径的收集链是逐帧 ``get_recording_capture``（recording 已在
    stop_recording 载荷里，无独立 get_recording_result 调用）。
    """

    def __init__(self, fail_times, fail_on):
        self.calls: list[tuple[str, dict | None, float | None]] = []
        self.fail_times = fail_times
        self.fail_on = fail_on
        self.recording = _recording_doc()
        self.payload = _png("blue")

    def ensure_ready(self) -> dict:
        return {"ok": True}

    def call_tool(self, name, arguments=None, timeout=None):
        self.calls.append((name, arguments, timeout))
        if name in self.fail_on and len(
                [c for c in self.calls if c[0] == name]) <= self.fail_times:
            raise ConnectionError("tunnel blip during cli stop collection")
        if name == "recording_status":
            return {"ok": False, "code": "recording_session_missing"}
        if name == "stop_recording":
            return {"ok": True, "state": "stopped", "recording": dict(self.recording)}
        if name == "get_recording_capture":
            return {
                "ok": True,
                "captureId": arguments.get("capture_id"),
                "sha256": "sha256:" + hashlib.sha256(self.payload).hexdigest(),
                "image_b64": base64.b64encode(self.payload).decode("ascii"),
            }
        return {"ok": True}


def test_cli_stop_transient_failure_recovers_and_persists(monkeypatch, tmp_path, capsys):
    agent = _CliStopFlakyAgent(
        fail_times=2, fail_on={"get_recording_capture"})
    monkeypatch.setattr(cli, "_target_agent", lambda *_args: agent)

    exit_code = cli.main([
        "--target", "win-dev", "record", "stop", "--output-root", str(tmp_path),
        "--profile", "windows_hisec",
    ])
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 0, output
    assert output["ok"] is True
    assert (Path(output["directory"]) / "recording.json").exists()
    assert (Path(output["directory"]) / "golden-trace.json").exists()
    # 重试确实发生了（收集链路至少重试过，不是一次通过）
    assert sum(1 for n, _, _ in agent.calls
               if n == "get_recording_capture") >= 3


def test_cli_stop_persistent_failure_is_structured_not_a_crash(monkeypatch, tmp_path, capsys):
    agent = _CliStopFlakyAgent(fail_times=10_000, fail_on={"get_recording_capture"})
    monkeypatch.setattr(cli, "_target_agent", lambda *_args: agent)

    exit_code = cli.main([
        "--target", "win-dev", "record", "stop", "--output-root", str(tmp_path),
        "--profile", "windows_hisec",
    ])
    output = json.loads(capsys.readouterr().out)

    # 预算耗尽：CLI 退出码非零、输出结构化、没有裸 traceback。
    assert exit_code != 0
    assert output["ok"] is False
    assert output.get("code")
    # 预算语义：有限次尝试后放弃，不是无限重试。
    attempts = sum(1 for n, _, _ in agent.calls if n == "get_recording_capture")
    assert 3 <= attempts <= 10


def test_ui_bridge_reconnect_semantics_unchanged():
    """修复不得动 §6y 的桥路径契约：_RetryingAgent 仍是收集链路的恢复件。"""
    from agent.recording.ui_bridge import _RetryingAgent

    class _AlwaysFail:
        def call_tool(self, name, arguments=None, timeout=None):
            raise ConnectionError("down")

    wrapper = _RetryingAgent(_AlwaysFail(), interval=0.001, reconnect_tries=2,
                             log=lambda m: None)
    with pytest.raises(Exception) as exc:
        wrapper.call_tool("get_recording_capture")
    assert "connection lost" in str(exc.value).lower()
