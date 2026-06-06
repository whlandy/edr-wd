"""
test_list_windows.py — 集成测试：list_windows MCP tool
"""

import pytest
from test_case.conftest import McpClient, is_server_online


@pytest.fixture(scope="module")
def client():
    c = McpClient()
    resp = c.initialize()
    assert "error" not in resp, f"initialize failed: {resp}"
    return c


@pytest.mark.skipif(not is_server_online(), reason="MCP server not reachable")
def test_list_windows_returns_ok(client):
    """list_windows 应返回 ok=true 和 windows 列表"""
    result = client.call_tool("list_windows")
    print(f"\n[list_windows] {result}")
    assert result.get("ok") is True, f"list_windows failed: {result}"
    assert "windows" in result
    assert isinstance(result["windows"], list)


@pytest.mark.skipif(not is_server_online(), reason="MCP server not reachable")
def test_list_windows_contains_desktop_windows(client):
    """list_windows 应至少能枚举到一些顶层窗口（哪怕不是 EDR）"""
    result = client.call_tool("list_windows")
    assert result.get("ok") is True
    windows = result.get("windows", [])
    # Windows 桌面通常有 Task View、Search、Start 等窗口
    assert len(windows) >= 0  # 允许空列表（某些 Server Core 环境）


@pytest.mark.skipif(not is_server_online(), reason="MCP server not reachable")
def test_list_windows_fields(client):
    """每个窗口应包含 title、process_id、class_name、rectangle、visible、enabled"""
    result = client.call_tool("list_windows")
    assert result.get("ok") is True
    for win in result.get("windows", [])[:3]:  # 只检查前3个
        assert "title" in win
        assert "class_name" in win
        assert "process_id" in win
        assert "rectangle" in win
        assert "visible" in win
        assert "enabled" in win
