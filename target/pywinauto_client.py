"""
pywinauto_client.py — pywinauto 封装，提供控件级 GUI 操作

EDR (HiSecEndpoint) Windows 自动化核心客户端。
通过 pywinauto 连接 Windows 窗口，枚举控件树，按 control_id 执行操作。
"""

from __future__ import annotations

import base64
import io
import logging
import os
import subprocess
import time
from typing import Optional

import psutil
from pywinauto import Application, timings
from pywinauto import mouse

try:
    from artifacts import screenshot_path
except ImportError:
    from target.artifacts import screenshot_path

logger = logging.getLogger("edr_wd.pywinauto_client")

# Default HiSec entry executable path (can be overridden via EDR_WD_EDR_EXE env var)
DEFAULT_EDR_EXE = r"C:\Program Files\HiSec-Endpoint\core\safra\HisecEndpointAgent.exe"
DEFAULT_EDR_CLIENT_EXE = r"C:\Program Files\HiSec-Endpoint\core\EDRClient.exe"


class WindowsGUI:
    """Windows GUI 自动化客户端（pywinauto 封装）"""

    def __init__(self, backend: str = "uia"):
        self.backend = backend
        self.app: Optional[Application] = None
        self.main_window = None

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def connect_by_title(self, title_re: str, timeout: float = 10.0) -> dict:
        """通过窗口标题模糊匹配连接应用"""
        try:
            self.app = Application(backend=self.backend).connect(
                title_re=title_re, timeout=timeout
            )
            self.main_window = self.app.window(title_re=title_re)
            self.main_window.wait("visible", timeout=timeout)
            return {"ok": True, "title": self.main_window.window_text()}
        except Exception as e:
            logger.exception("connect_by_title failed")
            return {"ok": False, "error": str(e)}

    def _wrapper_handle(self, win) -> Optional[int]:
        """Return a pywinauto wrapper handle across UIA/win32 wrapper variants."""
        try:
            handle = getattr(win, "handle", None)
            if callable(handle):
                handle = handle()
            return int(handle) if handle else None
        except Exception:
            return None

    def _desktop_windows_for_pid(self, pid: int) -> list:
        """Return visible top-level Desktop windows for a PID."""
        from pywinauto import Desktop

        windows = []
        for win in Desktop(backend=self.backend).windows():
            try:
                if not win.is_top_level():
                    continue
            except Exception:
                pass
            try:
                if win.process_id() != pid:
                    continue
            except Exception:
                continue
            try:
                if not win.is_visible():
                    continue
            except Exception:
                pass
            windows.append(win)
        return windows

    def connect_by_process(self, process_name: str, timeout: float = 10.0) -> dict:
        """
        Connect by process name.

        Some Qt/UIA apps expose top-level windows through Desktop enumeration
        but cannot be reached by Application.connect(process=PID). Prefer the
        Desktop window handle path and keep process connect as fallback.
        """
        try:
            # Step 1: psutil 查找匹配的进程 PID
            matches = []
            for p in psutil.process_iter(["pid", "name", "exe", "username", "create_time"]):
                try:
                    name = p.info.get("name", "") or ""
                    if isinstance(name, str) and name.lower() == process_name.lower():
                        matches.append(p.info)
                except psutil.Error:
                    pass

            if not matches:
                return {"ok": False, "error": f"No process found matching: {process_name}"}

            # 返回多个候选，方便调试
            candidates = [{"pid": m["pid"], "name": m["name"]} for m in matches]

            errors = []
            for match in matches:
                pid = match["pid"]

                # Step 2a: prefer Desktop top-level window handle. This matches
                # is_window_open/list_windows and works for apps where
                # Application.connect(process=PID) reports no windows.
                desktop_windows = self._desktop_windows_for_pid(pid)
                for win in desktop_windows:
                    handle = self._wrapper_handle(win)
                    if not handle:
                        continue
                    try:
                        self.app = Application(backend=self.backend).connect(handle=handle, timeout=timeout)
                        self.main_window = self.app.window(handle=handle)
                        self.main_window.wait("visible", timeout=timeout)
                        return {
                            "ok": True,
                            "process": process_name,
                            "pid": pid,
                            "handle": handle,
                            "title": self.main_window.window_text(),
                            "method": "desktop_handle",
                            "candidates": candidates,
                        }
                    except Exception as e:
                        errors.append(f"handle {handle}: {e}")

                # Step 2b: fallback to pywinauto's process connection.
                try:
                    self.app = Application(backend=self.backend).connect(process=pid, timeout=timeout)
                    self.main_window = self.app.top_window()
                    self.main_window.wait("visible", timeout=timeout)
                    return {
                        "ok": True,
                        "process": process_name,
                        "pid": pid,
                        "method": "process",
                        "candidates": candidates,
                    }
                except Exception as e:
                    errors.append(f"process {pid}: {e}")

            return {
                "ok": False,
                "error": "No connectable top-level windows found for process",
                "process": process_name,
                "candidates": candidates,
                "attempt_errors": errors,
            }
        except Exception as e:
            logger.exception("connect_by_process failed")
            return {"ok": False, "error": str(e)}

    def connect_by_window(self, handle: int, pid: int, timeout: float = 10.0) -> dict:
        """Connect to one specific top-level window by handle.

        `connect_by_pid` binds `top_window()`, which is the wrong window
        whenever the process owns several. Callers that already resolved an
        exact window must keep that resolution.
        """
        try:
            self.app = Application(backend=self.backend).connect(process=pid, timeout=timeout)
            for win in self._desktop_windows_for_pid(pid):
                if self._wrapper_handle(win) == handle:
                    self.main_window = win
                    return {
                        "ok": True,
                        "pid": pid,
                        "handle": handle,
                        "title": win.window_text(),
                    }
            return {
                "ok": False,
                "error": f"window handle {handle} is no longer present for pid {pid}",
            }
        except Exception as e:
            logger.exception("connect_by_window failed")
            return {"ok": False, "error": str(e)}

    def connect_by_pid(self, pid: int) -> dict:
        """通过 PID 连接"""
        try:
            self.app = Application(backend=self.backend).connect(process=pid)
            self.main_window = self.app.top_window()
            return {"ok": True, "pid": pid}
        except Exception as e:
            logger.exception("connect_by_pid failed")
            return {"ok": False, "error": str(e)}

    # ------------------------------------------------------------------
    # Window Detection
    # ------------------------------------------------------------------

    # Default EDR window title regex (case-insensitive)
    _EDR_TITLE_RE = r".*(HiSec|Hisec|Endpoint|EDR|华为|安全).*"

    def _win_info(self, win) -> Optional[dict]:
        """Extract serialisable info from a pywinauto HwndElement wrapper."""
        try:
            title = win.window_text()
        except Exception:
            title = ""
        try:
            cid = win.control_id()
        except Exception:
            cid = None
        try:
            cls = win.friendly_class_name()
        except Exception:
            cls = ""
        try:
            pid = win.process_id()
        except Exception:
            pid = None
        try:
            rect = win.rectangle()
            rect_dict = {"x": rect.left, "y": rect.top, "w": rect.width(), "h": rect.height()}
        except Exception:
            rect_dict = {}
        try:
            visible = win.is_visible()
        except Exception:
            visible = False
        try:
            enabled = win.is_enabled()
        except Exception:
            enabled = False
        handle = self._wrapper_handle(win)

        return {
            "title": title,
            "class_name": cls,
            "control_id": cid,
            "process_id": pid,
            "handle": handle,
            "visible": visible,
            "enabled": enabled,
            "rectangle": rect_dict,
        }

    def _find_windows(self, title_re: str = None, process_name: str = None,
                      class_name: str = None) -> list:
        """
        Enumerate all top-level windows matching the given criteria.
        Uses Desktop(backend).windows() so does NOT require self.app to be connected.
        Returns a list of window info dicts.
        """
        import re
        from pywinauto import Desktop

        matched = []
        try:
            desktop = Desktop(backend=self.backend)
            for win in desktop.windows():
                try:
                    is_top = win.is_top_level()
                except Exception:
                    is_top = True  # keep window if is_top_level() is unavailable

                if not is_top:
                    continue

                info = self._win_info(win)
                if info is None:
                    continue

                # Filter by title regex
                if title_re:
                    try:
                        if not re.search(title_re, info["title"], re.IGNORECASE):
                            continue
                    except re.error:
                        continue
                # Filter by class_name
                if class_name:
                    if info["class_name"] != class_name:
                        continue
                # Filter by process name
                if process_name:
                    if info["process_id"] is None:
                        continue
                    try:
                        proc = psutil.Process(info["process_id"])
                        if proc.name().lower() != process_name.lower():
                            continue
                    except Exception:
                        continue

                matched.append(info)
        except Exception as e:
            logger.warning("_find_windows error: %s", e)
        return matched

    def list_windows(self) -> dict:
        """
        List all top-level windows visible on the desktop.
        Returns all windows regardless of whether they match EDR patterns.
        """
        try:
            from pywinauto import Desktop
            windows = []
            for win in Desktop(backend=self.backend).windows():
                try:
                    if not win.is_top_level():
                        continue
                except Exception:
                    pass  # keep window if is_top_level() is unavailable
                info = self._win_info(win)
                if info:
                    windows.append(info)
            return {"ok": True, "count": len(windows), "windows": windows}
        except Exception as e:
            logger.exception("list_windows failed")
            return {"ok": False, "error": str(e)}

    def is_window_open(self, title_re: str = None, process_name: str = None,
                       class_name: str = None) -> dict:
        """
        Check if any window matches the given criteria.
        At least one of title_re / process_name / class_name must be provided.
        """
        if not any([title_re, process_name, class_name]):
            return {"ok": False, "error": "At least one filter required: title_re, process_name, or class_name"}
        windows = self._find_windows(title_re=title_re, process_name=process_name,
                                      class_name=class_name)
        return {"ok": True, "found": len(windows) > 0, "count": len(windows), "windows": windows}

    def wait_window(self, title_re: str = None, process_name: str = None,
                    class_name: str = None, timeout: float = 10.0,
                    interval: float = 0.5) -> dict:
        """
        Poll until a matching window appears or timeout expires.

        Business timeout is handled internally — does NOT rely on HTTP/SSE timeout.
        Returns structured result on both success and timeout; caller never sees
        an HTTP-level timeout exception.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                result = self.is_window_open(
                    title_re=title_re, process_name=process_name,
                    class_name=class_name
                )
            except Exception as e:
                # Internal error — keep polling, surface it only on timeout
                logger.warning("wait_window: is_window_open raised %s", e)
                time.sleep(interval)
                continue

            if result.get("found"):
                return result
            time.sleep(interval)

        # Timeout: surface the error that occurred during polling if any
        return {
            "ok": False,
            "found": False,
            "error": "timeout",
            "windows": [],
            "count": 0,
        }

    # ------------------------------------------------------------------
    # EDR Activation
    # ------------------------------------------------------------------

    def _hisec_current_left_tab(self) -> dict:
        """Identify the active HiSec left tab from structural UIA content."""
        try:
            if not hasattr(self, "app") or self.app is None:
                return {"ok": False, "tab": None, "error": "no app connection"}

            win = self.app.window(title_re="华为.*")
            try:
                win.wait("visible", timeout=3)
            except Exception:
                pass

            tree = self.dump_tree(max_depth=999)
            if not tree.get("ok"):
                return {"ok": False, "tab": None, "error": tree.get("error", "dump_tree failed")}

            # The active tab exposes a distinctive content Dialog:
            #   安全防护:    ...EdrUIMainWindow
            #   安全中心:    ...BaselineUIMainWindow
            # Do not fall back to coordinates here; ambiguous tree state must be
            # surfaced so activate_edr does not click the wrong page.
            has_edr_dialog = False
            has_baseline_dialog = False

            for ctrl in tree.get("controls", []):
                aid = ctrl.get("automation_id", "")
                cls = ctrl.get("class_name", "")
                if cls == "Dialog":
                    if aid.endswith("EdrUIMainWindow"):
                        has_edr_dialog = True
                    elif aid.endswith("BaselineUIMainWindow"):
                        has_baseline_dialog = True

            if has_edr_dialog and not has_baseline_dialog:
                return {"ok": True, "tab": "安全防护"}
            if has_baseline_dialog and not has_edr_dialog:
                return {"ok": True, "tab": "安全中心"}

            return {
                "ok": False,
                "tab": None,
                "error": (
                    "ambiguous tab state: "
                    f"edr={has_edr_dialog}, baseline={has_baseline_dialog}"
                ),
            }
        except Exception as e:
            return {"ok": False, "tab": None, "error": str(e)}

    def _ensure_security_protection_tab(self) -> dict:
        """Ensure HiSecEndpointAgent is on the left-side 安全防护 tab."""
        before = self._hisec_current_left_tab()
        if before.get("ok") and before.get("tab") == "安全防护":
            return {"ok": True, "already": True, "before": before, "after": before}

        click_result = self.click(auto_id_suffix=".SafraUI.EdrUI", control_type="CheckBox")
        if not click_result.get("ok"):
            return {
                "ok": False,
                "already": False,
                "before": before,
                "click": click_result,
                "error": click_result.get("error", "failed to click 安全防护 tab"),
            }

        time.sleep(0.3)
        after = self._hisec_current_left_tab()
        return {
            "ok": after.get("ok") and after.get("tab") == "安全防护",
            "already": False,
            "before": before,
            "after": after,
            "click": click_result,
            "error": None if after.get("tab") == "安全防护" else after.get("error", "tab did not switch to 安全防护"),
        }

    def activate_edr(self, exe_path: str = None, wait: bool = True,
                     timeout: float = 15.0,
                     edr_widget_auto_id: str = None) -> dict:
        """
        Activate the Windows HiSec EDR GUI and verify both desktop windows.

        Flow:
          1. Ensure HisecEndpointAgent.exe is visible via `cmd ui`.
          2. Prefer `EDRClient.exe 17 --show` to open the EDRClient window.
          3. Fall back to clicking HisecEndpointAgent's edrWidget.
          4. Optionally connect EDRClient for follow-up dump/click/restore.

        Args:
            exe_path: path to HisecEndpointAgent.exe.
            wait: if True, block until EDRClient appears and is connected.
            timeout: seconds to wait for EDRClient window.
            edr_widget_auto_id: full automation_id of the edrWidget GroupBox.
                Defaults to the known EDR path.
        """
        exe = exe_path or os.environ.get("EDR_WD_EDR_EXE", DEFAULT_EDR_EXE)
        client_exe = os.environ.get("EDR_WD_EDR_CLIENT_EXE", DEFAULT_EDR_CLIENT_EXE)

        # Default automation_id for the edrWidget GroupBox (card-button parent
        # of the "前往安全防护中心" Static label). This path is stable for
        # the current EDR version.
        default_edr_widget_id = (
            "SafraUIMainWindow.MainWidget.content_widget.featureWidget."
            "EdrUIMainWindow.centralwidget.edrWidget"
        )
        edr_widget_auto_id = edr_widget_auto_id or default_edr_widget_id

        def _launch(args: list[str], cwd: str | None = None) -> tuple[bool, str | None]:
            try:
                subprocess.Popen(args, cwd=cwd)
                return True, None
            except Exception as e:
                logger.exception("activate_edr: failed to launch %s", args)
                return False, str(e)

        # ── Step 1: ensure HisecEndpointAgent entry window ──────────────
        hisec_win = self.is_window_open(process_name="HisecEndpointAgent.exe")
        if not hisec_win.get("found"):
            launched, launch_error = _launch([exe, "cmd", "ui"], cwd=os.path.dirname(exe) or None)
            if not launched:
                return {"ok": False, "error": f"Failed to launch HisecEndpointAgent: {launch_error}"}

            if not wait:
                return {"ok": True, "already_open": False, "exe_path": exe}

            # Wait for HisecEndpointAgent window to appear
            hisec_win = self.wait_window(
                process_name="HisecEndpointAgent.exe", timeout=timeout
            )
            if not hisec_win.get("found"):
                return {
                    "ok": False,
                    "error": "HisecEndpointAgent.exe window did not appear",
                    "exe_path": exe,
                }

        # ── Step 2: primary EDRClient path ──────────────────────────────
        primary_launch = {"ok": None, "path": client_exe, "args": ["17", "--show"]}
        edr_client = self.is_window_open(process_name="EDRClient.exe")
        if not edr_client.get("found"):
            launched, launch_error = _launch(
                [client_exe, "17", "--show"],
                cwd=os.path.dirname(client_exe) or None,
            )
            primary_launch["ok"] = launched
            if launch_error:
                primary_launch["error"] = launch_error
            if wait and launched:
                edr_client = self.wait_window(
                    process_name="EDRClient.exe", timeout=timeout, interval=0.5
                )
            elif not wait:
                return {
                    "ok": True,
                    "already_open": False,
                    "main": {"window_found": bool(hisec_win.get("found")), "window": hisec_win},
                    "client": {"window_found": False, "window": edr_client},
                    "exe_path": exe,
                    "client_exe_path": client_exe,
                    "primary_launch": primary_launch,
                }
        else:
            primary_launch["ok"] = False
            primary_launch["skipped"] = "EDRClient.exe already visible"

        # ── Step 3: fallback click from HisecEndpointAgent ──────────────
        click_result = {"ok": None, "skipped": True}
        tab_check = {"ok": True, "skipped": True}
        if not edr_client.get("found"):
            conn = self.connect_by_process("HisecEndpointAgent.exe", timeout=10)
            if not conn.get("ok"):
                return {
                    "ok": False,
                    "error": f"Cannot connect to HisecEndpointAgent: {conn.get('error')}",
                    "stage": "fallback_connect_hisec",
                    "main": {"window_found": bool(hisec_win.get("found")), "window": hisec_win},
                    "client": {"window_found": False, "window": edr_client},
                    "primary_launch": primary_launch,
                    "tab_check": tab_check,
                }

            # The fallback edrWidget only exists on the left-side 安全防护 page.
            # If HisecEndpointAgent is currently on 安全中心, switch back first;
            # if the page cannot be identified, fail instead of guessing.
            tab_check = self._ensure_security_protection_tab()
            if not tab_check.get("ok"):
                return {
                    "ok": False,
                    "error": tab_check.get("error", "could not switch to 安全防护 tab"),
                    "stage": "security_protection_tab_required",
                    "tab_check": tab_check,
                    "main": {"window_found": bool(hisec_win.get("found")), "window": hisec_win},
                    "client": {"window_found": False, "window": edr_client},
                    "primary_launch": primary_launch,
                }

            click_result = self.click(automation_id=edr_widget_auto_id)
            if not click_result.get("ok"):
                logger.warning("activate_edr: click edrWidget failed: %s", click_result.get("error"))

            edr_client = self.wait_window(
                process_name="EDRClient.exe", timeout=timeout, interval=0.5
            )
            if not edr_client.get("found"):
                return {
                    "ok": False,
                    "error": "EDRClient.exe window did not appear via primary path or edrWidget fallback",
                    "stage": "post_fallback_wait",
                    "click_ok": click_result.get("ok", False),
                    "edr_client_found": False,
                    "main": {"window_found": bool(hisec_win.get("found")), "window": hisec_win},
                    "client": {"window_found": False, "window": edr_client},
                    "exe_path": exe,
                    "client_exe_path": client_exe,
                    "primary_launch": primary_launch,
                    "tab_check": tab_check,
                }

        if not wait:
            return {
                "ok": True,
                "already_open": False,
                "edr_client_found": True,
                "main": {"window_found": bool(hisec_win.get("found")), "window": hisec_win},
                "client": {"window_found": True, "window": edr_client},
                "exe_path": exe,
                "client_exe_path": client_exe,
                "primary_launch": primary_launch,
                "fallback_click": click_result,
                "tab_check": tab_check,
            }

        # Step 7: connect EDRClient only when caller explicitly asked for it
        conn_edr = self.connect_by_process("EDRClient.exe", timeout=10)
        return {
            "ok": True,
            "already_open": bool(primary_launch.get("skipped")),
            "main": {"window_found": bool(hisec_win.get("found")), "window": hisec_win},
            "client": {"window_found": True, "window": edr_client},
            "hisec_connected": True,
            "edr_client_connected": conn_edr.get("ok", False),
            "edr_client": edr_client,
            "exe_path": exe,
            "client_exe_path": client_exe,
            "primary_launch": primary_launch,
            "fallback_click": click_result,
            "tab_check": tab_check,
        }

    def _window_rect(self, window_title_re: str = None) -> dict:
        """获取窗口在屏幕上的绝对矩形坐标。"""
        try:
            if window_title_re and self.app:
                win = self.app.window(title_re=window_title_re)
            elif self.main_window:
                win = self.main_window
            else:
                return {}
            r = win.rectangle()
            return {"left": r.left, "top": r.top, "right": r.right, "bottom": r.bottom,
                    "width": r.width(), "height": r.height()}
        except Exception:
            return {}

    # ------------------------------------------------------------------
    # Dump tree
    # ------------------------------------------------------------------

    def dump_tree(self, window_title_re: str = None, max_depth: int = 10) -> dict:
        """
        导出控件树。

        rectangle_mode:
          "screen" — ctrl.rectangle() 返回屏幕绝对坐标（pywinauto UIA 标准行为）
          "relative" — 控件位于子窗口内，坐标相对于子窗口

        返回结构:
        {
          "ok": True,
          "title": "窗口标题",
          "window_rectangle": {"x": 0, "y": 0, "w": 800, "h": 600},
          "rectangle_mode": "screen",
          "controls": [
            {
              "class_name": "Button",
              "text": "确定",
              "control_id": 12345,
              "rectangle": {"x": 0, "y": 0, "w": 100, "h": 30},
              "is_visible": True,
              "is_enabled": True,
              "depth": 0
            },
            ...
          ]
        }
        """
        try:
            if window_title_re and self.app:
                win = self.app.window(title_re=window_title_re)
            elif self.main_window:
                win = self.main_window
            else:
                return {"ok": False, "error": "No window connected"}

            win_rect = win.rectangle()
            win_rect_dict = {
                "x": win_rect.left, "y": win_rect.top,
                "w": win_rect.width(), "h": win_rect.height()
            }
            tree = self._build_tree(win, depth=0, max_depth=max_depth)
            return {
                "ok": True,
                "title": win.window_text(),
                "window_rectangle": win_rect_dict,
                "rectangle_mode": "screen",
                "controls": tree
            }
        except Exception as e:
            logger.exception("dump_tree failed")
            return {"ok": False, "error": str(e)}

    # Hard cap: prevent accidentally passing unbounded depth values
    _MAX_TREE_DEPTH = 15

    def _build_tree(self, ctrl, depth: int = 0, max_depth: int = 15) -> list:
        """递归构建控件树（带深度限制防止卡死）"""
        # Enforce hard cap regardless of what caller passed
        max_depth = min(max_depth, self._MAX_TREE_DEPTH)
        if depth > max_depth:
            return []

        results = []
        try:
            try:
                text = ctrl.window_text()
            except Exception:
                text = ""
            try:
                cid = ctrl.control_id()
            except Exception:
                cid = None
            try:
                cls = ctrl.friendly_class_name()
            except Exception:
                cls = ""
            try:
                rect = ctrl.rectangle()
                rect_dict = {"x": rect.left, "y": rect.top, "w": rect.width(), "h": rect.height()}
            except Exception:
                rect_dict = {}
            try:
                is_visible = ctrl.is_visible()
            except Exception:
                is_visible = False
            try:
                is_enabled = ctrl.is_enabled()
            except Exception:
                is_enabled = False
            # UIA-specific fields (may not exist on win32 backend)
            automation_id = ""
            control_type = ""
            try:
                automation_id = ctrl.automation_id()
            except Exception:
                pass
            try:
                control_type = str(ctrl.control_type())
            except Exception:
                pass
            is_password = None
            try:
                is_password = bool(ctrl.element_info.is_password)
            except Exception:
                try:
                    is_password = bool(ctrl.legacy_properties().get("IsPassword"))
                except Exception:
                    pass

            results.append({
                "class_name": cls,
                "text": text,
                "control_id": cid,
                "rectangle": rect_dict,
                "is_visible": is_visible,
                "is_enabled": is_enabled,
                "depth": depth,
                "automation_id": automation_id,
                "control_type": control_type,
                "is_password": is_password,
            })
        except Exception:
            pass

        # 递归子控件（限制子控件数量避免卡死）
        try:
            children = ctrl.children()
            for child in children[:200]:
                results.extend(self._build_tree(child, depth + 1, max_depth))
        except Exception:
            pass

        return results

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def click(self, control_id: int = None, text: str = None,
              class_name: str = None, parent_text: str = None,
              automation_id: str = None,
              auto_id_contains: str = None, auto_id_suffix: str = None,
              parent_of: str = None, control_type: str = None,
              parent_fallback: bool = True) -> dict:
        """
        Click a control (by control_id, automation_id, text, class_name, or derived filters).

        If parent_fallback=True and the matched control is a non-interactive text/label
        (Static/Text/Label/Pane), the click is automatically redirected to its parent
        container — this is the correct behaviour for Qt "card button" patterns where
        the visual text label is a child of the clickable GroupBox/QWidget.
        """
        try:
            ctrl = self._find_control(
                control_id=control_id, text=text, class_name=class_name,
                parent_text=parent_text, automation_id=automation_id,
                auto_id_contains=auto_id_contains, auto_id_suffix=auto_id_suffix,
                parent_of=parent_of, control_type=control_type,
            )
            if not ctrl:
                return {"ok": False, "error": "Control not found"}

            # Auto-parent fallback: if the found control is a non-interactive leaf
            # text node, click its parent container instead.
            if parent_fallback and self._control_is_leaf_text(ctrl):
                parent_ctrl = self._get_parent_of(ctrl)
                if parent_ctrl is not None:
                    logger.info("click: redirected from leaf %s to parent %s",
                                ctrl.automation_id(), parent_ctrl.automation_id())
                    ctrl = parent_ctrl

            semantic_result = self._semantic_activate(ctrl)
            if semantic_result is not None:
                semantic_result["control_id"] = control_id
                return semantic_result

            try:
                ctrl.click_input()
                time.sleep(0.1)
                return {"ok": True, "method": "click_input", "control_id": control_id,
                        "automation_id": getattr(ctrl, "automation_id", lambda: None)()}
            except Exception as click_error:
                logger.warning("click_input failed; falling back to coordinates: %s", click_error)
                rect = ctrl.rectangle()
                x = int(rect.left + rect.width() / 2)
                y = int(rect.top + rect.height() / 2)
                mouse.click(button="left", coords=(x, y))
                time.sleep(0.1)
                return {
                    "ok": True,
                    "method": "coordinate_fallback",
                    "control_id": control_id,
                    "automation_id": getattr(ctrl, "automation_id", lambda: None)(),
                    "x": x,
                    "y": y,
                    "click_input_error": str(click_error),
                }
        except Exception as e:
            logger.exception("click failed")
            return {"ok": False, "error": str(e)}

    def click_target(self, control_id: int = None, text: str = None,
                     class_name: str = None, parent_text: str = None,
                     automation_id: str = None,
                     auto_id_contains: str = None, auto_id_suffix: str = None,
                     parent_of: str = None, control_type: str = None,
                     x_offset: int = 0, y_offset: int = 0,
                     parent_fallback: bool = True) -> dict:
        """
        Click the centre of a matched control's screen rectangle.

        Unlike click() which uses click_input() (控件级点击), this uses
        mouse.click(coords) (裸坐标点击). For non-interactive text/label controls
        the click is redirected to the parent container when parent_fallback=True.

        Prefer click() for Qt UIA controls; use this only when click_input() is
        confirmed to not trigger the UI reaction.
        """
        try:
            ctrl = self._find_control(
                control_id=control_id, text=text, class_name=class_name,
                parent_text=parent_text, automation_id=automation_id,
                auto_id_contains=auto_id_contains, auto_id_suffix=auto_id_suffix,
                parent_of=parent_of, control_type=control_type,
            )
            if not ctrl:
                return {"ok": False, "error": "Control not found"}

            # Auto-parent fallback
            if parent_fallback and self._control_is_leaf_text(ctrl):
                parent_ctrl = self._get_parent_of(ctrl)
                if parent_ctrl is not None:
                    logger.info("click_target: redirected from leaf to parent")
                    ctrl = parent_ctrl

            rect = ctrl.rectangle()
            x = int(rect.left + rect.width() / 2 + x_offset)
            y = int(rect.top + rect.height() / 2 + y_offset)
            mouse.click(button="left", coords=(x, y))
            time.sleep(0.1)
            return {
                "ok": True,
                "method": "mouse.click",
                "x": x,
                "y": y,
                "rectangle": {"x": rect.left, "y": rect.top, "w": rect.width(), "h": rect.height()},
            }
        except Exception as e:
            logger.exception("click_target failed")
            return {"ok": False, "error": str(e)}

    def click_at(self, x: int, y: int) -> dict:
        """
        Click absolute screen coordinates (x, y on the screen).

        Use this when you already have screen-space coordinates, e.g. from
        dump_tree's window_rectangle + control rectangle center.
        """
        try:
            mouse.click(button="left", coords=(int(x), int(y)))
            time.sleep(0.1)
            return {"ok": True, "method": "mouse.click", "x": int(x), "y": int(y)}
        except Exception as e:
            logger.exception("click_at failed")
            return {"ok": False, "error": str(e)}

    def click_window_at(self, x: int, y: int, window_title_re: str = None) -> dict:
        """
        Click window-relative coordinates.

        Converts (x, y) from window-relative space to screen absolute space,
        then performs the click.

        Use this when you have coordinates relative to the window's top-left
        corner and dump_tree's rectangle_mode is "relative".
        """
        try:
            win_rect = self._window_rect(window_title_re)
            if not win_rect:
                return {"ok": False, "error": "No window connected"}
            screen_x = win_rect["left"] + int(x)
            screen_y = win_rect["top"] + int(y)
            mouse.click(button="left", coords=(screen_x, screen_y))
            time.sleep(0.1)
            return {
                "ok": True,
                "method": "mouse.click",
                "window_relative": {"x": x, "y": y},
                "screen": {"x": screen_x, "y": screen_y},
                "window_rect": win_rect,
            }
        except Exception as e:
            logger.exception("click_window_at failed")
            return {"ok": False, "error": str(e)}

    def double_click(self, control_id: int = None, text: str = None,
                     class_name: str = None) -> dict:
        """双击控件"""
        try:
            ctrl = self._find_control(control_id, text, class_name)
            if not ctrl:
                return {"ok": False, "error": "Control not found"}
            ctrl.double_click_input()
            return {"ok": True, "method": "double_click_input"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def type_text(self, control_id: int = None, text: str = None,
                   class_name: str = None, string: str = "") -> dict:
        """向控件输入文本"""
        try:
            ctrl = self._find_control(control_id, text, class_name)
            if not ctrl:
                return {"ok": False, "error": "Control not found"}
            ctrl.set_edit_text(string)
            return {"ok": True, "method": "set_edit_text", "text": string}
        except Exception as e:
            # Fallback: click first then type_keys
            try:
                ctrl.click_input()
                ctrl.type_keys(string, with_spaces=True)
                return {"ok": True, "method": "type_keys", "text": string}
            except Exception as e2:
                return {"ok": False, "error": f"set_edit_text: {e}, type_keys: {e2}"}

    def select(self, control_id: int = None, text: str = None,
                class_name: str = None, item: str = None, index: int = None) -> dict:
        """下拉框选择"""
        try:
            ctrl = self._find_control(control_id, text, class_name)
            if not ctrl:
                return {"ok": False, "error": "Control not found"}

            if item:
                ctrl.select(item)
            elif index is not None:
                ctrl.select(index)
            else:
                return {"ok": False, "error": "Must specify item or index"}

            return {"ok": True, "method": "select", "item": item or index}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def get_text(self, control_id: int = None, text: str = None,
                  class_name: str = None) -> dict:
        """读取控件文本"""
        try:
            ctrl = self._find_control(control_id, text, class_name)
            if not ctrl:
                return {"ok": False, "error": "Control not found"}
            content = ctrl.window_text()
            return {"ok": True, "text": content}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def screenshot(self, path: str = None) -> dict:
        """
        截图整个窗口。
        返回 base64 PNG 或保存到文件。
        """
        try:
            if self.main_window:
                win = self.main_window
            else:
                return {"ok": False, "error": "No window connected"}

            capture_error = None
            try:
                img = win.capture_as_image()
            except Exception as exc:
                capture_error = str(exc)
                img = None

            if img is None:
                try:
                    from PIL import ImageGrab

                    rect = win.rectangle()
                    bbox = (rect.left, rect.top, rect.right, rect.bottom)
                    img = ImageGrab.grab(bbox=bbox)
                except Exception as exc:
                    imagegrab_error = str(exc)
                    try:
                        img = self._capture_window_with_gdi(win)
                    except Exception as gdi_exc:
                        gdi_error = str(gdi_exc)
                        if capture_error:
                            return {
                                "ok": False,
                                "error": (
                                    "screen grab failed: "
                                    f"capture_as_image={capture_error}; "
                                    f"imagegrab={imagegrab_error}; "
                                    f"gdi={gdi_error}"
                                ),
                            }
                        return {
                            "ok": False,
                            "error": f"screen grab failed: imagegrab={imagegrab_error}; gdi={gdi_error}",
                        }

            buf = io.BytesIO()
            img.save(buf, format="PNG")
            b64 = base64.b64encode(buf.getvalue()).decode()
            result = {
                "ok": True,
                "image_b64": b64,
                "width": img.width,
                "height": img.height,
                "origin": [int(win.rectangle().left), int(win.rectangle().top)],
                "capture_scope": "window",
            }
            # The MCP contract says an omitted path returns in-memory PNG.
            # This is also required by recording source-redaction: an
            # unredacted frame must not be written before masking.
            if path is not None:
                output_path = screenshot_path(path)
                parent = os.path.dirname(output_path)
                if parent:
                    os.makedirs(parent, exist_ok=True)
                img.save(output_path)
                result.update({"saved_to": output_path, "path": output_path})
            return result
        except Exception as e:
            logger.exception("screenshot failed")
            return {"ok": False, "error": str(e)}

    def _capture_window_with_gdi(self, win):
        """Capture a window image with Win32 GDI when pywinauto/Pillow grabs fail."""
        import ctypes
        from ctypes import wintypes
        from PIL import Image

        hwnd = self._wrapper_handle(win)
        if not hwnd:
            raise RuntimeError("window handle unavailable")

        rect = win.rectangle()
        width = int(rect.width())
        height = int(rect.height())
        if width <= 0 or height <= 0:
            raise RuntimeError(f"invalid window rectangle: {rect}")

        user32 = ctypes.windll.user32
        gdi32 = ctypes.windll.gdi32

        screen_dc = user32.GetDC(0)
        mem_dc = gdi32.CreateCompatibleDC(screen_dc)
        bitmap = gdi32.CreateCompatibleBitmap(screen_dc, width, height)
        old_obj = gdi32.SelectObject(mem_dc, bitmap)

        try:
            # Try rendering the window itself first. If the app refuses
            # PrintWindow, fall back to copying the visible screen rectangle.
            rendered = user32.PrintWindow(hwnd, mem_dc, 2)
            if not rendered:
                SRCCOPY = 0x00CC0020
                copied = gdi32.BitBlt(mem_dc, 0, 0, width, height, screen_dc, rect.left, rect.top, SRCCOPY)
                if not copied:
                    raise RuntimeError("PrintWindow and BitBlt both failed")

            class BITMAPINFOHEADER(ctypes.Structure):
                _fields_ = [
                    ("biSize", wintypes.DWORD),
                    ("biWidth", wintypes.LONG),
                    ("biHeight", wintypes.LONG),
                    ("biPlanes", wintypes.WORD),
                    ("biBitCount", wintypes.WORD),
                    ("biCompression", wintypes.DWORD),
                    ("biSizeImage", wintypes.DWORD),
                    ("biXPelsPerMeter", wintypes.LONG),
                    ("biYPelsPerMeter", wintypes.LONG),
                    ("biClrUsed", wintypes.DWORD),
                    ("biClrImportant", wintypes.DWORD),
                ]

            class BITMAPINFO(ctypes.Structure):
                _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]

            bmi = BITMAPINFO()
            bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
            bmi.bmiHeader.biWidth = width
            bmi.bmiHeader.biHeight = -height
            bmi.bmiHeader.biPlanes = 1
            bmi.bmiHeader.biBitCount = 32
            bmi.bmiHeader.biCompression = 0  # BI_RGB
            bmi.bmiHeader.biSizeImage = width * height * 4

            buffer = ctypes.create_string_buffer(width * height * 4)
            lines = gdi32.GetDIBits(
                mem_dc,
                bitmap,
                0,
                height,
                buffer,
                ctypes.byref(bmi),
                0,
            )
            if lines != height:
                raise RuntimeError(f"GetDIBits returned {lines}/{height} lines")

            return Image.frombuffer("RGB", (width, height), buffer, "raw", "BGRX", 0, 1).copy()
        finally:
            if old_obj:
                gdi32.SelectObject(mem_dc, old_obj)
            if bitmap:
                gdi32.DeleteObject(bitmap)
            if mem_dc:
                gdi32.DeleteDC(mem_dc)
            if screen_dc:
                user32.ReleaseDC(0, screen_dc)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    # Control types that are typically non-interactive text labels and
    # should be auto-routed to their parent container when used as click targets.
    _NON_INTERACTIVE_TYPES = {"Static", "Text", "Label", "Pane"}

    def _control_is_leaf_text(self, ctrl) -> bool:
        """Return True if ctrl is a non-interactive text/label control."""
        try:
            ctrl_type = str(ctrl.control_type()) if ctrl.control_type() else ""
            cls = ctrl.friendly_class_name() or ""
            return ctrl_type in self._NON_INTERACTIVE_TYPES or cls in self._NON_INTERACTIVE_TYPES
        except Exception:
            return False

    def _get_parent_of(self, ctrl):
        """Return the parent of a control, or None if unavailable."""
        try:
            return ctrl.parent()
        except Exception:
            return None

    _SEMANTIC_ACTIVATION_TYPES = {
        "Button", "CheckBox", "RadioButton", "TabItem",
        "Hyperlink", "MenuItem", "ListItem",
    }

    def _semantic_activate(self, ctrl):
        """
        Activate a UIA control through its exposed pattern before using mouse input.

        Qt/UIA controls can expose accurate component patterns even when their
        screen rectangle is broad or overlaps neighbouring content. Prefer Invoke
        or Toggle so selectors from dump_tree stay component-driven.
        """
        try:
            control_type = str(ctrl.control_type() or "")
        except Exception:
            control_type = ""
        try:
            cls = ctrl.friendly_class_name() or ""
        except Exception:
            cls = ""

        # HiSec's left navigation tabs are exposed as UIA CheckBox controls.
        # toggle() can update UIA state without switching the Qt content page, so
        # these tab-like controls must be activated by a real component click.
        if self._is_hisec_left_nav_tab(ctrl, control_type, cls):
            return None

        if (
            control_type not in self._SEMANTIC_ACTIVATION_TYPES
            and cls not in self._SEMANTIC_ACTIVATION_TYPES
        ):
            return None

        errors = []
        for method in ("invoke", "toggle"):
            if not hasattr(ctrl, method):
                continue
            try:
                getattr(ctrl, method)()
                time.sleep(0.1)
                return {
                    "ok": True,
                    "method": f"uia_{method}",
                    "automation_id": getattr(ctrl, "automation_id", lambda: None)(),
                    "control_type": control_type,
                    "class_name": cls,
                }
            except Exception as exc:
                errors.append(f"{method}: {exc}")

        if errors:
            logger.info(
                "semantic activation unavailable for %s/%s: %s",
                control_type,
                cls,
                "; ".join(errors),
            )
        return None

    def _is_hisec_left_nav_tab(self, ctrl, control_type: str, cls: str) -> bool:
        """Return True for HiSec left navigation controls that require click_input."""
        if control_type != "CheckBox" and cls != "CheckBox":
            return False
        try:
            aid = ctrl.automation_id() or ""
        except Exception:
            return False
        return aid.endswith(".SafraUI.EdrUI") or aid.endswith(".SafraUI.BaselineUI")

    def _find_control(self, control_id=None, text=None, class_name=None, parent_text=None,
                      automation_id=None, auto_id_contains=None, auto_id_suffix=None,
                      parent_of=None, control_type=None):
        """
        Find a control by any combination of filters.

        New filters (compared to plain child_window):
          - auto_id_contains: automation_id must contain this substring
          - auto_id_suffix:   automation_id must end with this suffix
          - parent_of:        find a control whose text contains this string,
                              then return its *parent* container (useful for
                              "前往安全防护中心" Static label → edrWidget GroupBox)
          - control_type:     UIA control type must equal this string
        """
        try:
            if self.app is None:
                return None

            if parent_text:
                parent = self.app.window(title_re=parent_text)
            elif self.main_window:
                parent = self.main_window
            else:
                return None

            if control_id is not None:
                return parent.child_window(control_id=control_id)

            # ── auto_id exact match (original behaviour) ─────────────────
            if automation_id:
                try:
                    return parent.child_window(auto_id=automation_id)
                except Exception:
                    pass
                for ctrl in parent.descendants():
                    try:
                        if ctrl.automation_id() == automation_id:
                            return ctrl
                    except Exception:
                        pass

            # ── auto_id_contains / auto_id_suffix ────────────────────────
            if auto_id_contains is not None or auto_id_suffix is not None:
                for ctrl in parent.descendants():
                    try:
                        aid = ctrl.automation_id() or ""
                    except Exception:
                        continue
                    if auto_id_contains is not None and auto_id_contains not in aid:
                        continue
                    if auto_id_suffix is not None and not aid.endswith(auto_id_suffix):
                        continue
                    if control_type is not None:
                        try:
                            if str(ctrl.control_type()) != control_type:
                                continue
                        except Exception:
                            continue
                    return ctrl
                return None

            # ── parent_of: find leaf text control, return its parent ───────
            if parent_of is not None:
                for ctrl in parent.descendants():
                    try:
                        if parent_of in (ctrl.window_text() or ""):
                            if control_type is not None and str(ctrl.control_type()) != control_type:
                                continue
                            parent_ctrl = self._get_parent_of(ctrl)
                            if parent_ctrl is not None:
                                return parent_ctrl
                    except Exception:
                        continue
                return None

            # ── text-based search ────────────────────────────────────────
            if text:
                try:
                    return parent.child_window(title_re=text, class_name=class_name)
                except Exception:
                    pass
                for ctrl in parent.descendants():
                    try:
                        if text in (ctrl.window_text() or ""):
                            if class_name and ctrl.friendly_class_name() != class_name:
                                continue
                            if control_type is not None and str(ctrl.control_type()) != control_type:
                                continue
                            return ctrl
                    except Exception:
                        pass
                return None

            if class_name:
                return parent.child_window(class_name=class_name)

            return None
        except Exception:
            return None
