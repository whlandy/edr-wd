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

import re
import time
from typing import Optional, Any

import pyautogui

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
        self._window_lock: dict | None = None

    # ── Identity ──────────────────────────────────────────────────────────────

    @property
    def backend(self) -> str:
        return self._gui.backend  # 'uia' or 'win32' depending on construction

    @property
    def connected_app(self):
        """Expose the connected pywinauto Application (used by status tool)."""
        return self._gui.app

    @property
    def connected_app_instance(self):
        """Expose the connected app object for restore_edr and similar helpers."""
        return self._gui.app

    @property
    def main_window(self):
        """Expose the connected main window (used by status tool)."""
        return self._gui.main_window

    # ── Window lock ──────────────────────────────────────────────────────────

    def _active_window_state(self) -> dict:
        try:
            from pywinauto import Desktop
            active = Desktop(backend=self.backend).get_active()
            title = active.window_text()
            pid = active.process_id()
            process_name = None
            try:
                import psutil
                process_name = psutil.Process(pid).name()
            except Exception:
                pass
            rect = None
            try:
                r = active.rectangle()
                rect = {"x": r.left, "y": r.top, "w": r.width(), "h": r.height()}
            except Exception:
                pass
            return {
                "ok": True,
                "title": title,
                "pid": pid,
                "process_name": process_name,
                "rectangle": rect,
            }
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def _connected_window_state(self) -> dict:
        win = self._gui.main_window
        if win is None:
            return {"ok": False, "error": "Not connected"}
        try:
            title = win.window_text()
        except Exception:
            title = ""
        try:
            pid = win.process_id()
        except Exception:
            pid = None
        process_name = None
        if pid:
            try:
                import psutil
                process_name = psutil.Process(pid).name()
            except Exception:
                pass
        rect = None
        try:
            r = win.rectangle()
            rect = {"x": r.left, "y": r.top, "w": r.width(), "h": r.height()}
        except Exception:
            pass
        return {
            "ok": True,
            "title": title,
            "pid": pid,
            "process_name": process_name,
            "rectangle": rect,
        }

    def _state_matches_lock(self, state: dict, lock: dict) -> bool:
        if not state.get("ok"):
            return False
        if lock.get("pid") is not None and state.get("pid") != lock.get("pid"):
            return False
        if lock.get("process_name"):
            current = (state.get("process_name") or "").lower()
            expected = str(lock["process_name"]).lower()
            if expected not in current and current not in expected:
                return False
        if lock.get("title_re"):
            try:
                if not re.search(str(lock["title_re"]), state.get("title") or ""):
                    return False
            except re.error:
                return False
        return True

    def _activate_locked_window(self) -> dict:
        if self._gui.main_window is None:
            return {"ok": False, "error": "No connected main window to activate"}
        try:
            self._gui.main_window.restore()
        except Exception:
            pass
        try:
            self._gui.main_window.set_focus()
            time.sleep(0.2)
            return {"ok": True, "method": "pywinauto.set_focus"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def lock_window(
        self,
        title_re: Optional[str] = None,
        process_name: Optional[str] = None,
        pid: Optional[int] = None,
        strict: bool = True,
        activate: bool = True,
    ) -> dict:
        state = self._connected_window_state()
        if not state.get("ok"):
            state = self._active_window_state()
        if not state.get("ok"):
            return state
        lock = {
            "backend": "windows_pywinauto",
            "title_re": title_re or (re.escape(state.get("title") or "") if state.get("title") else None),
            "process_name": process_name or state.get("process_name"),
            "pid": pid if pid is not None else state.get("pid"),
            "strict": bool(strict),
            "snapshot": state,
        }
        self._window_lock = lock
        if activate:
            self._activate_locked_window()
        verify = self.verify_window_lock(activate=activate)
        return {"ok": verify.get("ok") is True, "lock": lock, "verify": verify}

    def unlock_window(self) -> dict:
        previous = self._window_lock
        self._window_lock = None
        return {"ok": True, "previous": previous}

    def get_window_lock(self) -> dict:
        return {"ok": True, "locked": self._window_lock is not None, "lock": self._window_lock}

    def verify_window_lock(self, activate: bool = True) -> dict:
        if self._window_lock is None:
            return {"ok": True, "locked": False}
        state = self._active_window_state()
        if self._state_matches_lock(state, self._window_lock):
            return {"ok": True, "locked": True, "active": state, "lock": self._window_lock}
        activation = None
        if activate:
            activation = self._activate_locked_window()
            state = self._active_window_state()
            if self._state_matches_lock(state, self._window_lock):
                return {
                    "ok": True,
                    "locked": True,
                    "active": state,
                    "lock": self._window_lock,
                    "activation": activation,
                }
        return {
            "ok": False,
            "locked": True,
            "error": "Window lock mismatch",
            "active": state,
            "lock": self._window_lock,
            "activation": activation,
        }

    def _ensure_window_lock(self) -> Optional[dict]:
        check = self.verify_window_lock(activate=True)
        if check.get("ok"):
            return None
        return check

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
        if (lock_error := self._ensure_window_lock()) is not None:
            return lock_error
        return self._gui.click_at(x, y)

    def double_click_at(self, x: int, y: int) -> dict:
        if (lock_error := self._ensure_window_lock()) is not None:
            return lock_error
        try:
            pyautogui.doubleClick(x=int(x), y=int(y), button="left")
            return {"ok": True, "method": "pyautogui.doubleClick", "x": int(x), "y": int(y)}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def right_click_at(self, x: int, y: int) -> dict:
        if (lock_error := self._ensure_window_lock()) is not None:
            return lock_error
        try:
            pyautogui.rightClick(x=int(x), y=int(y))
            return {"ok": True, "method": "pyautogui.rightClick", "x": int(x), "y": int(y)}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def middle_click_at(self, x: int, y: int) -> dict:
        if (lock_error := self._ensure_window_lock()) is not None:
            return lock_error
        try:
            pyautogui.middleClick(x=int(x), y=int(y))
            return {"ok": True, "method": "pyautogui.middleClick", "x": int(x), "y": int(y)}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def hover_at(self, x: int, y: int) -> dict:
        if (lock_error := self._ensure_window_lock()) is not None:
            return lock_error
        try:
            pyautogui.moveTo(int(x), int(y))
            return {"ok": True, "method": "pyautogui.moveTo", "x": int(x), "y": int(y)}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def drag(self, x1: int, y1: int, x2: int, y2: int, duration: float = 0.25) -> dict:
        if (lock_error := self._ensure_window_lock()) is not None:
            return lock_error
        try:
            pyautogui.moveTo(int(x1), int(y1))
            pyautogui.dragTo(int(x2), int(y2), duration=float(duration), button="left")
            return {
                "ok": True,
                "method": "pyautogui.dragTo",
                "start": {"x": int(x1), "y": int(y1)},
                "end": {"x": int(x2), "y": int(y2)},
                "duration": float(duration),
            }
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def scroll(self, clicks: int, x: Optional[int] = None, y: Optional[int] = None) -> dict:
        if (lock_error := self._ensure_window_lock()) is not None:
            return lock_error
        try:
            if x is not None and y is not None:
                pyautogui.moveTo(int(x), int(y))
            pyautogui.scroll(int(clicks))
            payload = {"ok": True, "method": "pyautogui.scroll", "clicks": int(clicks)}
            if x is not None and y is not None:
                payload["point"] = {"x": int(x), "y": int(y)}
            return payload
        except Exception as e:
            return {"ok": False, "error": str(e)}

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

    def find_control(
        self,
        text: Optional[str] = None,
        role: Optional[str] = None,
        identifier: Optional[str] = None,
        title_re: Optional[str] = None,
        max_depth: int = 10,
    ) -> dict:
        tree = self.dump_tree(max_depth=max_depth)
        if not tree.get("ok"):
            return tree
        controls = tree.get("controls", [])
        if not isinstance(controls, list):
            return {"ok": False, "error": "dump_tree returned no controls array"}
        title_rx = None
        if title_re:
            try:
                title_rx = re.compile(title_re)
            except re.error as e:
                return {"ok": False, "error": f"invalid title_re: {e}"}
        matches = []
        for control in controls:
            if not isinstance(control, dict):
                continue
            haystack = " ".join(
                str(control.get(k) or "")
                for k in ("text", "title", "name", "class_name", "automation_id")
            )
            if text and text not in haystack:
                continue
            if role and role.lower() not in str(control.get("control_type") or control.get("class_name") or "").lower():
                continue
            if identifier and identifier != str(control.get("automation_id") or ""):
                continue
            if title_rx and not title_rx.search(haystack):
                continue
            matches.append(control)
        return {
            "ok": bool(matches),
            "matches": matches,
            "count": len(matches),
            "error": None if matches else "No Windows control matched selector",
        }

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
        if (lock_error := self._ensure_window_lock()) is not None:
            return lock_error
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
        auto_id_contains: Optional[str] = None,
        auto_id_suffix: Optional[str] = None,
        parent_of: Optional[str] = None,
        control_type: Optional[str] = None,
        x_offset: int = 0,
        y_offset: int = 0,
        parent_fallback: bool = True,
        timeout: float = 5.0,
    ) -> dict:
        if (lock_error := self._ensure_window_lock()) is not None:
            return lock_error
        # Same — drop timeout; click_target does not take it.
        return self._gui.click_target(  # type: ignore[call-overload]
            control_id, text, class_name, parent_text, automation_id,
            auto_id_contains, auto_id_suffix, parent_of, control_type,
            x_offset, y_offset, parent_fallback,
        )

    def click_window_at(self, x: int, y: int, window_title_re: Optional[str] = None) -> dict:
        if (lock_error := self._ensure_window_lock()) is not None:
            return lock_error
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
        Windows-specific. Ensure the HiSecEndpointAgent entry window and
        EDRClient target window are visible. Not part of the cross-platform
        protocol.
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
