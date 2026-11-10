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

try:
    from automation.physical_activation import (
        PhysicalActivationConfigError,
        parse_physical_activation_spec,
        physical_activation_required,
    )
except ImportError:
    from target.automation.physical_activation import (
        PhysicalActivationConfigError,
        parse_physical_activation_spec,
        physical_activation_required,
    )

logger = logging.getLogger("edr_wd.pywinauto_client")

try:
    from automation.native_pointer import send_left_click
except ImportError:
    from target.automation.native_pointer import send_left_click

try:
    from automation.native_inventory import native_top_windows
except ImportError:
    from target.automation.native_inventory import native_top_windows

#: The service that actually draws the EDR UI. `HisecEndpointAgent.exe cmd ui`
#: is not a launcher — it is a gRPC client that asks this service, over local
#: IPC, to show the window. With the service absent nothing is listening, the
#: command exits 0 having sent nothing, and no window ever appears.
DEFAULT_EDR_SERVICE = "HiSec OneAgent Service"
#: Where that IPC listens. Read from core\safra\etc\ipcportconf.ini when set;
#: this is the agent's own default for an empty file.
DEFAULT_EDR_IPC_PORT = 58299

#: UIA control types whose containers may virtualize their rows: `children()`
#: returns only the currently materialized items, with no error or cap signal,
#: so the returned list alone cannot prove the container is fully enumerated.
#: Listed lower-cased for direct comparison against `control_type()`.
_VIRTUALIZATION_CAPABLE = {
    "table", "datagrid", "list", "tree", "treeview", "listbox",
}


def _children_enumeration_is_proven_complete(gui, ctrl, children) -> bool:
    """Whether the container's returned children are provably all of them.

    ``children()`` returning without error proves exhaustiveness only for
    containers that cannot virtualize (plain panes/groups/buttons). For a
    virtualization-capable container (table/list/grid/tree) the materialized
    set may be a strict subset of all rows, so the walk must NOT claim
    completeness unless it has positive proof that the count of materialized
    children equals the container's reported item count.

    The former positive shortcut (``item_count == len(children)``) was
    unreliable: ``children()`` can also include non-row nodes (headers,
    scrollbars), so a table with 3 total rows, 2 materialized rows and 1 header
    reported ``len(children)==3`` and was wrongly marked complete. A partially
    materialized tree must never be compiled to a weak-identity ``ready``, so a
    virtualization-capable container is conservatively treated as not complete.
    """
    ctype = ""
    try:
        ctype = str(ctrl.control_type() or "")
    except Exception:
        try:
            ctype = str(ctrl.element_info.control_type or "")
        except Exception:
            ctype = ""
    if ctype.lower() in _VIRTUALIZATION_CAPABLE:
        # A virtualizable container's materialized children are not provably
        # all of them; stay incomplete rather than promise a ready against a
        # partial tree.
        return False
    return True


