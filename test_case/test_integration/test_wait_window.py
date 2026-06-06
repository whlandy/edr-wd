"""
test_wait_window.py — 集成测试：wait_window MCP tool
"""

import pytest
from test_case.conftest import McpClient, is_server_online


@pytest.fixture(scope="module")
def client():
    c = McpClient()
    c.initialize()
    return c


@pytest.mark.skipif(not is_server_online(), reason="MCP server not reachable")
def test_wait_window_timeout(client):
    """不存在的窗口应在超时后返回 ok=False"""
    result = client.call_tool("wait_window", {
        "process_name": "nonexistent_process_xyz.exe",
        "timeout": 2.0,
        "interval": 0.3,
    })
    print(f"\n[wait_window timeout] {result}")
    assert result.get("ok") is False
    assert result.get("found") is False
    assert result.get("error") == "timeout"


@pytest.mark.skipif(not is_server_online(), reason="MCP server not reachable")
def test_wait_window_defaults(client):
    """默认超时10s，默认间隔0.5s，调用应返回结构完整的响应"""
    result = client.call_tool("wait_window", {"process_name": "nonexistent_xyz.exe"})
    assert result.get("ok") is False  # 超时
    assert "error" in result
    assert "windows" in result
    assert "count" in result
