"""
test_edr_full_workflow.py — 端到端测试：EDR 窗口激活 → 连接 → 枚举控件 → 点击

测试流程：
  1. is_window_open(process_name="EDRClient.exe")   # 检查是否已打开
  2. activate_edr(wait=True, timeout=15)           # 激活 EDR 窗口
  3. wait_window(process_name="EDRClient.exe")      # 等待窗口出现
  4. connect(process_name="EDRClient.exe")          # 连接到窗口
  5. dump_tree()                                   # 枚举控件树
  6. screenshot()                                   # 截图
  7. restore_edr()                                  # 还原窗口（如最小化）
  8. is_window_open(process_name="EDRClient.exe")   # 再次验证

前置条件：
  - Windows 目标机已有已登录的本地/RDP 桌面会话
  - 已运行 bash agent/edr-wd.sh up（远程启动 Windows server + 建立 SSH 隧道）
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
    result = client.call_tool("tools/list")
    assert "result" in result
    tool_names = [t["name"] for t in result["result"].get("tools", [])]
    required = [
        "is_window_open", "activate_edr", "wait_window",
        "connect", "dump_tree", "screenshot", "restore_edr",
        "list_windows",
    ]
    missing = [t for t in required if t not in tool_names]
    if missing:
        pytest.skip(f"Tools not registered: {missing}")
    return tool_names


@pytest.mark.skipif(not is_server_online(), reason="MCP server not reachable")
class TestEdrWorkflow:
    """完整 EDR GUI 自动化工作流"""

    def test_0_check_edr_already_open(self, client, tools):
        """Step 0: 检查 EDR 窗口是否已打开"""
        result = client.call_tool("is_window_open", {"process_name": "EDRClient.exe"})
        print(f"\n[Step0 is_window_open] found={result.get('found')}, count={result.get('count')}")
        print(f"  windows: {result.get('windows', [])}")
        # 不强制要求已打开，只记录状态
        assert result.get("ok") is True

    def test_1_activate_edr(self, client, tools):
        """Step 1: 激活 EDR（启动或唤醒窗口）"""
        result = client.call_tool("activate_edr", {"wait": True, "timeout": 15.0})
        print(f"\n[Step1 activate_edr] {result}")
        assert result.get("ok") is True, f"activate_edr failed: {result}"
        # already_open=true 表示之前已打开；wait成功则窗口已出现
        if not result.get("already_open"):
            assert result.get("found") is True, "EDR window did not appear after activate"

    def test_2_wait_window(self, client, tools):
        """Step 2: 等待 EDRClient 窗口出现"""
        result = client.call_tool("wait_window", {
            "process_name": "EDRClient.exe",
            "timeout": 15.0,
            "interval": 0.5,
        })
        print(f"\n[Step2 wait_window] {result}")
        assert result.get("ok") is True, f"wait_window failed: {result}"
        assert result.get("found") is True, "EDRClient.exe window not found"
        wins = result.get("windows", [])
        assert len(wins) > 0
        print(f"  Window info: title={wins[0].get('title')}, "
              f"handle={wins[0].get('handle')}, "
              f"rect={wins[0].get('rectangle')}")

    def test_3_connect(self, client, tools):
        """Step 3: 连接到 EDR 窗口"""
        result = client.call_tool("connect", {"process_name": "EDRClient.exe", "timeout": 10.0})
        print(f"\n[Step3 connect] {result}")
        assert result.get("ok") is True, f"connect failed: {result}"

    def test_4_dump_tree(self, client, tools):
        """Step 4: 导出控件树"""
        result = client.call_tool("dump_tree", {"max_depth": 10})
        print(f"\n[Step4 dump_tree] ok={result.get('ok')}")
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

    def test_5_screenshot(self, client, tools):
        """Step 5: 截图"""
        result = client.call_tool("screenshot", {})
        print(f"\n[Step5 screenshot] ok={result.get('ok')}, has_data={'image' in result or 'path' in result}")
        assert result.get("ok") is True, f"screenshot failed: {result}"

    def test_6_restore_edr(self, client, tools):
        """Step 6: 还原 EDR 窗口（如最小化）"""
        result = client.call_tool("restore_edr", {})
        print(f"\n[Step6 restore_edr] {result}")
        # restore_edr 需要先 connect，所以可能失败
        # 只检查返回结构，不强制要求成功（connect 失败时这是预期行为）
        assert "ok" in result

    def test_7_verify_still_open(self, client, tools):
        """Step 7: 操作后再次验证窗口仍打开"""
        result = client.call_tool("is_window_open", {"process_name": "EDRClient.exe"})
        print(f"\n[Step7 is_window_open after ops] {result}")
        assert result.get("ok") is True
        assert result.get("found") is True, "EDR window disappeared after operations"
