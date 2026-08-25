# -*- coding: utf-8 -*-
"""Window capture must not depend on a live UIA round-trip.

When a Windows session stops rendering (RDP disconnected, no console attached)
every UIA property read fails with EVENT_E_ALL_SUBSCRIBERS_FAILED. The GDI
`PrintWindow(PW_RENDERFULLCONTENT)` path exists to keep capturing in exactly
that state, so it must resolve the HWND and the window rectangle through Win32
rather than through the wrapper that has just stopped answering.
"""

import sys as _sys
import types as _types

import pytest

# pywinauto installs on macOS but cannot expose its Windows-only API, so the
# module under test is unimportable in the dev venv without these stubs.
_pwa = _sys.modules.get("pywinauto")
if _pwa is None or not hasattr(_pwa, "Application"):
    _pwa = _types.ModuleType("pywinauto")
    _pwa.Application = object
    _pwa.timings = _types.SimpleNamespace()
    _pwa.mouse = _types.SimpleNamespace(click=lambda **kwargs: None)
    _pwa.Desktop = object
    _sys.modules["pywinauto"] = _pwa
    _sys.modules["pywinauto.mouse"] = _pwa.mouse

from pywinauto_client import WindowsGUI

pytestmark = pytest.mark.unit


class _DeadWrapper:
    """A UIA wrapper in a session that stopped rendering: everything raises."""

    COM_ERROR = "(-2147220991, '事件无法调用任何订户', (None, None, None, 0, None))"

    @property
    def handle(self):
        raise OSError(self.COM_ERROR)

    def capture_as_image(self):
        raise OSError(self.COM_ERROR)

    def rectangle(self):
        raise OSError(self.COM_ERROR)


def _gui(hwnd=None, main_window=None):
    gui = WindowsGUI.__new__(WindowsGUI)
    gui.backend = "uia"
    gui.app = None
    gui.main_window = main_window
    gui._connected_hwnd = hwnd
    return gui


def test_the_cached_handle_is_used_when_the_wrapper_stops_answering():
    gui = _gui(hwnd=1639286, main_window=_DeadWrapper())
    assert gui.window_handle() == 1639286


def test_without_a_cache_the_handle_still_comes_from_the_wrapper():
    class _Live:
        handle = 4242

    assert _gui(main_window=_Live()).window_handle() == 4242


def test_a_dead_wrapper_without_a_cached_handle_yields_no_handle():
    assert _gui(main_window=_DeadWrapper()).window_handle() is None


def test_capture_geometry_prefers_win32_over_the_wrapper_rectangle(monkeypatch):
    gui = _gui(hwnd=1639286, main_window=_DeadWrapper())
    monkeypatch.setattr(
        WindowsGUI, "window_rect_win32",
        staticmethod(lambda hwnd: (105, 107, 1027, 709)),
    )

    hwnd, left, top, width, height = gui._capture_geometry()

    assert (hwnd, left, top) == (1639286, 105, 107)
    assert (width, height) == (922, 602)


def test_capture_geometry_refuses_a_degenerate_rectangle(monkeypatch):
    gui = _gui(hwnd=1, main_window=_DeadWrapper())
    monkeypatch.setattr(
        WindowsGUI, "window_rect_win32", staticmethod(lambda hwnd: (0, 0, 0, 0)),
    )
    with pytest.raises(RuntimeError, match="invalid window rectangle"):
        gui._capture_geometry()


def test_capture_geometry_without_any_handle_is_an_explicit_failure():
    gui = _gui(main_window=_DeadWrapper())
    with pytest.raises(RuntimeError, match="window handle unavailable"):
        gui._capture_geometry()


def test_screenshot_falls_back_to_the_background_path_and_reports_both_errors(monkeypatch):
    gui = _gui(hwnd=1639286, main_window=_DeadWrapper())
    monkeypatch.setattr(
        WindowsGUI, "window_rect_win32",
        staticmethod(lambda hwnd: (105, 107, 1027, 709)),
    )

    class _Image:
        width, height = 922, 602

        def save(self, buffer, format=None):
            buffer.write(b"\x89PNG\r\n\x1a\n")

    monkeypatch.setattr(WindowsGUI, "_capture_window_with_gdi", lambda self, win=None: _Image())

    result = gui.screenshot()

    assert result["ok"] is True
    assert result["capture_scope"] == "window"
    assert result["origin"] == [105, 107]


def test_screenshot_names_every_failed_path_when_all_of_them_fail(monkeypatch):
    gui = _gui(hwnd=1639286, main_window=_DeadWrapper())
    monkeypatch.setattr(WindowsGUI, "window_rect_win32", staticmethod(lambda hwnd: None))

    def _no_gdi(self, win=None):
        raise RuntimeError("PrintWindow and BitBlt both failed")

    monkeypatch.setattr(WindowsGUI, "_capture_window_with_gdi", _no_gdi)

    result = gui.screenshot()

    assert result["ok"] is False
    assert "capture_as_image=" in result["error"]
    assert "gdi=PrintWindow and BitBlt both failed" in result["error"]


def test_capture_is_possible_from_a_cached_handle_with_no_wrapper_at_all(monkeypatch):
    """The wrapper may be gone entirely; a cached HWND is enough to capture."""
    gui = _gui(hwnd=1639286, main_window=None)
    monkeypatch.setattr(
        WindowsGUI, "window_rect_win32",
        staticmethod(lambda hwnd: (0, 0, 100, 50)),
    )

    class _Image:
        width, height = 100, 50

        def save(self, buffer, format=None):
            buffer.write(b"\x89PNG\r\n\x1a\n")

    monkeypatch.setattr(WindowsGUI, "_capture_window_with_gdi", lambda self, win=None: _Image())

    result = gui.screenshot()

    assert result["ok"] is True
    assert result["origin"] == [0, 0]
