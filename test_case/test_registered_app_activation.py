"""Windows generic launch accepts only a locally registered executable."""

from __future__ import annotations

import sys
import types

from target.automation.windows_pywinauto import WindowsPywinautoBackend


def _backend(windows=()):
    backend = WindowsPywinautoBackend.__new__(WindowsPywinautoBackend)
    backend._enumerate_top_windows_windows = lambda: list(windows)
    return backend


def test_registered_app_launch_and_invalid_identity():
    launched = []
    backend = _backend()
    result = backend.activate_registered_app(
        "Demo.exe", _resolve=lambda _: "/tmp/Demo.exe", _launch=launched.append,
    )
    assert result["ok"] and launched == ["/tmp/Demo.exe"]
    assert backend.activate_registered_app("/tmp/Demo.exe")["code"] == "registered_app_invalid"
    assert backend.activate_registered_app("Missing.exe", _resolve=lambda _: None)["code"] == "registered_app_not_found"


def test_registered_app_focuses_one_existing_window(monkeypatch):
    focused = []

    class Window:
        def set_focus(self):
            focused.append(True)

    module = types.ModuleType("pywinauto")
    module.Desktop = lambda backend: types.SimpleNamespace(window=lambda handle: Window())
    monkeypatch.setitem(sys.modules, "pywinauto", module)
    backend = _backend([{"handle": 8, "process_name": "Demo.exe"}])
    result = backend.activate_registered_app(
        "Demo.exe", _resolve=lambda _: "/tmp/Demo.exe",
        _launch=lambda _: raise_unexpected_launch(),
    )
    assert result["method"] == "focus_registered" and focused == [True]


def raise_unexpected_launch():
    raise AssertionError("an already running application must not be launched")