class WindowsGUI:
    """Windows GUI 自动化客户端（pywinauto 封装）"""

    def __init__(self, backend: str = "uia"):
        self.backend = backend
        self.app: Optional[Application] = None
        self.main_window = None
        # 显式物理激活声明（106 轮方案 C 收窄版）：进程+完整窗口 title+
        # automation_id 三元组同时精确匹配的控件跳过 UIA Invoke，落既有
        # 物理路径。配置缺失 → 无规则（默认语义零变化）；配置非法 → 启动
        # 即明确失败，不静默忽略。
        self._physical_activation_rules = parse_physical_activation_spec(
            os.environ.get("EDR_WD_PHYSICAL_ACTIVATION"))
        # HWND of the connected window, cached at connect time.  Screen capture
        # must not need a live UIA round-trip: when the session stops rendering
        # every UIA property read fails, and that is exactly when the GDI
        # PrintWindow path has to keep working.
        self._connected_hwnd: int | None = None

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
            self._connected_hwnd = self._wrapper_handle(self.main_window)
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
        for win in native_top_windows(Desktop, backend=self.backend):
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
                        self._connected_hwnd = int(handle)
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
                    self._connected_hwnd = self._wrapper_handle(self.main_window)
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
                    self._connected_hwnd = int(handle)
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
            self._connected_hwnd = self._wrapper_handle(self.main_window)
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
        # Design 11 D2: native owner edge for coexisting same-titled windows
        # (settings dialog + its language submenu both read "EDRClient").
        # GA_ROOTOWNER; 0/no-owner → absent key, never fabricated.
        owner_handle = None
        if isinstance(handle, int) and handle:
            try:
                import ctypes
                owner_handle = int(
                    ctypes.WinDLL("user32", use_last_error=True).GetAncestor(
                        handle, 3)) or None  # GA_ROOTOWNER
            except Exception:
                owner_handle = None
        # §7.2: focus verification reads the inventory's foreground fact —
        # the enumeration never carried it, so the live refresh answered
        # foreground=False for every window and refused all binds
        # (live run 47e0aac2: "bound instance is not foreground after
        # focus"). GetForegroundWindow() == handle.
        foreground = False
        if isinstance(handle, int) and handle:
            try:
                import ctypes
                foreground = int(
                    ctypes.WinDLL("user32", use_last_error=True)
                    .GetForegroundWindow()) == handle
            except Exception:
                foreground = False

        return {
            "title": title,
            "class_name": cls,
            "control_id": cid,
            "process_id": pid,
            "handle": handle,
            "visible": visible,
            "enabled": enabled,
            "rectangle": rect_dict,
            "owner_handle": owner_handle,
            "foreground": foreground,
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
            for win in native_top_windows(Desktop, backend=self.backend):
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

    def process_name_for_pid(self, pid: int) -> Optional[str]:
        """Real owning process name for a pid, or None when unresolvable.

        The recorder panel needs the true process behind each listed window
        to start a scoped capture; the window entries themselves carry no
        process name, so this is resolved with psutil (a declared dependency)
        at the only layer that can answer it.
        """
        try:
            import psutil
            return psutil.Process(int(pid)).name()
        except Exception:
            return None

    def list_windows(self) -> dict:
        """
        List all top-level windows visible on the desktop.
        Returns all windows regardless of whether they match EDR patterns.
        """
        try:
            from pywinauto import Desktop
            windows = []
            for win in native_top_windows(Desktop, backend=self.backend):
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

    @staticmethod
    def _service_state(name: str) -> str:
        """One of: "running", "stopped", "missing", "unknown".

        Read through `sc query`, which needs no extra dependency and reports a
        missing service distinctly from a stopped one — the distinction that
        decides whether activation is a retry or a reinstall.
        """
        try:
            completed = subprocess.run(
                ["sc", "query", name],
                capture_output=True, text=True, timeout=15,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception:
            return "unknown"
        output = (completed.stdout or "") + (completed.stderr or "")
        if completed.returncode == 1060 or "1060" in output:
            return "missing"
        if "RUNNING" in output:
            return "running"
        if "STOPPED" in output or "STOP_PENDING" in output:
            return "stopped"
        return "unknown"

    @staticmethod
    def _start_service(name: str, timeout: float = 30.0) -> tuple[bool, str | None]:
        try:
            completed = subprocess.run(
                ["sc", "start", name],
                capture_output=True, text=True, timeout=timeout,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception as e:
            return False, str(e)
        if completed.returncode == 0:
            return True, None
        return False, ((completed.stdout or "") + (completed.stderr or "")).strip()[:200]

    @staticmethod
    def _ipc_listening(port: int, timeout: float = 1.0) -> bool:
        import socket

        try:
            with socket.create_connection(("127.0.0.1", int(port)), timeout=timeout):
                return True
        except Exception:
            return False

    def _ensure_ui_service(self, service: str, port: int, timeout: float) -> dict:
        """Make the UI service available, or say precisely why it is not.

        Reporting "window did not appear" for this costs a whole debugging
        session: the command that was supposed to open the window exits 0
        whether or not anything received it, so the only visible symptom is a
        window that is missing for no stated reason.
        """
        state = self._service_state(service)
        if state == "missing":
            return {
                "ok": False,
                "stage": "ui_service_missing",
                "service": service,
                "service_state": state,
                "error": (
                    f"the service {service!r} is not installed. The EDR UI is "
                    "drawn by that service and `cmd ui` only asks it over local "
                    "IPC, so no command can open the window until the product "
                    "is reinstalled."
                ),
            }
        started = False
        if state in {"stopped", "unknown"}:
            started, start_error = self._start_service(service)
            if not started and state == "stopped":
                return {
                    "ok": False,
                    "stage": "ui_service_will_not_start",
                    "service": service,
                    "service_state": state,
                    "error": f"{service!r} is installed but would not start: {start_error}",
                }
        deadline = time.time() + max(timeout, 5.0)
        while time.time() < deadline:
            if self._ipc_listening(port):
                return {
                    "ok": True, "service": service, "service_state": "running",
                    "started_service": started, "ipc_port": port,
                }
            time.sleep(0.5)
        return {
            "ok": False,
            "stage": "ui_service_ipc_unreachable",
            "service": service,
            "service_state": self._service_state(service),
            "started_service": started,
            "ipc_port": port,
            "error": (
                f"nothing is listening on 127.0.0.1:{port}, so the request to "
                "show the window cannot be delivered"
            ),
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
        try:
            from .product_profiles import INSTALLED_PRODUCT_SPECS
        except ImportError:  # target-local deployment
            from product_profiles import INSTALLED_PRODUCT_SPECS
        application = INSTALLED_PRODUCT_SPECS[("windows_hisec", "windows_pywinauto")][
            "application"]
        exe = exe_path or application["entry_path"]
        client_exe = application["path"]
        ui_service = os.environ.get("EDR_WD_EDR_SERVICE", DEFAULT_EDR_SERVICE)
        try:
            ipc_port = int(os.environ.get("EDR_WD_EDR_IPC_PORT", DEFAULT_EDR_IPC_PORT))
        except ValueError:
            ipc_port = DEFAULT_EDR_IPC_PORT

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
        # The agent window is not a precondition for activation. It is only
        # needed by step 3's fallback click, and step 2 — the primary path —
        # launches the client directly. Treating a missing agent window as
        # fatal cost a whole session on a target whose agent service was not
        # registered: the client would have opened on the first try.
        hisec_win = self.is_window_open(process_name="HisecEndpointAgent.exe")
        service_check = {"ok": True, "skipped": "agent window already open"}
        agent_error: str | None = None
        if not hisec_win.get("found"):
            # `cmd ui` sends an IPC message and exits 0 whether or not anything
            # received it, so a failure here is otherwise invisible. Diagnose
            # it, report it, and carry on to the path that does not need it.
            service_check = self._ensure_ui_service(ui_service, ipc_port, timeout)
            if not service_check.get("ok"):
                agent_error = service_check.get("error")
            else:
                launched, launch_error = _launch(
                    [exe, "cmd", "ui"], cwd=os.path.dirname(exe) or None,
                )
                if not launched:
                    agent_error = f"failed to launch HisecEndpointAgent: {launch_error}"
                elif wait:
                    hisec_win = self.wait_window(
                        process_name="HisecEndpointAgent.exe", timeout=timeout,
                    )
                    if not hisec_win.get("found"):
                        agent_error = (
                            "the show-UI request reached "
                            f"{ui_service!r} but no window followed"
                        )

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
                    # The fallback needs the agent window; when the agent
                    # itself never came up, that reason is the useful one.
                    "error": (
                        f"the client did not open and the fallback needs the "
                        f"agent window, which is unavailable: {agent_error}"
                        if agent_error else
                        f"Cannot connect to HisecEndpointAgent: {conn.get('error')}"
                    ),
                    "stage": "fallback_connect_hisec",
                    "agent_error": agent_error,
                    "service_check": service_check,
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
            incomplete = [False]
            tree = self._build_tree(win, depth=0, max_depth=max_depth,
                                    _incomplete=incomplete)
            complete = not incomplete[0]
            return {
                "ok": True,
                "title": win.window_text(),
                "window_rectangle": win_rect_dict,
                "rectangle_mode": "screen",
                "truncated": not complete,
                "complete": complete,
                "controls": tree
            }
        except Exception as e:
            logger.exception("dump_tree failed")
            return {"ok": False, "error": str(e)}

    def dump_tree_for_window(self, handle: int, max_depth: int = 12) -> dict:
        """Dump one window's control tree without connecting to it.

        `dump_tree` walks whichever window this client is connected to, so
        using it to inspect a second window means re-connecting and losing the
        first. This addresses the window directly through the desktop, leaves
        `self.app` / `self.main_window` untouched, and never activates
        anything — it is safe to call while a recording is in progress.
        """
        try:
            from pywinauto import Desktop

            win = Desktop(backend=self.backend).window(handle=int(handle))
            rect = win.rectangle()
            incomplete = [False]
            controls = self._build_tree(win, depth=0, max_depth=max_depth,
                                        _incomplete=incomplete)
            complete = not incomplete[0]
            return {
                "ok": True,
                "title": win.window_text(),
                "handle": int(handle),
                "truncated": not complete,
                "complete": complete,
                "window_rectangle": {
                    "x": rect.left, "y": rect.top,
                    "w": rect.width(), "h": rect.height(),
                },
                "rectangle_mode": "screen",
                "controls": controls,
            }
        except Exception as e:
            logger.exception("dump_tree_for_window failed")
            return {"ok": False, "error": str(e), "handle": int(handle)}

    # Hard cap: prevent accidentally passing unbounded depth values
    _MAX_TREE_DEPTH = 15

    @staticmethod
    def _ancestor_entry(ctrl):
        """One ancestry level (controlType/automationId/name, empties dropped).

        Extracted so ``_build_tree`` can compute a child's chain from the
        already-walked parent chain without issuing any extra UIA ``parent()``
        round-trips per descendant node.

        Returns ``None`` when ``element_info`` cannot be read -- the invalid-
        chain sentinel. A valid-but-empty level returns ``{}``; callers must
        clear the whole chain (not keep a ``{}`` placeholder) when this returns
        ``None``, to match the recording side's all-or-nothing contract.
        """
        try:
            info = ctrl.element_info
            item = {
                "controlType": getattr(info, "control_type", None),
                "automationId": getattr(info, "automation_id", None),
                "name": getattr(info, "name", None),
            }
            return {
                key: str(value) for key, value in item.items()
                if value not in (None, "")
            }
        except Exception:
            # element_info access or any field read (properties may raise on
            # access too) fails -> invalid-chain sentinel clears the chain.
            return None

    def _build_ancestry(self, ctrl, *, limit: int = 6) -> list:
        """Ancestor chain of a control, nearest parent first.

        Mirrors the recording-side ancestry in ``target/recording/windows.py``:
        walk the UIA parent chain up to *limit* levels and, for each level,
        keep the ``controlType``/``automationId``/``name`` fields with empty
        values dropped. Replay-time observation must carry the same fields in
        the same order the compiler baked into the selector, or a recorded
        step whose control has no stable automation id (and therefore keeps
        an ancestry fallback selector) can never match at replay.

        Any exception while reading a level clears the entire chain, exactly
        as the recording side does -- a partially-built chain must never be
        emitted, or the two ends would disagree field-for-field (§5 contract).
        """
        ancestry: list = []
        try:
            parent = ctrl.parent()
            for _ in range(limit):
                if parent is None:
                    break
                # Read element_info directly in the guarded loop so a failure
                # reading any level clears the *entire* chain, exactly as the
                # recording-side ancestry does. Relying on _ancestor_entry here
                # would swallow the exception into a partial entry and break
                # the "all-or-nothing" chain contract its own docstring states.
                info = parent.element_info
                item = {
                    "controlType": getattr(info, "control_type", None),
                    "automationId": getattr(info, "automation_id", None),
                    "name": getattr(info, "name", None),
                }
                ancestry.append({
                    key: str(value) for key, value in item.items()
                    if value not in (None, "")
                })
                parent = parent.parent()
        except Exception:
            return []
        return ancestry

    def _build_tree(self, ctrl, depth: int = 0, max_depth: int = 15,
                    _parent_ancestry: list | None = None,
                    _chain_invalid: bool = False,
                    _incomplete: list | None = None) -> list:
        """递归构建控件树（带深度限制防止卡死）

        ``_parent_ancestry`` threads the nearest-parent-first ancestor chain
        down the recursion so each descendant node's ancestry is derived from
        its parent's already-built chain (one entry + the parent's prefix)
        instead of re-walking up to ``_MAX_TREE_DEPTH`` UIA ``parent()``
        calls per node. The root passes ``None`` and walks once.

        ``_chain_invalid`` is an *invalidated* marker distinct from an empty
        chain: once an ancestor's ``element_info`` cannot be read, every deeper
        descendant must also emit an empty ancestry (recording-side all-or-
        nothing contract). It is threaded explicitly so a later healthy parent
        does not rebuild a chain that a broken ancestor already invalidated.

        ``_incomplete`` is a per-call ``[bool]`` accumulator owned by the
        top-level ``dump_tree``/``dump_tree_for_window`` caller. Any truncation
        signal (depth cap, child-count cap, children-enumeration exception, or
        an unprovable virtualized container) flips it True, so completeness is
        decided from the whole walk, never from shared instance state. A direct
        caller without one gets a private accumulator; the boolean is read back
        from that same list after the call.
        """
        if _incomplete is None:
            _incomplete = [False]
        # Enforce hard cap regardless of what caller passed
        max_depth = min(max_depth, self._MAX_TREE_DEPTH)
        if depth > max_depth:
            # Reached the depth cap with a real child beneath: the walk is
            # incomplete, so downstream code must not treat it as exhaustive.
            _incomplete[0] = True
            return []

        results = []
        my_ancestry: list = []
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
                try:
                    control_type = str(ctrl.element_info.control_type or "")
                except Exception:
                    pass
            checked = None
            if (control_type or cls).lower() in {"checkbox", "radiobutton"}:
                try:
                    state = ctrl.get_toggle_state()
                    # UIA's third state is indeterminate, not checked.
                    if state in (0, 1):
                        checked = bool(state)
                except Exception:
                    # Radio buttons normally expose SelectionItem rather
                    # than Toggle. Preserve unknown when neither is available.
                    try:
                        checked = bool(ctrl.is_selected())
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

            if _chain_invalid:
                my_ancestry = []
            else:
                my_ancestry = (_parent_ancestry if _parent_ancestry is not None
                               else self._build_ancestry(ctrl))

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
                "checked": checked,
                "ancestry": list(my_ancestry),
            })
        except Exception:
            pass

        # 递归子控件（限制子控件数量避免卡死）
        try:
            children = ctrl.children()
            # A virtualization-capable container (table/list/grid/tree) may
            # return only the rows currently materialized, with no error and no
            # depth/child-count cap. `children()` completing without a signal
            # does NOT prove exhaustiveness; only a positive materialization
            # proof does. Without one, the walk cannot claim completeness, or a
            # weak-identity `ready` would be promised against a partial tree.
            if not _children_enumeration_is_proven_complete(self, ctrl, children):
                _incomplete[0] = True
            # Child chain = [this node] + this node's chain (nearest-first),
            # truncated to the same 6-level cap -- no fresh UIA parent() walks.
            # An invalid entry (sentinelled as None) means a level's
            # element_info could not be read: clear the whole chain instead of
            # propagating a `{}` placeholder, exactly as the recording-side
            # ancestry does when any level read fails.
            entry = self._ancestor_entry(ctrl)
            child_invalid = _chain_invalid or entry is None
            if child_invalid:
                child_ancestry = []
            else:
                child_ancestry = ([entry] + list(my_ancestry[:5]))[:6]
            if len(children) > 200:
                _incomplete[0] = True
            for child in children[:200]:
                results.extend(self._build_tree(
                    child, depth + 1, max_depth, child_ancestry, child_invalid,
                    _incomplete))
        except Exception:
            # A subtree whose children cannot be enumerated is an *unknown*
            # (incomplete) region, not a proven-empty one. Marking the whole
            # tree truncated is what lets the honest-completeness gate refuse a
            # weak-identity `ready`: a duplicate may live beyond the failure.
            _incomplete[0] = True

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

            physical_required = self._physical_activation_required_for(ctrl)
            semantic_result = (
                None if physical_required else self._semantic_activate(ctrl))
            if semantic_result is not None:
                semantic_result["control_id"] = control_id
                return semantic_result

            rect = ctrl.rectangle()
            x = int(rect.left + rect.width() / 2)
            y = int(rect.top + rect.height() / 2)
            pointer_guard = getattr(self, "pointer_guard", None)
            if callable(pointer_guard):
                guarded = pointer_guard(x, y)
                if isinstance(guarded, dict):
                    return guarded

            # Submission facts only. Never retry via another primitive after
            # an unknown/partial send; the caller verifies the declared effect.
            result = send_left_click(x, y)
            result.update(control_id=control_id,
                          automation_id=getattr(ctrl, "automation_id", lambda: None)())
            return result
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
            pointer_guard = getattr(self, "pointer_guard", None)
            if callable(pointer_guard):
                guarded = pointer_guard(x, y)
                if isinstance(guarded, dict):
                    return guarded
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
            return send_left_click(int(x), int(y))
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
                  class_name: str = None, string: str = "",
                  automation_id: str = None, control_type: str = None) -> dict:
        """向控件输入文本"""
        try:
            ctrl = self._find_control(control_id, text, class_name,
                                      automation_id=automation_id,
                                      control_type=control_type)
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

    # ------------------------------------------------------------------
    # Region / container-row observation (region_query assertions)
    # ------------------------------------------------------------------
    #
    # 增量只读能力：为区域/动态列表断言提供同帧行快照。仅做读取，不点击、
    # 不滚动、不改变任何 UI 状态；虚拟列表只反映当前可访问数据。待实机验证。

    _REGION_ROW_TYPES = {"listitem", "dataitem", "tablerow", "row",
                         "treeitem", "custom", "group"}

    def _resolve_control_identity(self, identity: dict):
        """Resolve one stable control identity inside the connected window.

        automationId wins; controlType+Name is the weak fallback. Returns
        (ctrl, None) or (None, error_code).
        """
        win = self.main_window
        if win is None:
            return None, "No window connected"
        automation_id = (identity.get("automationId")
                         or identity.get("automation_id") or "")
        control_type = (identity.get("controlType")
                        or identity.get("control_type") or "")
        name = identity.get("name") or identity.get("text") or ""
        matches = []
        for ctrl in win.descendants():
            try:
                aid = ""
                try:
                    aid = ctrl.automation_id() or ""
                except Exception:
                    pass
                if automation_id:
                    if aid == automation_id:
                        matches.append(ctrl)
                    continue
                ctype = ""
                try:
                    ctype = str(ctrl.control_type() or "")
                except Exception:
                    try:
                        ctype = str(ctrl.element_info.control_type or "")
                    except Exception:
                        pass
                wtext = ""
                try:
                    wtext = ctrl.window_text() or ""
                except Exception:
                    pass
                if control_type and name and ctype == control_type and wtext == name:
                    matches.append(ctrl)
            except Exception:
                continue
        if not matches:
            return None, "control_not_found"
        if len(matches) > 1:
            return None, "control_ambiguous"
        return matches[0], None

    def _control_is_password(self, ctrl) -> bool:
        """受保护控件识别（脱敏边界）：UIA IsPassword 优先，Win32 样式回退。

        读不出就当非受保护处理——不猜；真正的取值脱敏由服务端按行再兜一层。
        """
        for attr in ("is_password", "has_password"):
            try:
                value = getattr(ctrl, attr)
                value = value() if callable(value) else value
                if value:
                    return True
            except Exception:
                continue
        try:
            # UIA IsPassword 属性（LegacyIaAccessible / CurrentPropertyValue）。
            return bool(
                ctrl.element_info.CurrentPropertyValue(
                    30019)  # UIA_IsPasswordPropertyId
            ) if hasattr(ctrl.element_info, "CurrentPropertyValue") else False
        except Exception:
            pass
        try:
            # Win32 ES_PASSWORD (0x0020)。
            return bool(int(ctrl.element_info.style) & 0x20)
        except Exception:
            return False

    def _read_row_field(self, ctrl, field: str):
        """Read one field off a row control; unreadable stays None."""
        try:
            if field in ("name",):
                value = ctrl.window_text()
                return value if value else None
            if field in ("text", "time"):
                return ctrl.window_text() or None
            if field == "value":
                try:
                    return ctrl.get_value()
                except Exception:
                    try:
                        return ctrl.legacy_properties().get("Value")
                    except Exception:
                        return None
            if field == "checked":
                try:
                    state = ctrl.get_toggle_state()
                    return bool(state) if state in (0, 1) else None
                except Exception:
                    return None
            if field == "enabled":
                try:
                    return bool(ctrl.is_enabled())
                except Exception:
                    return None
        except Exception:
            return None
        return None

    def query_container_rows(self, container: dict = None,
                             fields: list = None) -> dict:
        """读取一个容器的当前行快照（同帧、只读、不刷新不滚动）。

        返回 {"ok": True, "rows": [{"rowIndex", <field>: value}]} 或
        {"ok": False, "error": str}。容器定位只用稳定身份（automationId 或
        controlType+Name），绝不用坐标。
        """
        try:
            if not isinstance(container, dict):
                return {"ok": False, "error": "container identity required"}
            wanted = [str(f) for f in (
                fields or ["name", "text", "value", "checked", "enabled", "time"])]
            container_ctrl, error = self._resolve_control_identity(container)
            if container_ctrl is None:
                return {"ok": False, "error": error or "container_not_found",
                        "container": dict(container)}
            rows = []
            for index, child in enumerate(container_ctrl.children()):
                try:
                    protected = self._control_is_password(child)
                    row = {"rowIndex": index}
                    for field in wanted:
                        value = self._read_row_field(child, field)
                        # 受保护控件的取值/文本在本端先脱敏，服务端再兜一层。
                        if protected and field in ("value", "text"):
                            value = None
                        row[field] = value
                    try:
                        row["controlType"] = str(child.control_type() or "")
                    except Exception:
                        row["controlType"] = None
                    if protected:
                        row["protected"] = True
                    rows.append(row)
                except Exception:
                    continue
            return {"ok": True, "rows": rows, "container": dict(container)}
        except Exception as e:
            logger.exception("query_container_rows failed")
            return {"ok": False, "error": str(e)}

    def read_component_values(self, components: list = None) -> dict:
        """读取固定多组件各自当前值（{"field","identity"} 列表 → 逐个只读）。"""
        try:
            values = []
            for component in components or []:
                if not isinstance(component, dict):
                    continue
                field = str(component.get("field") or "")
                identity = component.get("identity") or {}
                ctrl, error = self._resolve_control_identity(identity)
                if ctrl is None:
                    entry = {"field": field, "value": None,
                             "reason": error or "component_not_found"}
                    if component.get("key"):
                        entry["key"] = str(component["key"])
                    values.append(entry)
                    continue
                protected = self._control_is_password(ctrl)
                value = self._read_row_field(ctrl, field)
                if protected:
                    value = None
                entry = {"field": field, "value": value}
                if protected:
                    entry["protected"] = True
                # P2f：稳定键随采样值回传（身份锚定评估链路）。
                if component.get("key"):
                    entry["key"] = str(component["key"])
                values.append(entry)
            return {"ok": True, "values": values}
        except Exception as e:
            logger.exception("read_component_values failed")
            return {"ok": False, "error": str(e)}

    def screenshot(self, path: str = None) -> dict:
        """
        截图整个窗口。
        返回 base64 PNG 或保存到文件。
        """
        try:
            win = self.main_window
            if win is None and not self._connected_hwnd:
                return {"ok": False, "error": "No window connected"}
            if self.is_minimized(win):
                # A minimized window's GDI/PrintWindow frame is not a view of
                # the real UI: downstream replay would project recorded
                # coordinates onto parked geometry that observation also
                # reports, so agent-side stale-frame gates cannot catch it.
                return {
                    "ok": False,
                    "code": "window_minimized",
                    "error": (
                        "the connected window is minimized; restore it before "
                        "capture or replay"
                    ),
                }

            errors = {}
            img = None
            # Screen-composition paths first: best fidelity while the desktop
            # is being rendered.  Both need a live session.
            if win is not None:
                try:
                    img = win.capture_as_image()
                except Exception as exc:
                    errors["capture_as_image"] = str(exc)
                if img is None:
                    try:
                        from PIL import ImageGrab

                        bounds = self.window_rect_win32(self.window_handle(win))
                        if bounds is None:
                            rect = win.rectangle()
                            bounds = (rect.left, rect.top, rect.right, rect.bottom)
                        img = ImageGrab.grab(bbox=bounds)
                    except Exception as exc:
                        errors["imagegrab"] = str(exc)
            if img is None:
                # Background path: asks the window to paint itself, so it keeps
                # working when nothing is compositing the desktop.
                try:
                    img = self._capture_window_with_gdi(win)
                except Exception as exc:
                    errors["gdi"] = str(exc)
                    return {
                        "ok": False,
                        "error": "screen grab failed: " + "; ".join(
                            f"{name}={message}" for name, message in errors.items()
                        ),
                    }

            buf = io.BytesIO()
            img.save(buf, format="PNG")
            b64 = base64.b64encode(buf.getvalue()).decode()
            result = {
                "ok": True,
                "image_b64": b64,
                "width": img.width,
                "height": img.height,
                "origin": list(self._capture_origin(win)),
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

    def _capture_origin(self, win=None) -> tuple:
        """Top-left of the captured window, Win32 first so it survives UIA loss."""
        bounds = self.window_rect_win32(self.window_handle(win))
        if bounds is not None:
            return bounds[0], bounds[1]
        target = win if win is not None else self.main_window
        rect = target.rectangle()
        return int(rect.left), int(rect.top)

    # GetWindowRect parks a minimized window's rect at (-32000,-32000)-ish
    # coordinates; anything at that far-negative offset is not a real
    # on-screen position and must never become a capture origin or click
    # anchor. Both axes park together, so requiring both keeps a
    # multi-monitor layout with genuinely negative left (e.g. -3840)
    # unambiguous.
    _MINIMIZED_OFFSET = -30000

    @classmethod
    def _is_minimized_rect(cls, bounds) -> bool:
        if not bounds or len(bounds) != 4:
            return False
        return (bounds[0] < cls._MINIMIZED_OFFSET
                and bounds[1] < cls._MINIMIZED_OFFSET)

    def is_minimized(self, win=None) -> bool:
        """Win32 placement check, UIA-independent like the capture origin."""
        hwnd = self.window_handle(win)
        if not hwnd:
            return False
        bounds = self.window_rect_win32(hwnd)
        return bounds is not None and self._is_minimized_rect(bounds)

    def window_handle(self, win=None) -> Optional[int]:
        """HWND of the connected window, preferring the connect-time cache.

        Reading `.handle` off a UIA wrapper is itself a UIA call, so it fails
        under exactly the conditions the GDI capture path exists to survive.
        """
        if self._connected_hwnd:
            return self._connected_hwnd
        target = win if win is not None else self.main_window
        return self._wrapper_handle(target) if target is not None else None

    @staticmethod
    def window_rect_win32(hwnd: int) -> Optional[tuple]:
        """(left, top, right, bottom) straight from Win32, never through UIA."""
        try:
            import ctypes
            from ctypes import wintypes

            rect = wintypes.RECT()
            if not ctypes.windll.user32.GetWindowRect(int(hwnd), ctypes.byref(rect)):
                return None
            return int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)
        except Exception:
            return None

    def _capture_geometry(self, win=None) -> tuple:
        """Resolve (hwnd, left, top, width, height) without needing a live desktop."""
        hwnd = self.window_handle(win)
        if not hwnd:
            raise RuntimeError("window handle unavailable")
        bounds = self.window_rect_win32(hwnd)
        if bounds is None:
            target = win if win is not None else self.main_window
            if target is None:
                raise RuntimeError("window rectangle unavailable")
            rect = target.rectangle()
            bounds = (int(rect.left), int(rect.top), int(rect.right), int(rect.bottom))
        if self._is_minimized_rect(bounds):
            raise RuntimeError(
                "window_minimized: the locked window is minimized; its parked "
                f"rectangle {tuple(int(v) for v in bounds[:2])} cannot be a "
                "capture origin — restore the window first")
        left, top, right, bottom = bounds
        width, height = right - left, bottom - top
        if width <= 0 or height <= 0:
            raise RuntimeError(f"invalid window rectangle: {bounds}")
        return hwnd, left, top, width, height

    def _capture_window_with_gdi(self, win=None):
        """Capture a window with Win32 GDI, including while nothing renders it.

        `PrintWindow(PW_RENDERFULLCONTENT)` asks the window to paint itself into
        a memory DC rather than copying screen pixels, so it does not need the
        session's desktop to be composited — this is the background-capture
        path. BitBlt of the screen rectangle stays as a last resort and does
        require a rendering desktop.
        """
        import ctypes
        from ctypes import wintypes
        from PIL import Image

        hwnd, left, top, width, height = self._capture_geometry(win)

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
                copied = gdi32.BitBlt(mem_dc, 0, 0, width, height, screen_dc, left, top, SRCCOPY)
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

    def _physical_activation_required_for(self, ctrl) -> bool:
        """显式声明的三元组（process+完整 title+automation_id）同时精确匹配
        才让路物理路径；任何取值失败一律 False（不降级、不猜测）。"""
        if not getattr(self, "_physical_activation_rules", ()):
            return False
        try:
            aid = ctrl.automation_id() or ""
        except Exception:
            return False
        try:
            win = self.main_window
            title = win.window_text() if win is not None else ""
            pid = win.process_id() if win is not None else None
        except Exception:
            return False
        process_name = None
        if pid:
            try:
                process_name = psutil.Process(pid).name()
            except Exception:
                process_name = None
        return physical_activation_required(rules=self._physical_activation_rules,
                                            process_name=process_name,
                                            window_title=title,
                                            automation_id=aid)

    def _semantic_activate(self, ctrl):
        """
        Activate a UIA control through its exposed pattern before using mouse input.

        Qt/UIA controls can expose accurate component patterns even when their
        screen rectangle is broad or overlaps neighbouring content. Prefer Invoke
        or Toggle so selectors from dump_tree stay component-driven.

        显式物理激活声明（EDR_WD_PHYSICAL_ACTIVATION）命中的控件在此让路：
        返回 None → 调用方落既有控件定位 + pointer_guard + click_input 物理路径。
        """
        if self._physical_activation_required_for(ctrl):
            logger.info("physical activation declared for %r; skipping semantic activate",
                        getattr(ctrl, "automation_id", lambda: None)())
            return None
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
