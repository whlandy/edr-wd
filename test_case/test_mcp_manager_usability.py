"""Regression tests for script-free MCP connection and result handling."""

import io

from agent import mcp_manager


def test_initialize_repairs_stale_tunnel_once(monkeypatch):
    attempts = []

    class FakeTargetConfig:
        def get_default_target(self):
            return "win-dev"

        def has_target(self, name):
            return name == "win-dev"

        def list_targets(self):
            return {"win-dev": {}}

        def build_mcp_url(self, _name):
            return "http://127.0.0.1:18765/mcp"

        def get_target(self, _name):
            return {"mcp": {"connect_mode": "tunnel"}}

    def fake_initialize(_url):
        attempts.append(True)
        if len(attempts) == 1:
            return False, None, "connection reset"
        return True, "session-1", None

    monkeypatch.setattr(mcp_manager, "TargetConfig", FakeTargetConfig)
    monkeypatch.setattr(mcp_manager, "_mcp_initialize", fake_initialize)
    monkeypatch.setattr(
        "agent.tunnel.ensure_tunnel",
        lambda _target: {"ok": True, "status": "started"},
    )

    result = mcp_manager.initialize("win-dev")

    assert result["ok"] is True
    assert result["data"]["session_id"] == "session-1"
    assert result["data"]["tunnel_repaired"] is True
    assert len(attempts) == 2


def test_initialize_does_not_repair_direct_connection(monkeypatch):
    class FakeTargetConfig:
        def has_target(self, _name):
            return True

        def list_targets(self):
            return {"direct": {}}

        def build_mcp_url(self, _name):
            return "http://192.0.2.10:8765/mcp"

        def get_target(self, _name):
            return {"mcp": {"connect_mode": "direct"}}

    monkeypatch.setattr(mcp_manager, "TargetConfig", FakeTargetConfig)
    monkeypatch.setattr(
        mcp_manager, "_mcp_initialize", lambda _url: (False, None, "offline")
    )

    result = mcp_manager.initialize("direct")

    assert result["ok"] is False


def test_mcp_initialize_converts_connection_reset_to_structured_error(monkeypatch):
    monkeypatch.setattr(
        mcp_manager,
        "_post_sse",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ConnectionResetError("peer reset")
        ),
    )

    ok, session, error = mcp_manager._mcp_initialize("http://127.0.0.1:1/mcp")

    assert ok is False
    assert session is None
    assert "ConnectionResetError" in error


def test_unwrap_tool_result_parses_fastmcp_text_json():
    raw = {
        "ok": True,
        "data": {
            "result": {
                "content": [{"type": "text", "text": '{"ok": true, "found": true}'}]
            }
        },
    }
    assert mcp_manager.unwrap_tool_result(raw) == {"ok": True, "found": True}


def test_unwrap_tools_list_returns_explicit_tools_array():
    tools = [{"name": "activate_edr"}, {"name": "screenshot"}]
    raw = {"ok": True, "data": {"result": {"tools": tools}}}
    assert mcp_manager.unwrap_tools_list(raw) == {"ok": True, "tools": tools}


def test_sse_reader_reads_large_data_by_line_not_single_bytes():
    class CountingResponse:
        def __init__(self, payload):
            self._stream = io.BytesIO(payload)
            self.readline_calls = 0

        def readline(self):
            self.readline_calls += 1
            return self._stream.readline()

        def read(self, _size=-1):
            raise AssertionError("SSE reader must not use single-byte read()")

    image = "A" * 300_000
    payload = f'data: {{"id":1,"result":{{"image_b64":"{image}"}}}}\r\n\r\n'.encode()
    response = CountingResponse(payload)

    raw = mcp_manager._read_all_sse_data(response)

    assert len(raw) > 300_000
    assert response.readline_calls == 2
