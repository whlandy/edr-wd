"""
test_edr_window_pair_e2e.py — 基础集成 E2E：入口窗口 + EDRClient 窗口。

只要运行 pytest 基础测试目录，这个用例就会出现。它通过 MCP 依次：
  1. 打开 HisecEndpointAgent.exe 入口窗口
  2. 用窗口检测确认入口窗口在桌面上
  3. activate_edr 打开 EDRClient.exe
  4. 用窗口检测确认 EDRClient.exe 在桌面上
  5. 再次用 is_window_open 确认两个窗口都可见
"""

import pytest

from test_case.conftest import McpClient, is_server_online


HISEC_AGENT = "HisecEndpointAgent.exe"
EDR_CLIENT = "EDRClient.exe"


@pytest.fixture(scope="module")
def client():
    c = McpClient()
    resp = c.initialize()
    assert "error" not in resp, f"initialize failed: {resp}"
    return c


def _require_windows_backend(client):
    status = client.call_tool("status")
    backend_kind = status.get("backend_kind") or status.get("backend")
    if backend_kind != "windows_pywinauto":
        pytest.skip(f"EDR window-pair E2E requires windows_pywinauto, got {backend_kind!r}")


def _open_hisec_agent(client):
    command = (
        "$p = 'C:\\Program Files\\HiSec-Endpoint\\core\\safra\\HisecEndpointAgent.exe'; "
        "if (-not (Test-Path $p)) { throw \"HisecEndpointAgent.exe not found: $p\" }; "
        "Start-Process -FilePath $p -ArgumentList @('cmd','ui'); "
        "Write-Output 'started'"
    )
    return client.call_tool("run_powershell", {"command": command, "timeout": 10})


def _wait_visible(client, process_name):
    return client.call_tool(
        "wait_window",
        {"process_name": process_name, "timeout": 15.0, "interval": 0.5},
    )


def _assert_visible(client, process_name):
    result = client.call_tool("is_window_open", {"process_name": process_name})
    assert result.get("ok") is True, f"is_window_open failed for {process_name}: {result}"
    assert result.get("found") is True, f"{process_name} desktop window not found: {result}"
    return result


@pytest.mark.skipif(not is_server_online(), reason="MCP server not reachable")
def test_e2e_hisec_agent_and_edrclient_windows_are_visible(client):
    """基础 E2E：打开并检测 HisecEndpointAgent 和 EDRClient 两个桌面窗口。"""
    _require_windows_backend(client)

    launch = _open_hisec_agent(client)
    assert launch.get("ok") is True, f"open {HISEC_AGENT} failed: {launch}"

    hisec_wait = _wait_visible(client, HISEC_AGENT)
    assert hisec_wait.get("ok") is not False, f"wait_window failed for {HISEC_AGENT}: {hisec_wait}"
    assert hisec_wait.get("found") is True, f"{HISEC_AGENT} desktop window not found: {hisec_wait}"

    activate = client.call_tool("activate_edr", {"wait": True, "timeout": 15.0})
    assert activate.get("ok") is True, f"activate_edr failed: {activate}"

    edr_wait = _wait_visible(client, EDR_CLIENT)
    assert edr_wait.get("ok") is not False, f"wait_window failed for {EDR_CLIENT}: {edr_wait}"
    assert edr_wait.get("found") is True, f"{EDR_CLIENT} desktop window not found: {edr_wait}"

    _assert_visible(client, HISEC_AGENT)
    _assert_visible(client, EDR_CLIENT)
