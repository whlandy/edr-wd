"""
server.py — fastmcp HTTP Server for cross-platform EDR GUI Automation
======================================================================

Usage:
    # Local stdio
    python -m edr_wd.server

    # HTTP mode (for SSH tunnel / remote access)
    python -m edr_wd.server --http --port 8765

    # Expose beyond localhost only when direct LAN access is required
    python -m edr_wd.server --http --host 0.0.0.0 --port 8765

Backend selection:
    EDR_WD_AUTOMATION_BACKEND=windows_pywinauto    (default; legacy behavior)
    EDR_WD_AUTOMATION_BACKEND=macos_accessibility  (macOS target)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import threading
import time
import uuid

from fastmcp import FastMCP

from automation import create_backend
from automation.base import AutomationBackend

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("edr-wd")

# ---------------------------------------------------------------------------
# Global automation backend (singleton per server instance)
# ---------------------------------------------------------------------------
# Selection precedence: EDR_WD_AUTOMATION_BACKEND env var > "windows_pywinauto".
# The factory raises UnsupportedBackendError for unknown names — caught here
# so the server still starts (with a clear log line) and operators can fix
# the env var. This avoids breaking deployments over a typo.
try:
    _backend: AutomationBackend = create_backend()
    _backend_error: str | None = None
    _backend_kind = os.environ.get("EDR_WD_AUTOMATION_BACKEND") or (
        "macos_accessibility" if sys.platform == "darwin" else "windows_pywinauto"
    )
except Exception as _e:  # pragma: no cover — defensive startup guard
    logger.error("Failed to construct automation backend: %s", _e)
    _backend = None  # type: ignore[assignment]
    _backend_error = str(_e)
    _backend_kind = "unknown"


# Server bind metadata. Updated by main() so status() can report the actual
# runtime port instead of assuming the default 8765.
_server_host = "127.0.0.1"
_server_port = 8765


def _backend_unavailable(tool_name: str) -> str:
    """Return a structured error when the backend failed to load."""
    return json.dumps({
        "ok": False,
        "tool": tool_name,
        "backend": "unknown",
        "error": (
            "Automation backend is not loaded. "
            "Set EDR_WD_AUTOMATION_BACKEND=macos_accessibility on macOS "
            "or EDR_WD_AUTOMATION_BACKEND=windows_pywinauto on Windows. "
            f"Startup error: {_backend_error or 'unknown'}"
        ),
    }, ensure_ascii=False)


# ---------------------------------------------------------------------------
# FastMCP server
# ---------------------------------------------------------------------------
mcp = FastMCP("edr-wd")


@mcp.tool(
    name="connect",
    description=(
        "Connect to a GUI application by window title (regex), process name, PID, "
        "app name, or bundle id. The exact matchers supported depend on the "
        "active automation backend. Must be called before any other "
        "connect-required operation. "
        "auto_activate: if True and first connect attempt fails AND the backend "
        "exposes activate_edr() (Windows HiSec EDR only), try activate_edr() "
        "once then retry."
    ),
)
def connect(
    title_re: str = None,
    process_name: str = None,
    pid: int = None,
    app_name: str = None,
    bundle_id: str = None,
    timeout: float = 10.0,
    auto_activate: bool = False,
) -> str:
    if _backend is None:
        return json.dumps({"ok": False, "error": "Automation backend not initialized"})

    def do_connect():
        return _backend.connect(
            title_re=title_re,
            process_name=process_name,
            pid=pid,
            app_name=app_name,
            bundle_id=bundle_id,
            timeout=timeout,
        )

    result = do_connect()
    activate_result = None
    # auto_activate: try activate_edr on the backend if connect failed.
    # Windows needs PowerShell; macOS does not.
    needs_ps = "windows" in type(_backend).__name__.lower()
    if not result["ok"] and auto_activate and hasattr(_backend, "activate_edr"):
        if not needs_ps or ENABLE_POWERSHELL:
            activate_result = _backend.activate_edr()  # type: ignore[attr-defined]
            time.sleep(3)
            result = do_connect()
            if not result["ok"]:
                result["activate_result"] = activate_result
    return json.dumps(result, ensure_ascii=False)


@mcp.tool(
    name="activate_app",
    description=(
        "Activate (bring to foreground) an application by app_name or bundle_id. "
        "Supported by macos_accessibility and windows_pywinauto backends."
    ),
)
def activate_app(app_name: str = None, bundle_id: str = None) -> str:
    if _backend is None:
        return json.dumps({"ok": False, "error": "Automation backend not initialized"})
    if not hasattr(_backend, "activate_app"):
        return json.dumps({
            "ok": False,
            "error": "activate_app is not supported by this backend",
            "backend": getattr(_backend, "backend_name", "unknown"),
        })
    result = _backend.activate_app(app_name=app_name, bundle_id=bundle_id)
    return json.dumps(result, ensure_ascii=False)


@mcp.tool(
    name="dump_tree",
    description=(
        "Dump the control tree of the connected window. "
        "Returns a flat list of all controls with class_name, text, control_id, rectangle, "
        "is_visible, is_enabled. Use control_id as the unique identifier for click/select operations."
    ),
)
def dump_tree(window_title_re: str = None, max_depth: int = 10) -> str:
    if _backend is None:
        return _backend_unavailable("dump_tree")
    result = _backend.dump_tree(window_title_re, max_depth=max_depth)
    return json.dumps(result, ensure_ascii=False)


@mcp.tool(
    name="click",
    description=(
        "Click a control by control_id, text, class_name, automation_id, or derived filters. "
        "control_id is the preferred identifier (unique within the window). "
        "New: auto_id_contains, auto_id_suffix, parent_of, control_type for flexible lookup. "
        "parent_fallback=True (default): if the found control is a Static/Text/Label/Pane leaf, "
        "click its parent container automatically — use this for Qt 'card button' patterns."
    ),
)
def click(
    control_id: int = None,
    text: str = None,
    class_name: str = None,
    parent_text: str = None,
    automation_id: str = None,
    auto_id_contains: str = None,
    auto_id_suffix: str = None,
    parent_of: str = None,
    control_type: str = None,
    parent_fallback: bool = True,
) -> str:
    result = _backend.click(
        control_id, text, class_name, parent_text, automation_id,
        auto_id_contains, auto_id_suffix, parent_of, control_type,
        parent_fallback,
    )
    return json.dumps(result, ensure_ascii=False)


@mcp.tool(
    name="click_target",
    description=(
        "Click the centre of a matched control's screen rectangle. "
        "Unlike click() which uses click_input(), this uses mouse.click(coords). "
        "Prefer click() for Qt UIA controls; use this only when click_input() does not "
        "trigger the UI reaction. "
        "Supports auto_id_contains, auto_id_suffix, parent_of, control_type filters "
        "and parent_fallback=True (default, redirects Static/Text/Label/Pane to parent)."
    ),
)
def click_target(
    control_id: int = None,
    text: str = None,
    class_name: str = None,
    parent_text: str = None,
    automation_id: str = None,
    auto_id_contains: str = None,
    auto_id_suffix: str = None,
    parent_of: str = None,
    control_type: str = None,
    x_offset: int = 0,
    y_offset: int = 0,
    parent_fallback: bool = True,
) -> str:
    result = _backend.click_target(
        control_id=control_id,
        text=text,
        class_name=class_name,
        parent_text=parent_text,
        automation_id=automation_id,
        auto_id_contains=auto_id_contains,
        auto_id_suffix=auto_id_suffix,
        parent_of=parent_of,
        control_type=control_type,
        x_offset=x_offset,
        y_offset=y_offset,
        parent_fallback=parent_fallback,
    )
    return json.dumps(result, ensure_ascii=False)


@mcp.tool(
    name="click_at",
    description=(
        "Click absolute screen coordinates (x, y on the screen). "
        "Use when you have screen-space coordinates, e.g. from dump_tree window_rectangle + control rectangle center."
    ),
)
def click_at(x: int, y: int) -> str:
    if _backend is None:
        return _backend_unavailable("click_at")
    result = _backend.click_at(x, y)
    return json.dumps(result, ensure_ascii=False)


@mcp.tool(
    name="click_window_at",
    description=(
        "Click window-relative coordinates (x, y) converted to screen space using the connected window's rectangle. "
        "Use when you have coordinates relative to the window's top-left corner. "
        "If window_title_re is not given, uses the currently connected window."
    ),
)
def click_window_at(x: int, y: int, window_title_re: str = None) -> str:
    if _backend is None:
        return _backend_unavailable("click_window_at")
    result = _backend.click_window_at(x, y, window_title_re)
    return json.dumps(result, ensure_ascii=False)


@mcp.tool(
    name="type_text",
    description=(
        "Type text into an edit control. "
        "Finds the control by control_id (preferred), text, or class_name, "
        "then sets the text content."
    ),
)
def type_text(
    control_id: int = None,
    text: str = None,
    class_name: str = None,
    string: str = "",
) -> str:
    result = _backend.type_text(control_id, text, class_name, string)
    return json.dumps(result, ensure_ascii=False)


@mcp.tool(
    name="select",
    description=(
        "Select an item from a combo box (dropdown). "
        "Specify the combo by control_id, text, or class_name. "
        "Provide either item text (str) or zero-based index (int)."
    ),
)
def select(
    control_id: int = None,
    text: str = None,
    class_name: str = None,
    item: str = None,
    index: int = None,
) -> str:
    result = _backend.select(control_id, text, class_name, item, index)
    return json.dumps(result, ensure_ascii=False)


@mcp.tool(
    name="get_text",
    description="Get the text content of a control.",
)
def get_text(
    control_id: int = None,
    text: str = None,
    class_name: str = None,
) -> str:
    result = _backend.get_text(control_id, text, class_name)
    return json.dumps(result, ensure_ascii=False)


@mcp.tool(
    name="screenshot",
    description=(
        "Take a screenshot of the connected window. "
        "Returns base64 PNG image if path is not specified. "
        "Set path to save to file instead."
    ),
)
def screenshot(path: str = None) -> str:
    if _backend is None:
        return _backend_unavailable("screenshot")
    result = _backend.screenshot(path)
    return json.dumps(result, ensure_ascii=False)


@mcp.tool(
    name="activate_edr",
    description=(
        "Windows HiSec EDR specific. Activate the EDR GUI by launching "
        "HisecEndpointAgent with 'cmd ui'. By default waits up to 15 s for the "
        "EDRClient window to appear. If the window is already open, returns "
        "immediately with already_open=true. exe_path can override the default "
        "EDR executable path. Requires EDR_WD_ENABLE_POWERSHELL=1 on the server. "
        "On non-Windows backends, returns ok=false with an explanatory error."
    ),
)
def activate_edr(exe_path: str = None, wait: bool = True, timeout: float = 15.0,
                 edr_widget_auto_id: str = None) -> str:
    if _backend is None:
        return json.dumps({"ok": False, "error": "Automation backend not initialized"})
    if not hasattr(_backend, "activate_edr"):
        return json.dumps({
            "ok": False,
            "error": f"activate_edr is not supported by the {type(_backend).__name__} backend",
        })
    # Windows backend requires PowerShell; macOS backend does not
    if "windows" in type(_backend).__name__.lower() and not ENABLE_POWERSHELL:
        return json.dumps({"ok": False, "error": "PowerShell disabled: set EDR_WD_ENABLE_POWERSHELL=1 to enable"})
    result = _backend.activate_edr(  # type: ignore[attr-defined]
        exe_path=exe_path, wait=wait, timeout=timeout,
        edr_widget_auto_id=edr_widget_auto_id,
    )
    return json.dumps(result, ensure_ascii=False)


@mcp.tool(
    name="list_windows",
    description=(
        "List all top-level windows visible on the desktop. "
        "Does NOT require a prior connect() call. "
        "Returns every window with its title, process_id, class_name, handle, visible/enabled flags, and rectangle."
    ),
)
def list_windows() -> str:
    if _backend is None:
        return _backend_unavailable("list_windows")
    result = _backend.list_windows()
    return json.dumps(result, ensure_ascii=False)


@mcp.tool(
    name="is_window_open",
    description=(
        "Check if any window matches the given criteria (title regex, process name, or class name). "
        "At least one filter must be provided. "
        "Does NOT require a prior connect() call. "
        "Returns found=true/false and the matching windows array."
    ),
)
def is_window_open(
    title_re: str = None,
    process_name: str = None,
    class_name: str = None,
) -> str:
    result = _backend.is_window_open(title_re=title_re, process_name=process_name, class_name=class_name)
    return json.dumps(result, ensure_ascii=False)


@mcp.tool(
    name="wait_window",
    description=(
        "Poll until a window matching the given criteria appears, or timeout expires. "
        "Useful for verifying that a pop-up or new window has actually appeared after an action. "
        "Defaults: timeout=10 s, interval=0.5 s. "
        "Does NOT require a prior connect() call."
    ),
)
def wait_window(
    title_re: str = None,
    process_name: str = None,
    class_name: str = None,
    timeout: float = 10.0,
    interval: float = 0.5,
) -> str:
    result = _backend.wait_window(
        title_re=title_re, process_name=process_name, class_name=class_name,
        timeout=timeout, interval=interval,
    )
    return json.dumps(result, ensure_ascii=False)


@mcp.tool(
    name="restore_edr",
    description="Restore the EDR window if minimized. Call this before dump_tree/screenshot if the window may be minimized.",
)
def restore_edr() -> str:
    """还原 EDR 窗口（如果最小化则强制恢复）"""
    if _backend is None:
        return _backend_unavailable("restore_edr")
    try:
        if _backend.connected_app is None:
            return json.dumps({"ok": False, "error": "Not connected"})

        wins = _backend.connected_app.windows()
        if not wins:
            return json.dumps({"ok": False, "error": "No windows found"})

        win = wins[0]
        # Force-refresh window state
        try:
            win.wait_for_minimized(timeout=0.5)
            win.restore()
            win.wait_for_not_minimized(timeout=5)
        except Exception:
            pass  # Not minimized, continue

        r = win.rectangle()
        return json.dumps({
            "ok": True,
            "rectangle": {"x": r.left, "y": r.top, "w": r.width(), "h": r.height()},
            "is_minimized": win.is_minimized()
        })
    except Exception as e:
        logger.exception("restore_edr failed")
        return json.dumps({"ok": False, "error": str(e)})


# ---------------------------------------------------------------------------

# --------------------------------------------------------------------------
# Status
# --------------------------------------------------------------------------

import socket


def _get_server_pid() -> int | None:
    """Return the PID of the process listening on the configured port, or None."""
    try:
        import psutil
        for conn in psutil.net_connections(kind="inet"):
            if conn.laddr.port == _server_port and conn.status == "LISTEN":
                return conn.pid
    except Exception:
        pass
    return None


def _check_port(port: int) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(2)
    try:
        s.connect(("127.0.0.1", port))
        return True
    except Exception:
        return False
    finally:
        s.close()


@mcp.tool(name="status", description=(
    "Return the health and environment status of this MCP server. "
    "Use this before any operation to confirm the server is running in a "
    "proper interactive GUI session."
))
def status() -> str:
    warnings: list[str] = []

    pid = _get_server_pid()
    if pid is None:
        warnings.append("server_pid unavailable on this platform / session")

    port_open = _check_port(_server_port)
    if not port_open:
        warnings.append(f"port {_server_port} not reachable")

    backend_name = "unknown"
    backend_ok = _backend is not None
    if _backend is not None:
        try:
            backend_name = _backend.backend
        except Exception:
            pass

    # ok=true when server is alive and backend is loaded;
    # do NOT gate on pid detection (platform/session-dependent)
    result: dict[str, object] = {
        "ok": backend_ok and port_open,
        "host": _server_host,
        "port": _server_port,
        "backend": backend_name,
        "backend_kind": _backend_kind,
        "backend_loaded": backend_ok,
        "backend_error": _backend_error,
        "platform": sys.platform,
        "cwd": os.getcwd(),
        "server_pid": pid,
        "server_pid_available": pid is not None,
        "warnings": warnings,
        # HiSec-specific; granular process/window detection
        "hisec_agent_process_found": False,
        "hisec_main_window_found": False,
        "edr_client_process_found": False,
        "edr_client_window_found": False,
        "interactive_session": os.environ.get("SESSIONNAME", ""),
    }

    # Probe HiSecEndpoint process and window presence only on macOS.
    # Windows backends already have their own activation/status flow and
    # should not run the AppleScript/CGWindowList diagnostics here.
    if backend_name == "macos_accessibility" and _backend is not None and hasattr(_backend, "activate_edr"):
        proc_found = getattr(_backend, "_proc_exists", None)
        if proc_found:
            result["hisec_agent_process_found"] = proc_found("HiSecEndpointAgent")
            result["edr_client_process_found"] = proc_found("EDRClient")

        # Check windows with structured return (owner + pid + titles), not just bool
        import subprocess

        def _check_window_structured(proc_name: str) -> dict:
            """
            Returns {"found": bool, "owner": str, "pid": int|None, "titles": [str]}.
            Never raises — always returns a structured dict.
            """
            script = (
                f'tell application "System Events"\n'
                f'  set winList to every window of process "{proc_name}"\n'
                f'  set out to ""\n'
                f'  repeat with w in winList\n'
                f'    set out to out & (name of w) & "|"\n'
                f'  end repeat\n'
                f'  return out\n'
                f'end tell'
            )
            try:
                cp = subprocess.run(
                    ["osascript", "-e", script],
                    capture_output=True, text=True, timeout=5,
                )
                titles = []
                if cp.returncode == 0 and cp.stdout:
                    titles = [t for t in cp.stdout.strip().split("|") if t]
                # Also get PID for the process
                pid_script = f'tell application "System Events" to unix id of process "{proc_name}"'
                pid_cp = subprocess.run(
                    ["osascript", "-e", pid_script],
                    capture_output=True, text=True, timeout=3,
                )
                pid = None
                if pid_cp.returncode == 0:
                    try:
                        pid = int(pid_cp.stdout.strip())
                    except ValueError:
                        pass
                found = bool(titles)
                return {
                    "found": found,
                    "owner": proc_name,
                    "pid": pid,
                    "titles": titles,
                    "has_chinese": any("\u4e00" <= c <= "\u9fff" for t in titles for c in t),
                }
            except Exception as e:
                return {
                    "found": False,
                    "owner": proc_name,
                    "pid": None,
                    "titles": [],
                    "error": str(e),
                }

        hisec_struct = _check_window_structured("HiSecEndpointAgent")
        edr_struct = _check_window_structured("EDRClient")

        result["hisec_main_window_found"] = hisec_struct["found"]
        result["hisec_main_window_detected_by"] = hisec_struct.get("owner")
        result["hisec_main_window_pid"] = hisec_struct.get("pid")
        result["hisec_main_window_titles"] = hisec_struct.get("titles", [])
        result["edr_client_window_found"] = edr_struct["found"]
        result["edr_client_window_detected_by"] = edr_struct.get("owner")
        result["edr_client_window_pid"] = edr_struct.get("pid")
        result["edr_client_window_titles"] = edr_struct.get("titles", [])

    return json.dumps(result, ensure_ascii=False)


# PowerShell Execution (Popen + Terminate-Process, cancellable)
# Security: set EDR_WD_ENABLE_POWERSHELL=1 to enable (default: disabled)
# ---------------------------------------------------------------------------

ENABLE_POWERSHELL = os.environ.get("EDR_WD_ENABLE_POWERSHELL", "0") == "1"

# Job store: job_id -> {"proc": Popen, "command": str, "started_at": float}
_jobs: dict = {}
_jobs_lock = threading.Lock()


def _run_ps_sync(command: str, timeout: int, cwd: str) -> dict:
    """Synchronous run via subprocess.run (for run_powershell)."""
    started = time.time()
    try:
        proc = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive",
             "-ExecutionPolicy", "Bypass", "-Command", command],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )
        duration_ms = int((time.time() - started) * 1000)
        return {
            "ok": proc.returncode == 0,
            "stdout": proc.stdout[-20000:] if proc.stdout else "",
            "stderr": proc.stderr[-20000:] if proc.stderr else "",
            "returncode": proc.returncode,
            "duration_ms": duration_ms,
        }
    except subprocess.TimeoutExpired as e:
        return {
            "ok": False, "error": "timeout",
            "stdout": (e.stdout or "")[-20000:],
            "stderr": (e.stderr or "")[-20000:],
            "returncode": None,
            "duration_ms": int((time.time() - started) * 1000),
        }
    except Exception as e:
        return {"ok": False, "error": str(e), "returncode": None,
                "duration_ms": int((time.time() - started) * 1000)}


def _collect_ps_async(proc: subprocess.Popen, timeout: int, job_id: str) -> None:
    """Collect a background PowerShell process result and store it in _jobs."""
    started = time.time()
    try:
        stdout_bytes, stderr_bytes = proc.communicate(timeout=timeout)
        duration_ms = int((time.time() - started) * 1000)
        result = {
            "ok": proc.returncode == 0,
            "stdout": stdout_bytes.decode("utf-8", errors="replace")[-20000:],
            "stderr": stderr_bytes.decode("utf-8", errors="replace")[-20000:],
            "returncode": proc.returncode,
            "duration_ms": duration_ms,
        }
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        duration_ms = int((time.time() - started) * 1000)
        result = {
            "ok": False, "error": "timeout", "returncode": None,
            "duration_ms": duration_ms,
        }
    except Exception as e:
        result = {"ok": False, "error": str(e), "returncode": None,
                  "duration_ms": int((time.time() - started) * 1000)}
    with _jobs_lock:
        if job_id in _jobs:
            _jobs[job_id]["result"] = result
            _jobs[job_id]["completed_at"] = time.time()


def _check_ps_enabled() -> bool:
    if not ENABLE_POWERSHELL:
        logger.warning("PowerShell tools disabled (EDR_WD_ENABLE_POWERSHELL != 1)")
    return ENABLE_POWERSHELL


def _terminate_job(proc) -> None:
    """Terminate a running PowerShell process tree."""
    try:
        # Windows: use taskkill to kill process tree
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            capture_output=True, timeout=10,
        )
    except Exception:
        try:
            proc.terminate()
        except Exception:
            pass


@mcp.tool(
    name="run_powershell",
    description=(
        "Run a PowerShell command synchronously and return stdout/stderr/returncode. "
        "Short commands only (output < 20 KB, timeout < 30 s). "
        "Requires EDR_WD_ENABLE_POWERSHELL=1 environment variable on the server."
    ),
)
def run_powershell(command: str, timeout: int = 30, cwd: str = None) -> str:
    if not _check_ps_enabled():
        return json.dumps({"ok": False, "error": "PowerShell disabled: set EDR_WD_ENABLE_POWERSHELL=1 to enable"})
    result = _run_ps_sync(command, timeout, cwd)
    return json.dumps(result, ensure_ascii=False)


@mcp.tool(
    name="start_powershell",
    description=(
        "Start a PowerShell command as a background job. "
        "Poll with get_job(job_id=...). Cancel with cancel_job(job_id=...). "
        "Requires EDR_WD_ENABLE_POWERSHELL=1 environment variable on the server."
    ),
)
def start_powershell(command: str, timeout: int = 300, cwd: str = None) -> str:
    if not _check_ps_enabled():
        return json.dumps({"ok": False, "error": "PowerShell disabled: set EDR_WD_ENABLE_POWERSHELL=1 to enable"})
    job_id = uuid.uuid4().hex[:12]
    proc = subprocess.Popen(
        ["powershell.exe", "-NoProfile", "-NonInteractive",
         "-ExecutionPolicy", "Bypass", "-Command", command],
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=False,
    )
    with _jobs_lock:
        _jobs[job_id] = {"proc": proc, "command": command,
                          "started_at": time.time(), "timeout": timeout}
    # Fire-and-forget collector for the process we just started.
    t = threading.Thread(target=_collect_ps_async,
                        args=(proc, timeout, job_id), daemon=True)
    t.start()
    return json.dumps({"ok": True, "job_id": job_id}, ensure_ascii=False)


@mcp.tool(
    name="get_job",
    description="Poll a PowerShell job status. Returns result when done.",
)
def get_job(job_id: str) -> str:
    with _jobs_lock:
        if job_id not in _jobs:
            return json.dumps({"ok": False, "error": "job_id not found"})
        job = _jobs[job_id]
        proc = job["proc"]
    if proc.poll() is None:
        return json.dumps({"ok": True, "status": "running"})
    if "result" not in job:
        return json.dumps({"ok": True, "status": "collecting"})
    result = job["result"]
    return json.dumps({"ok": True, "status": "done", **result}, ensure_ascii=False)


@mcp.tool(
    name="cancel_job",
    description="Cancel a running PowerShell job by terminating its process tree.",
)
def cancel_job(job_id: str) -> str:
    with _jobs_lock:
        if job_id not in _jobs:
            return json.dumps({"ok": False, "error": "job_id not found"})
        job = _jobs[job_id]
        proc = job["proc"]
        _terminate_job(proc)
        del _jobs[job_id]
    return json.dumps({"ok": True}, ensure_ascii=False)


@mcp.tool(
    name="diagnose_windows",
    description=(
        "macOS-only debug tool: probe windows using both CGWindowList and "
        "System Events, then cross-reference. Useful for diagnosing why "
        "list_windows and activate_edr report different results for the same "
        "window."
    ),
)
def diagnose_windows() -> str:
    if _backend is None:
        return _backend_unavailable("diagnose_windows")

    backend_name = getattr(_backend, "backend", "unknown")
    if backend_name != "macos_accessibility":
        return json.dumps({
            "ok": False,
            "error": "diagnose_windows is only supported by the macos_accessibility backend",
            "backend": backend_name,
        }, ensure_ascii=False)

    from automation.macos_accessibility import diagnose_windows as _diag
    result = _diag()
    return json.dumps(result, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    global _server_host, _server_port
    parser = argparse.ArgumentParser(description="EDR-WD MCP Server")
    parser.add_argument("--http", action="store_true", help="Run in HTTP mode")
    parser.add_argument("--host", default="127.0.0.1", help="HTTP bind host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8765, help="HTTP port (default: 8765)")
    args = parser.parse_args()
    _server_host = args.host
    _server_port = args.port

    if args.http:
        logger.info(f"Starting HTTP server on {args.host}:{args.port}")
        mcp.run(transport="http", host=args.host, port=args.port)
    else:
        logger.info("Starting stdio server")
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
