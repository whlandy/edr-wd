"""Audit every public pointer MCP route and backend injection boundary."""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

from target import product_policy, server
from target.automation.macos_accessibility import MacOSAccessibilityBackend
from target.automation.windows_pywinauto import WindowsPywinautoBackend

def _windows_gui_method_source(name: str) -> str:
    source = (Path(__file__).resolve().parents[1] / "target" / "pywinauto_client.py").read_text()
    tree = ast.parse(source)
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef)
               and node.name == "WindowsGUI")
    method = next(node for node in cls.body if isinstance(node, ast.FunctionDef)
                  and node.name == name)
    return ast.get_source_segment(source, method) or ""


POINTER_TOOLS = (
    "click", "click_target", "click_at", "double_click_at", "right_click_at",
    "middle_click_at", "hover_at", "drag", "scroll", "scroll_window",
)


def test_all_public_pointer_tools_are_fenced_by_run_policy():
    for name in POINTER_TOOLS:
        assert name in product_policy.GUI_MUTATION_METHODS
        source = inspect.getsource(getattr(server, name))
        assert f"_backend.{name}(" in source
    assert "GuardedBackend(_backend, _backend_kind)" in inspect.getsource(server)


def test_windows_pointer_injection_routes_reach_native_guard():
    for name in ("click_at", "double_click_at", "right_click_at", "middle_click_at",
                 "hover_at", "drag", "scroll", "scroll_window", "click_window_at"):
        assert "_guarded_pointer(" in inspect.getsource(getattr(WindowsPywinautoBackend, name))
    for name in ("click", "click_target"):
        assert "pointer_guard" in _windows_gui_method_source(name)
    assert "self._guarded_pointer(" in inspect.getsource(WindowsPywinautoBackend.__init__)


def test_macos_real_pointer_routes_reach_native_guard():
    for name in ("click_at", "double_click_at", "right_click_at", "middle_click_at",
                 "hover_at", "drag", "scroll"):
        assert "_guarded_pointer(" in inspect.getsource(getattr(MacOSAccessibilityBackend, name))
    for name in ("click", "click_target"):
        assert "_click_control_center(" in inspect.getsource(getattr(MacOSAccessibilityBackend, name))
    assert "self.click_at(" in inspect.getsource(MacOSAccessibilityBackend._click_control_center)
    assert "self.click_at(" in inspect.getsource(MacOSAccessibilityBackend.click_window_at)
