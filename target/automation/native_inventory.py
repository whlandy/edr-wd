"""Join native top-level HWNDs to UIA without using UIA ancestry as HWND ancestry.

Qt may expose one native popup as a child of another popup in the UIA tree.
Desktop(UIA).windows()/is_top_level() then omit a live, visible HWND. Native
enumeration is authoritative for membership; UIA only enriches each exact HWND.
"""
from __future__ import annotations


class NativeWindowView:
    """Native identity/geometry with UIA controls, never a title-based join."""
    _NATIVE = frozenset({'handle', 'process_id', 'window_text', 'rectangle',
                         'is_visible', 'is_enabled'})

    def __init__(self, native, uia):
        self._native = native
        self._uia = uia

    def __getattr__(self, name):
        return getattr(self._native if name in self._NATIVE else self._uia, name)


def _uia_from_native(handle):
    # ElementFromHandle addresses the native popup directly. A desktop-root
    # find_elements(handle=...) search would omit the same nested Qt popup.
    from pywinauto.uia_element_info import UIAElementInfo
    from pywinauto.controls.uiawrapper import UIAWrapper
    return UIAWrapper(UIAElementInfo(handle))


def native_top_windows(desktop_factory, *, backend="uia", uia_factory=None):
    native = desktop_factory(backend="win32").windows()
    if backend == "win32":
        return native
    project = uia_factory or _uia_from_native
    result = []
    for window in native:
        # UIA may retain a hidden Qt popup with is_visible=True long after
        # native HIDE. Ask the native wrapper before projecting that handle.
        if not window.is_visible():
            continue
        handle = int(window.handle)
        projected = project(handle)
        if int(projected.handle) != handle:
            raise ValueError("native/UIA window identity mismatch")
        # Native foreground verification reads GetWindowText/IsWindowVisible.
        # Keep the same identity source here: Qt's UIA Menu root can have an
        # empty Name even when that exact native HWND is titled EDRClient.
        result.append(NativeWindowView(window, projected))
    return result
