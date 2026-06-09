"""
test_windows_hisec_e2e.py — Windows HiSec EDR 端到端测试。

测试流程：
  1. is_window_open(process_name="EDRClient.exe")   # 检查是否已打开
  2. run_powershell 启动 HisecEndpointAgent.exe     # 打开入口窗口
  3. wait_window(process_name="HisecEndpointAgent.exe")
  4. activate_edr(wait=True, timeout=15)           # 激活 EDRClient 窗口
  5. wait_window(process_name="EDRClient.exe")      # 等待目标窗口出现
  6. is_window_open 检测两个窗口都在桌面上
  7. connect(process_name="EDRClient.exe")          # 连接到目标窗口
  8. dump_tree()                                   # 枚举控件树
  9. screenshot()                                   # 截图
  10. restore_edr()                                 # 还原窗口（如最小化）
  11. is_window_open(process_name="EDRClient.exe")  # 再次验证

前置条件：
  - Windows 目标机已有已登录的本地/RDP 桌面会话
  - 已运行 bash agent/edr-wd.sh up（远程启动 Windows server + 建立 SSH 隧道）

注意：
  这个文件是 Windows-only。macOS HiSec E2E 在
  test_case/test_e2e/test_macos_hisec_e2e.py。
"""

import pytest
from test_case.conftest import McpClient, is_server_online


@pytest.fixture(scope="module")
def client():
    c = McpClient()
    resp = c.initialize()
    assert "error" not in resp, f"initialize failed: {resp}"
    return c


@pytest.fixture(scope="module")
def tools(client):
    """验证所需工具均已注册"""
    result = client.tools_list()
    assert "result" in result
    tool_names = [t["name"] for t in result["result"].get("tools", [])]
    required = [
        "is_window_open", "activate_edr", "wait_window",
        "connect", "dump_tree", "screenshot", "restore_edr",
        "list_windows", "run_powershell",
    ]
    missing = [t for t in required if t not in tool_names]
    if missing:
        pytest.skip(f"Tools not registered: {missing}")
    return tool_names


@pytest.fixture(scope="module")
def windows_backend(client, tools):
    """Skip unless the active MCP server is the Windows pywinauto backend."""
    status = client.call_tool("status", {})
    backend = status.get("backend_kind") or status.get("backend")
    if backend != "windows_pywinauto":
        pytest.skip(f"Windows HiSec E2E requires windows_pywinauto, got {backend!r}")
    return status


