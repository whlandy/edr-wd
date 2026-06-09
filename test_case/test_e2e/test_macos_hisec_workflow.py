"""
test_macos_hisec_workflow.py — macOS HiSecEndpoint E2E window-pair test.

This is the pytest E2E counterpart to the macos_hisec profile runner
(`test_case/run_macos_hisec.py`). It keeps the scope narrow and stable:

  1. initialize MCP session
  2. verify required tools through MCP tools/list
  3. require backend_kind/backend == macos_accessibility
  4. activate_edr(wait=True, timeout=20)
  5. assert HiSecEndpointAgent and EDRClient windows are visible
  6. re-check both windows through is_window_open

Do not add dump_tree/click/restore/screenshot here; those are covered by the
profile runner and are more sensitive to macOS GUI permissions.
"""

import pytest
from test_case.conftest import McpClient, is_server_online


HISEC_MAIN_TITLE_RE = "华为智能终端安全系统"
EDR_CLIENT_TITLE_RE = "华为HiSec Endpoint"
MACOS_BACKEND = "macos_accessibility"


@pytest.fixture(scope="module")
def client():
    c = McpClient()
    resp = c.initialize()
    assert "error" not in resp, f"initialize failed: {resp}"
    return c


def _tool_names(result: dict) -> list[str]:
    return [t["name"] for t in result.get("result", {}).get("tools", [])]


@pytest.fixture(scope="module")
def tools(client):
    """验证所需工具均已注册"""
    result = client.tools_list()
    assert "result" in result
    tool_names = _tool_names(result)
    required = [
        "activate_edr",
        "is_window_open",
        "list_windows",
        "status",
    ]
    missing = [t for t in required if t not in tool_names]
    if missing:
        pytest.skip(f"Tools not registered: {missing}")
    return tool_names


@pytest.fixture(scope="module")
def macos_backend(client, tools):
    """Skip unless the active MCP server is the macOS Accessibility backend."""
    status = client.call_tool("status", {})
    backend = status.get("backend_kind") or status.get("backend")
    if backend != MACOS_BACKEND:
        pytest.skip(f"macOS HiSec E2E requires {MACOS_BACKEND}, got {backend!r}")
    return status


@pytest.mark.skipif(not is_server_online(), reason="MCP server not reachable")
class TestMacosHisecWorkflow:
    """macOS HiSecEndpoint E2E: 弹出 EDRClient + HisecEndpointAgent 窗口"""

    def test_0_initialize(self, client):
        """Step 0: MCP initialize"""
        result = client.initialize()
        print(f"\n[Step0 initialize] {result}")
        assert "error" not in result, f"initialize failed: {result}"

    def test_1_backend_guard(self, macos_backend):
        """Step 1: 确认当前 MCP server 是 macOS Accessibility backend"""
        backend = macos_backend.get("backend_kind") or macos_backend.get("backend")
        print(f"\n[Step1 backend] {backend}")
        assert backend == MACOS_BACKEND

    def test_2_activate_edr(self, client, macos_backend):
        """Step 1: 激活 HiSecEndpoint（弹出主窗口 + EDRClient 子窗口）"""
        result = client.call_tool("activate_edr", {"wait": True, "timeout": 20.0})
        print(f"\n[Step2 activate_edr] ok={result.get('ok')}")
        print(f"  main window_found={result.get('main', {}).get('window_found')}")
        print(f"  client window_found={result.get('client', {}).get('window_found')}")
        print(f"  stage={result.get('stage')}")
        if result.get("main", {}).get("window_title"):
            print(f"  main title={result.get('main', {}).get('window_title')!r}")
        if result.get("client", {}).get("window_title"):
            print(f"  client title={result.get('client', {}).get('window_title')!r}")

        assert result.get("ok") is True, f"activate_edr failed: {result}"
        assert result.get("main", {}).get("window_found") is True, \
            f"HiSecEndpointAgent main window not found: {result}"
        assert result.get("client", {}).get("window_found") is True, \
            f"EDRClient sub-window not found: {result}"

    def test_3_verify_hisec_agent_window(self, client, macos_backend):
        """Step 2: 确认 HisecEndpointAgent 主窗口在桌面上"""
        result = client.call_tool("is_window_open", {
            "title_re": HISEC_MAIN_TITLE_RE,
        })
        print(f"\n[Step3 HisecEndpointAgent window] found={result.get('found')}")
        print(f"  windows: {result.get('windows', [])}")
        assert result.get("ok") is True, f"is_window_open failed: {result}"
        assert result.get("found") is True, \
            f"HisecEndpointAgent main window not on desktop: {result}"

    def test_4_verify_edr_client_window(self, client, macos_backend):
        """Step 3: 确认 EDRClient 子窗口在桌面上"""
        result = client.call_tool("is_window_open", {
            "title_re": EDR_CLIENT_TITLE_RE,
        })
        print(f"\n[Step4 EDRClient window] found={result.get('found')}")
        print(f"  windows: {result.get('windows', [])}")
        assert result.get("ok") is True, f"is_window_open failed: {result}"
        assert result.get("found") is True, \
            f"EDRClient sub-window not on desktop: {result}"
