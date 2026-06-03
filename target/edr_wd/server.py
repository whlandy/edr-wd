"""
server.py — fastmcp HTTP Server for Windows EDR GUI Automation
===============================================================

Usage:
    # Local stdio
    python -m edr_wd.server

    # HTTP mode (for SSH tunnel / remote access)
    python -m edr_wd.server --http --port 8765

    # Expose beyond localhost only when direct LAN access is required
    python -m edr_wd.server --http --host 0.0.0.0 --port 8765
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import threading
import time
import uuid

from fastmcp import FastMCP

from .pywinauto_client import WindowsGUI

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("edr-wd")

# ---------------------------------------------------------------------------
# Global GUI client (singleton per server instance)
# ---------------------------------------------------------------------------
_gui: WindowsGUI = WindowsGUI()


# ---------------------------------------------------------------------------
# FastMCP server
# ---------------------------------------------------------------------------
mcp = FastMCP("edr-wd")


@mcp.tool(
    name="connect",
    description=(
        "Connect to a Windows application by window title (regex), process name, or PID. "
        "Must be called before any other operation. "
        "auto_activate: if True and first connect attempt fails, try activate_edr() once then retry."
    ),
)
def connect(
    title_re: str = None,
    process_name: str = None,
    pid: int = None,
    timeout: float = 10.0,
    auto_activate: bool = False,
) -> str:
    def do_connect():
        if title_re:
            return _gui.connect_by_title(title_re, timeout)
        elif process_name:
            return _gui.connect_by_process(process_name, timeout)
        elif pid:
            return _gui.connect_by_pid(pid)
        else:
            return {"ok": False, "error": "Must specify title_re, process_name, or pid"}

    result = do_connect()
    activate_result = None
    if not result["ok"] and auto_activate and ENABLE_POWERSHELL:
        activate_result = _gui.activate_edr()
        time.sleep(3)
        result = do_connect()
        if not result["ok"]:
            result["activate_result"] = activate_result
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
    result = _gui.dump_tree(window_title_re, max_depth=max_depth)
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
    result = _gui.click(
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
    result = _gui.click_target(
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
    result = _gui.click_at(x, y)
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
    result = _gui.click_window_at(x, y, window_title_re)
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
    result = _gui.type_text(control_id, text, class_name, string)
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
    result = _gui.select(control_id, text, class_name, item, index)
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
    result = _gui.get_text(control_id, text, class_name)
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
    result = _gui.screenshot(path)
    return json.dumps(result, ensure_ascii=False)


@mcp.tool(
    name="activate_edr",
    description=(
        "Activate the EDR GUI by launching HisecEndpointAgent with 'cmd ui'. "
        "By default waits up to 15 s for the EDRClient window to appear. "
        "If the window is already open, returns immediately with already_open=true. "
        "exe_path can override the default EDR executable path. "
        "Requires EDR_WD_ENABLE_POWERSHELL=1 on the server."
    ),
)
def activate_edr(exe_path: str = None, wait: bool = True, timeout: float = 15.0,
                 edr_widget_auto_id: str = None) -> str:
    if not ENABLE_POWERSHELL:
        return json.dumps({"ok": False, "error": "PowerShell disabled: set EDR_WD_ENABLE_POWERSHELL=1 to enable"})
    result = _gui.activate_edr(exe_path=exe_path, wait=wait, timeout=timeout,
                               edr_widget_auto_id=edr_widget_auto_id)
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
    result = _gui.list_windows()
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
    result = _gui.is_window_open(title_re=title_re, process_name=process_name, class_name=class_name)
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
    result = _gui.wait_window(
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
    try:
        if _gui.app is None:
            return json.dumps({"ok": False, "error": "Not connected"})

        wins = _gui.app.windows()
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


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="EDR-WD MCP Server")
    parser.add_argument("--http", action="store_true", help="Run in HTTP mode")
    parser.add_argument("--host", default="127.0.0.1", help="HTTP bind host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8765, help="HTTP port (default: 8765)")
    args = parser.parse_args()

    if args.http:
        logger.info(f"Starting HTTP server on {args.host}:{args.port}")
        mcp.run(transport="http", host=args.host, port=args.port)
    else:
        logger.info("Starting stdio server")
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