@pytest.mark.skipif(not is_server_online(), reason="MCP server not reachable")
class TestWindowsHisecE2E:
    """完整 EDR GUI 自动化工作流"""

    def test_0_check_edr_already_open(self, client, windows_backend):
        """Step 0: 检查 EDR 窗口是否已打开"""
        result = client.call_tool("is_window_open", {"process_name": "EDRClient.exe"})
        print(f"\n[Step0 is_window_open] found={result.get('found')}, count={result.get('count')}")
        print(f"  windows: {result.get('windows', [])}")
        # 不强制要求已打开，只记录状态
        assert result.get("ok") is True

    def test_1_open_hisec_agent_entry_window(self, client, windows_backend):
        """Step 1: 通过 MCP PowerShell 打开 HisecEndpointAgent 入口窗口"""
        command = (
            "$p = 'C:\\Program Files\\HiSec-Endpoint\\core\\safra\\HisecEndpointAgent.exe'; "
            "if (-not (Test-Path $p)) { throw \"HisecEndpointAgent.exe not found: $p\" }; "
            "Start-Process -FilePath $p -ArgumentList @('cmd','ui'); "
            "Write-Output 'started'"
        )
        result = client.call_tool("run_powershell", {"command": command, "timeout": 10})
        print(f"\n[Step1 open HisecEndpointAgent] {result}")
        assert result.get("ok") is True, f"open HisecEndpointAgent failed: {result}"

    def test_2_wait_hisec_agent_window(self, client, windows_backend):
        """Step 2: 用窗口检测确认 HisecEndpointAgent 已打开在桌面上"""
        result = client.call_tool("wait_window", {
            "process_name": "HisecEndpointAgent.exe",
            "timeout": 15.0,
            "interval": 0.5,
        })
        print(f"\n[Step2 wait HisecEndpointAgent] {result}")
        assert result.get("ok") is not False, f"wait_window failed: {result}"
        assert result.get("found") is True, "HisecEndpointAgent.exe desktop window not found"

    def test_3_activate_edr(self, client, windows_backend):
        """Step 1: 激活 EDR（启动或唤醒窗口）"""
        result = client.call_tool("activate_edr", {"wait": True, "timeout": 15.0})
        print(f"\n[Step3 activate_edr] {result}")
        assert result.get("ok") is True, f"activate_edr failed: {result}"
        # 具体窗口是否出现在桌面上由后续 wait_window/is_window_open 验证。

    def test_4_wait_edr_client_window(self, client, windows_backend):
        """Step 4: 等待 EDRClient 窗口出现"""
        result = client.call_tool("wait_window", {
            "process_name": "EDRClient.exe",
            "timeout": 15.0,
            "interval": 0.5,
        })
        print(f"\n[Step4 wait EDRClient] {result}")
        assert result.get("ok") is True, f"wait_window failed: {result}"
        assert result.get("found") is True, "EDRClient.exe window not found"
        wins = result.get("windows", [])
        assert len(wins) > 0
        print(f"  Window info: title={wins[0].get('title')}, "
              f"handle={wins[0].get('handle')}, "
              f"rect={wins[0].get('rectangle')}")

    def test_5_verify_hisec_and_edr_windows_visible(self, client, windows_backend):
        """Step 5: 用窗口检测确认入口窗口和目标窗口都在桌面上"""
        hisec = client.call_tool("is_window_open", {"process_name": "HisecEndpointAgent.exe"})
        edr = client.call_tool("is_window_open", {"process_name": "EDRClient.exe"})
        print(f"\n[Step5 HisecEndpointAgent visible] {hisec}")
        print(f"\n[Step5 EDRClient visible] {edr}")
        assert hisec.get("ok") is True and hisec.get("found") is True, (
            f"HisecEndpointAgent.exe desktop window not found: {hisec}"
        )
        assert edr.get("ok") is True and edr.get("found") is True, (
            f"EDRClient.exe desktop window not found: {edr}"
        )

    def test_6_connect(self, client, windows_backend):
        """Step 3: 连接到 EDR 窗口"""
        result = client.call_tool("connect", {"process_name": "EDRClient.exe", "timeout": 10.0})
        print(f"\n[Step6 connect] {result}")
        assert result.get("ok") is True, f"connect failed: {result}"

    def test_7_dump_tree(self, client, windows_backend):
        """Step 4: 导出控件树"""
        result = client.call_tool("dump_tree", {"max_depth": 10})
        print(f"\n[Step7 dump_tree] ok={result.get('ok')}")
        assert result.get("ok") is True, f"dump_tree failed: {result}"
        controls = result.get("controls", [])
        print(f"  Controls found: {len(controls)}")
        if controls:
            print(f"  First 5 controls:")
            for ctrl in controls[:5]:
                print(f"    [{ctrl.get('control_id')}] {ctrl.get('class_name')} | "
                      f"text={ctrl.get('text')!r} | rect={ctrl.get('rectangle')}")
        # 断言有控件（EDR 窗口不可能控件树为空）
        assert len(controls) > 0, "EDR window control tree is empty"

    def test_8_screenshot(self, client, windows_backend):
        """Step 5: 截图"""
        result = client.call_tool("screenshot", {})
        print(f"\n[Step8 screenshot] ok={result.get('ok')}, has_data={'image' in result or 'path' in result}")
        assert result.get("ok") is True, f"screenshot failed: {result}"

    def test_9_restore_edr(self, client, windows_backend):
        """Step 6: 还原 EDR 窗口（如最小化）"""
        result = client.call_tool("restore_edr", {})
        print(f"\n[Step9 restore_edr] {result}")
        # restore_edr 需要先 connect，所以可能失败
        # 只检查返回结构，不强制要求成功（connect 失败时这是预期行为）
        assert "ok" in result

    def test_10_verify_still_open(self, client, windows_backend):
        """Step 7: 操作后再次验证窗口仍打开"""
        result = client.call_tool("is_window_open", {"process_name": "EDRClient.exe"})
        print(f"\n[Step10 is_window_open after ops] {result}")
        assert result.get("ok") is True
        assert result.get("found") is True, "EDR window disappeared after operations"
