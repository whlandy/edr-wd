# -*- coding: utf-8 -*-
"""A minimized locked window must not yield usable capture or dispatch.

A minimized top-level window's Win32 rect parks at (-32000,-32000).  Every
coordinate consumer downstream — GDI capture origin, screenshot metadata,
visual replay projection, pointer authorization — trusts the reported rect
to be the real on-screen position.  `_verify_frame_origin` (agent side)
cannot catch this because observation and capture *agree* on the parked
geometry.  The refusal therefore belongs at the capture source: before a
frame is captured (or a coordinate dispatched) the locked window must be
verified non-minimized, or an explicit `window_minimized` error returned.
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

MINIMIZED_RECT = (-32000, -32000, -31840, -31930)
VISIBLE_RECT = (80, 14, 1000, 614)


def _gui(hwnd=1639286, main_window=None):
    gui = WindowsGUI.__new__(WindowsGUI)
    gui.backend = "uia"
    gui.app = None
    gui.main_window = main_window
    gui._connected_hwnd = hwnd
    gui._window_lock = None
    return gui


def _win32(monkeypatch, rect):
    monkeypatch.setattr(
        WindowsGUI, "window_rect_win32",
        staticmethod(lambda hwnd: rect),
    )


class _Image:
    width, height = 160, 70

    def save(self, buffer, format=None):
        buffer.write(b"\x89PNG\r\n\x1a\n")


def test_minimized_rect_is_refused_by_capture_geometry(monkeypatch):
    """`_capture_geometry` is the shared origin of every capture path; the
    parked minimized rectangle must never become a capture origin."""
    gui = _gui(hwnd=1639286)
    _win32(monkeypatch, MINIMIZED_RECT)
    with pytest.raises(RuntimeError, match="window_minimized"):
        gui._capture_geometry()


def test_screenshot_refuses_a_minimized_window(monkeypatch):
    """The public screenshot entry must fail closed with an explicit code,
    not return a stale frame captured from the parked window."""
    gui = _gui(hwnd=1639286)
    _win32(monkeypatch, MINIMIZED_RECT)
    monkeypatch.setattr(
        WindowsGUI, "_capture_window_with_gdi", lambda self, win=None: _Image())
    monkeypatch.setattr(
        WindowsGUI, "is_minimized", lambda self, win=None: True)

    result = gui.screenshot()

    assert result["ok"] is False
    assert result["code"] == "window_minimized"
    assert "image_b64" not in result
    assert "saved_to" not in result


def test_screenshot_of_a_visible_window_is_unaffected(monkeypatch):
    gui = _gui(hwnd=1639286)
    _win32(monkeypatch, VISIBLE_RECT)
    monkeypatch.setattr(
        WindowsGUI, "_capture_window_with_gdi", lambda self, win=None: _Image())

    result = gui.screenshot()

    assert result["ok"] is True
    assert result["origin"] == [80, 14]


def test_screenshot_parks_no_file_when_minimized(monkeypatch, tmp_path):
    """Even when a `path` is requested, a minimized refusal must not write
    a partial or parked-geometry artifact to disk."""
    gui = _gui(hwnd=1639286)
    _win32(monkeypatch, MINIMIZED_RECT)
    monkeypatch.setattr(
        WindowsGUI, "_capture_window_with_gdi", lambda self, win=None: _Image())
    monkeypatch.setattr(
        WindowsGUI, "is_minimized", lambda self, win=None: True)

    result = gui.screenshot(path=str(tmp_path / "frame.png"))

    assert result["ok"] is False
    assert result["code"] == "window_minimized"
    assert not list(tmp_path.iterdir())


def test_is_minimized_reads_win32_placement():
    """The minimized check must come from Win32 GetWindowRect placement,
    not from UIA — the same source the capture origin trusts."""
    gui = _gui(hwnd=1639286)
    assert WindowsGUI._is_minimized_rect(MINIMIZED_RECT) is True
    assert WindowsGUI._is_minimized_rect(VISIBLE_RECT) is False
    # Real multi-monitor negative-left layouts park only x, not y.
    assert WindowsGUI._is_minimized_rect((-3840, 100, -3200, 300)) is False
    assert WindowsGUI._is_minimized_rect((100, -32000, 300, -31800)) is False
    assert WindowsGUI._is_minimized_rect(None) is False
