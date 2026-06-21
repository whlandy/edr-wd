"""Click context must distinguish HiSecEndpointAgent from EDRClient."""

import ast
from pathlib import Path

from target.automation.macos_accessibility import MacOSAccessibilityBackend
from target.automation.windows_pywinauto import WindowsPywinautoBackend


def test_windows_click_context_rejects_wrong_hisec_process():
    backend = object.__new__(WindowsPywinautoBackend)
    backend._connected_window_state = lambda: {
        "ok": True,
        "process_name": "EDRClient.exe",
    }

    result = backend._ensure_click_process("HisecEndpointAgent.exe")

    assert result["ok"] is False
    assert result["code"] == "click_context_mismatch"


def test_windows_click_context_accepts_exe_optional_suffix():
    backend = object.__new__(WindowsPywinautoBackend)
    backend._connected_window_state = lambda: {
        "ok": True,
        "process_name": "HisecEndpointAgent.exe",
    }

    assert backend._ensure_click_process("HisecEndpointAgent") is None


def test_windows_hisec_click_requires_explicit_context():
    backend = object.__new__(WindowsPywinautoBackend)
    backend._connected_window_state = lambda: {
        "ok": True,
        "process_name": "EDRClient.exe",
    }

    result = backend._ensure_click_process(None)

    assert result["code"] == "click_context_required"


def test_macos_click_context_rejects_wrong_hisec_process():
    backend = object.__new__(MacOSAccessibilityBackend)
    backend._connected_process_name = lambda: "EDRClient"

    result = backend._ensure_click_process("HiSecEndpointAgent")

    assert result["ok"] is False
    assert result["code"] == "click_context_mismatch"


def test_macos_click_context_accepts_edrclient():
    backend = object.__new__(MacOSAccessibilityBackend)
    backend._connected_process_name = lambda: "EDRClient"

    assert backend._ensure_click_process("EDRClient") is None


def test_macos_hisec_click_requires_explicit_context():
    backend = object.__new__(MacOSAccessibilityBackend)
    backend._connected_process_name = lambda: "HiSecEndpointAgent"

    result = backend._ensure_click_process(None)

    assert result["code"] == "click_context_required"


def test_all_click_mcp_tools_expose_process_context():
    server_path = Path(__file__).resolve().parents[1] / "server.py"
    tree = ast.parse(server_path.read_text(encoding="utf-8"))
    functions = {
        node.name: {arg.arg for arg in node.args.args}
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
    }
    for tool_name in (
        "click",
        "click_target",
        "click_at",
        "click_window_at",
        "double_click_at",
        "right_click_at",
        "middle_click_at",
    ):
        assert "expected_process_name" in functions[tool_name]
