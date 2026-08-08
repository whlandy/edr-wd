"""Tests for MCP-first file transfer and SCP fallback selection."""

from __future__ import annotations

import base64

from agent import file_transfer


class FakeConfig:
    def get_target(self, target):
        return {
            "platform": "windows",
            "ssh": {"user": "admin"},
            "file_transfer": {"remote_root": "C:/transfer"},
        }

    def resolve_auth(self, target):
        return {"host": "target", "user": "admin", "auth": {"type": "password", "password": "x"}}


class FakeMcpAgent:
    calls = []
    ready = {"ok": True}
    responses = []

    def __init__(self, target, config=None):
        self.target = target

    def ensure_ready(self):
        return dict(self.ready)

    def call_tool(self, name, arguments, timeout=None):
        self.calls.append((name, arguments))
        if self.responses:
            return self.responses.pop(0)
        return {"ok": False, "error": "tool not found"}


def test_upload_uses_mcp_chunks(monkeypatch, tmp_path):
    source = tmp_path / "source.bin"
    source.write_bytes(b"abcdef")
    FakeMcpAgent.calls = []
    FakeMcpAgent.ready = {"ok": True}
    FakeMcpAgent.responses = [
        {"ok": True, "next_offset": 3},
        {"ok": True, "next_offset": 6, "complete": True},
    ]
    monkeypatch.setattr(file_transfer, "TargetSubAgent", FakeMcpAgent)

    result = file_transfer.upload_file(
        "win", source, "nested/remote.bin", config=FakeConfig(), chunk_bytes=3,
    )
    assert result["ok"] is True
    assert result["transport"] == "mcp"
    assert [base64.b64decode(call[1]["content_base64"]) for call in FakeMcpAgent.calls] == [b"abc", b"def"]
    assert FakeMcpAgent.calls[-1][1]["final"] is True


def test_upload_falls_back_when_mcp_tool_is_missing(monkeypatch, tmp_path):
    source = tmp_path / "source.bin"
    source.write_bytes(b"payload")
    FakeMcpAgent.calls = []
    FakeMcpAgent.ready = {"ok": True}
    FakeMcpAgent.responses = [{"ok": False, "error": "unknown tool transfer_upload"}]
    monkeypatch.setattr(file_transfer, "TargetSubAgent", FakeMcpAgent)
    captured = {}

    def fake_scp(
        ssh, local, remote, timeout=30, preserve_bytes=False, overwrite=True,
    ):
        captured["remote"] = remote
        captured["bytes"] = local.read_bytes()
        captured["preserve_bytes"] = preserve_bytes
        captured["overwrite"] = overwrite
        return 0, "ok"

    monkeypatch.setattr(file_transfer, "scp_to", fake_scp)
    result = file_transfer.upload_file(
        "win", source, "nested/renamed.bin", config=FakeConfig(),
    )
    assert result["ok"] is True
    assert result["transport"] == "scp"
    assert captured == {
        "remote": "C:/transfer/nested/",
        "bytes": b"payload",
        "preserve_bytes": True,
        "overwrite": False,
    }


def test_safety_error_never_falls_back(monkeypatch, tmp_path):
    source = tmp_path / "source.bin"
    source.write_bytes(b"payload")
    FakeMcpAgent.calls = []
    FakeMcpAgent.ready = {"ok": True}
    FakeMcpAgent.responses = [{"ok": False, "error_code": "file_exists", "error": "exists"}]
    monkeypatch.setattr(file_transfer, "TargetSubAgent", FakeMcpAgent)
    monkeypatch.setattr(file_transfer, "scp_to", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not fallback")))
    result = file_transfer.upload_file("win", source, "a.bin", config=FakeConfig())
    assert result["error_code"] == "file_exists"


def test_download_falls_back_before_creating_staging_file(monkeypatch, tmp_path):
    FakeMcpAgent.calls = []
    FakeMcpAgent.ready = {"ok": False, "error": "MCP initialize failed: timeout"}
    monkeypatch.setattr(file_transfer, "TargetSubAgent", FakeMcpAgent)

    def fake_scp(ssh, remote, local, timeout=30):
        assert remote == "C:/transfer/results/out.bin"
        local.write_bytes(b"downloaded")
        return 0, "ok"

    monkeypatch.setattr(file_transfer, "scp_from", fake_scp)
    destination = tmp_path / "out.bin"
    result = file_transfer.download_file(
        "win", "results/out.bin", destination, config=FakeConfig(),
    )
    assert result["ok"] is True
    assert result["transport"] == "scp"
    assert destination.read_bytes() == b"downloaded"
