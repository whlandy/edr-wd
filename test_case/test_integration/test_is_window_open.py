"""
test_is_window_open.py — 集成测试：is_window_open MCP tool
"""

import pytest
from test_case.conftest import McpClient, is_server_online


@pytest.fixture(scope="module")
def client():
    c = McpClient()
    c.initialize()
    return c


@pytest.mark.skipif(not is_server_online(), reason="MCP server not reachable")
def test_is_window_open_by_title_re(client):
    """用宽泛标题正则查找窗口"""
    result = client.call_tool("is_window_open", {"title_re": r".*"})
    print(f"\n[is_window_open by title_re=.*] {result}")
    # .* 应匹配所有有标题的窗口
    assert result.get("ok") is True


@pytest.mark.skipif(not is_server_online(), reason="MCP server not reachable")
def test_is_window_open_by_process_name(client):
    """用进程名精确过滤（Explorer 几乎总在）"""
    result = client.call_tool("is_window_open", {"process_name": "explorer.exe"})
    print(f"\n[is_window_open by process_name=explorer.exe] {result}")
    assert result.get("ok") is True
    assert "found" in result
    assert "windows" in result


@pytest.mark.skipif(not is_server_online(), reason="MCP server not reachable")
def test_is_window_open_nonexistent(client):
    """不存在的进程名应返回 found=false"""
    result = client.call_tool("is_window_open", {"process_name": "nonexistent_process_xyz.exe"})
    print(f"\n[is_window_open nonexistent] {result}")
    assert result.get("ok") is True
    assert result.get("found") is False
    assert result.get("count") == 0


@pytest.mark.skipif(not is_server_online(), reason="MCP server not reachable")
def test_is_window_open_requires_filter(client):
    """不提供任何过滤条件应返回错误"""
    result = client.call_tool("is_window_open", {})
    print(f"\n[is_window_open no filter] {result}")
    assert result.get("ok") is False
    assert "error" in result
