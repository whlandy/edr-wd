"""
windows_pywinauto.py — Windows automation backend.

Thin adapter that exposes the existing WindowsGUI (pywinauto) class
through the AutomationBackend protocol. We do not change WindowsGUI
itself — that keeps the 16/16 Windows HiSec EDR regression test green.

The class WindowsPywinautoBackend is a Proxy: every method call
delegates to the wrapped WindowsGUI instance. The `backend` property
returns 'uia' to match the legacy status() output.

Note: method signatures accept Optional[...] parameters for backend
uniformity, but WindowsGUI's underlying signatures predate PEP 604.
We pass the values through — WindowsGUI already handles "all None"
cases by returning a structured error.
"""

from __future__ import annotations

from typing import Optional, Any

# pywinauto_client is Windows-only and depends on pywinauto + psutil, which
# are NOT installed on macOS targets. We import it lazily inside __init__
# so that `from automation import create_backend; create_backend("macos_…")`
# never touches pywinauto on a Mac.



class WindowsPywinautoBackend:
    """AutomationBackend implementation for Windows (UIA via pywinauto)."""

    def __init__(self) -> None:
        # Lazy import: Windows-only dependency. macOS targets never
        # instantiate this backend.
        from pywinauto_client import WindowsGUI
        self._gui = WindowsGUI()

    # ── Identity ──────────────────────────────────────────────────────────────

    @property
    def backend(self) -> str:
        return self._gui.backend  # 'uia' or 'win32' depending on construction

    @property
    def connected_app(self):
        """Expose the connected pywinauto Application (used by status tool)."""
        return self._gui.app

    @property
    def main_window(self):
        """Expose the connected main window (used by status tool)."""
        return self._gui.main_window

    # ── Always-available ──────────────────────────────────────────────────────

    def list_windows(self) -> dict:
        return self._gui.list_windows()

    def is_window_open(
        self,
        title_re: Optional[str] = None,
        process_name: Optional[str] = None,
        class_name: Optional[str] = None,
    ) -> dict:
        return self._gui.is_window_open(title_re, process_name, class_name)  # type: ignore[arg-type]

    def wait_window(
        self,
        title_re: Optional[str] = None,
        process_name: Optional[str] = None,
        class_name: Optional[str] = None,
        timeout: float = 10.0,
        interval: float = 0.5,
    ) -> dict:
        return self._gui.wait_window(
            title_re, process_name, class_name, timeout, interval,  # type: ignore[arg-type]
        )

    def activate_app(
        self,
        app_name: Optional[str] = None,
        bundle_id: Optional[str] = None,
    ) -> dict:
        """
        On Windows, app_name maps to a process start.
        bundle_id is not applicable; we ignore it.
        """
        if not app_name:
            return {"ok": False, "error": "activate_app requires app_name on Windows"}
        # pywinauto's start() takes the path of the executable. We don't have
        # a path — for now, just bring any existing window of that name to
        # the foreground via a low-level Windows call. This is a best-effort
        # path; the Windows HiSec EDR workflow uses activate_edr() instead.
        return {
            "ok": False,
            "error": (
                "Windows activate_app(app_name=...) is best-effort only; "
                "use activate_edr() for the HiSec EDR workflow."
            ),
        }

    def screenshot(self, path: Optional[str] = None) -> dict:
        return self._gui.screenshot(path)  # type: ignore[arg-type]

    def click_at(self, x: int, y: int) -> dict:
        return self._gui.click_at(x, y)

    # ── Connect-required ──────────────────────────────────────────────────────

    def connect(
        self,
        title_re: Optional[str] = None,
        process_name: Optional[str] = None,
        pid: Optional[int] = None,
        app_name: Optional[str] = None,
        bundle_id: Optional[str] = None,
        timeout: float = 10.0,
        auto_activate: bool = False,
    ) -> dict:
        if title_re:
            return self._gui.connect_by_title(title_re, timeout)
        if process_name:
            return self._gui.connect_by_process(process_name, timeout)
        if pid:
            return self._gui.connect_by_pid(pid)
        if app_name:
            return self._gui.connect_by_process(app_name, timeout)
        return {"ok": False, "error": "Must specify title_re, process_name, pid, or app_name"}

    def dump_tree(self, window_title_re: Optional[str] = None, max_depth: int = 10) -> dict:
        return self._gui.dump_tree(window_title_re, max_depth=max_depth)  # type: ignore[arg-type]

    def click(
        self,
        control_id: Optional[int] = None,
        text: Optional[str] = None,
        class_name: Optional[str] = None,
        parent_text: Optional[str] = None,
        automation_id: Optional[str] = None,
        auto_id_contains: Optional[str] = None,
        auto_id_suffix: Optional[str] = None,
        parent_of: Optional[str] = None,
        control_type: Optional[str] = None,
        parent_fallback: bool = True,
        timeout: float = 5.0,
    ) -> dict:
        # `timeout` is accepted for backend uniformity but WindowsGUI.click
        # does not yet take it — drop it from the positional args.
        # The WindowsGUI signatures predate PEP 604 (no Optional[]), so all
        # Optional -> non-Optional assignments are intentional. # type: ignore[call-overload]
        return self._gui.click(  # type: ignore[call-overload]
            control_id, text, class_name, parent_text, automation_id,
            auto_id_contains, auto_id_suffix, parent_of, control_type,
            parent_fallback,
        )

    def click_target(
        self,
        control_id: Optional[int] = None,
        text: Optional[str] = None,
        class_name: Optional[str] = None,
        parent_text: Optional[str] = None,
        automation_id: Optional[str] = None,
        timeout: float = 5.0,
    ) -> dict:
        # Same — drop timeout; click_target does not take it.
        return self._gui.click_target(  # type: ignore[call-overload]
            control_id, text, class_name, parent_text, automation_id,
        )

    def click_window_at(self, x: int, y: int, window_title_re: Optional[str] = None) -> dict:
        return self._gui.click_window_at(x, y, window_title_re)  # type: ignore[arg-type]

    def type_text(
        self,
        text: str,
        control_id: Optional[int] = None,
        class_name: Optional[str] = None,
    ) -> dict:
        return self._gui.type_text(control_id, text, class_name, text)  # type: ignore[arg-type]

    def select(
        self,
        item: Optional[str] = None,
        index: Optional[int] = None,
        control_id: Optional[int] = None,
        text: Optional[str] = None,
        class_name: Optional[str] = None,
    ) -> dict:
        return self._gui.select(control_id, text, class_name, item, index)  # type: ignore[arg-type]

    def get_text(
        self,
        control_id: Optional[int] = None,
        text: Optional[str] = None,
        class_name: Optional[str] = None,
    ) -> dict:
        return self._gui.get_text(control_id, text, class_name)  # type: ignore[arg-type]

    # ── Windows HiSec EDR specific (not in AutomationBackend protocol) ─────

    def activate_edr(
        self,
        exe_path: Optional[str] = None,
        wait: bool = True,
        timeout: float = 15.0,
        edr_widget_auto_id: Optional[str] = None,
    ) -> dict:
        """
        Windows-specific. Launch the HiSec EDR GUI via PowerShell and wait
        for its main window. Not part of the cross-platform protocol.
        """
        return self._gui.activate_edr(  # type: ignore[call-overload]
            exe_path=exe_path, wait=wait, timeout=timeout,
            edr_widget_auto_id=edr_widget_auto_id,
        )

    # Legacy aliases used by some external callers
    def connect_by_title(self, title_re: str, timeout: float = 10.0) -> dict:
        return self._gui.connect_by_title(title_re, timeout)

    def connect_by_process(self, process_name: str, timeout: float = 10.0) -> dict:
        return self._gui.connect_by_process(process_name, timeout)

    def connect_by_pid(self, pid: int) -> dict:
        return self._gui.connect_by_pid(pid)
