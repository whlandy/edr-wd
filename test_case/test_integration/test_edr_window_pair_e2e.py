"""
test_edr_window_pair_e2e.py — 基础集成 E2E：HiSec 入口窗口 + EDRClient 窗口。

只要运行 pytest 基础测试目录，这个用例就会出现。它按当前 MCP backend
分流到对应平台：

Windows:
  1. 打开 HisecEndpointAgent.exe 入口窗口
  2. 用窗口检测确认入口窗口在桌面上
  3. activate_edr 打开 EDRClient.exe
  4. 用窗口检测确认 EDRClient.exe 在桌面上
  5. 再次用 is_window_open 确认两个窗口都可见

macOS:
  1. activate_edr 使用 HiSecEndpointAgent cmd ui 打开主窗口
  2. Swift helper 点击“前往安全防护中心”打开 EDRClient
  3. 用窗口检测确认 HiSecEndpointAgent 和 EDRClient 都可见
"""

import pytest

from test_case.conftest import McpClient, is_server_online


WINDOWS_BACKEND = "windows_pywinauto"
MACOS_BACKEND = "macos_accessibility"

WINDOWS_HISEC_AGENT = "HisecEndpointAgent.exe"
WINDOWS_EDR_CLIENT = "EDRClient.exe"
MACOS_HISEC_AGENT = "HiSecEndpointAgent"
MACOS_EDR_CLIENT = "EDRClient"
MACOS_HISEC_TITLE_RE = "华为智能终端安全系统"
MACOS_EDR_TITLE_RE = "华为HiSec Endpoint"


@pytest.fixture(scope="module")
def client():
    c = McpClient()
    resp = c.initialize()
    assert "error" not in resp, f"initialize failed: {resp}"
    return c


@pytest.fixture(scope="module")
def backend_kind(client):
    status = client.call_tool("status")
    return status.get("backend_kind") or status.get("backend")


def _require_hisec_backend(backend_kind):
    if backend_kind not in {WINDOWS_BACKEND, MACOS_BACKEND}:
        pytest.skip(
            f"HiSec window-pair E2E requires {WINDOWS_BACKEND} or "
            f"{MACOS_BACKEND}, got {backend_kind!r}"
        )


def _wait_visible(client, process_name):
    return client.call_tool(
        "wait_window",
        {"process_name": process_name, "timeout": 15.0, "interval": 0.5},
    )


def _assert_visible(client, process_name, title_re=None):
    args = {"process_name": process_name}
    if title_re:
        args["title_re"] = title_re
    result = client.call_tool("is_window_open", args)
    label = f"{process_name} / {title_re}" if title_re else process_name
    assert result.get("ok") is True, f"is_window_open failed for {label}: {result}"
    assert result.get("found") is True, f"{label} desktop window not found: {result}"
    return result


def _run_windows_window_pair_e2e(client):
    launch = client.call_tool("activate_edr", {"wait": True, "timeout": 15.0})
    assert launch.get("ok") is True, f"activate_edr failed: {launch}"
    assert launch.get("main", {}).get("window_found") is True, (
        f"{WINDOWS_HISEC_AGENT} desktop window not found after activate_edr: {launch}"
    )

    hisec_wait = _wait_visible(client, WINDOWS_HISEC_AGENT)
    assert hisec_wait.get("ok") is not False, (
        f"wait_window failed for {WINDOWS_HISEC_AGENT}: {hisec_wait}"
    )
    assert hisec_wait.get("found") is True, (
        f"{WINDOWS_HISEC_AGENT} desktop window not found: {hisec_wait}"
    )

    edr_wait = _wait_visible(client, WINDOWS_EDR_CLIENT)
    assert edr_wait.get("ok") is not False, (
        f"wait_window failed for {WINDOWS_EDR_CLIENT}: {edr_wait}"
    )
    assert edr_wait.get("found") is True, (
        f"{WINDOWS_EDR_CLIENT} desktop window not found: {edr_wait}"
    )

    _assert_visible(client, WINDOWS_HISEC_AGENT)
    _assert_visible(client, WINDOWS_EDR_CLIENT)


def _run_macos_window_pair_e2e(client):
    activate = client.call_tool("activate_edr", {"wait": True, "timeout": 20.0})
    assert activate.get("ok") is True, f"activate_edr failed: {activate}"
    assert activate.get("main", {}).get("window_found") is True, (
        f"HiSecEndpointAgent main window not found: {activate}"
    )
    assert activate.get("client", {}).get("window_found") is True, (
        f"EDRClient window not found: {activate}"
    )

    _assert_visible(client, MACOS_HISEC_AGENT, MACOS_HISEC_TITLE_RE)
    _assert_visible(client, MACOS_EDR_CLIENT, MACOS_EDR_TITLE_RE)


@pytest.mark.skipif(not is_server_online(), reason="MCP server not reachable")
def test_e2e_hisec_agent_and_edrclient_windows_are_visible(client, backend_kind):
    """基础 E2E：打开并检测 HiSecEndpointAgent 和 EDRClient 两个桌面窗口。"""
    _require_hisec_backend(backend_kind)
    if backend_kind == MACOS_BACKEND:
        _run_macos_window_pair_e2e(client)
    else:
        _run_windows_window_pair_e2e(client)
