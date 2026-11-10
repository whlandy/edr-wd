"""
server.py — fastmcp HTTP Server for cross-platform EDR GUI Automation
======================================================================

Usage:
    # Local stdio
    edr-wd-target

    # HTTP mode (for SSH tunnel / remote access)
    edr-wd-target --http --port 8765

    # Expose beyond localhost only when direct LAN access is required
    edr-wd-target --http --host 0.0.0.0 --port 8765

Backend selection:
    EDR_WD_AUTOMATION_BACKEND=windows_pywinauto    (default; legacy behavior)
    EDR_WD_AUTOMATION_BACKEND=macos_accessibility  (macOS target)
"""

from __future__ import annotations

import argparse
import base64
import functools
import json
import ntpath
import posixpath
import logging
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from fastmcp import FastMCP

try:  # Installed package / `python -m target.server`.
    from .automation import create_backend
    from .automation.base import AutomationBackend
    from .action_catalog import (
        CATALOG_VERSION,
        catalog_digest,
        get_action_catalog as _get_action_catalog_dict,
        status_action_space,
    )
    from .scroll.pointer_result import normalize as _ptr_normalize
    from .scroll.window_args import resolve_window as _resolve_window
    from .scroll.window_args import window_doc as _window_doc
    from .file_transfer import download_chunk, stat_file, upload_chunk
    from .recording.service import (
        RecordingService,
        error_result as _recording_error,
        source_factory_for_backend as _recording_source_factory,
    )
    from .recording.models import RecordingModelError
    from .recording.indicator import tkinter_indicator_factory as _recording_indicator_factory
    from .recording.evidence import capture_redacted_window_frame as _capture_redacted_frame
    from .recording.result_store import RecordingResultStore
    from .recording.ui_controller import (
        RecorderPanelHost as _RecorderPanelHost,
        RecorderUIController as _RecorderUIController,
    )
except ImportError:  # Target-local `python server.py` deployment compatibility.
    from automation import create_backend
    from automation.base import AutomationBackend
    from action_catalog import (
        CATALOG_VERSION,
        catalog_digest,
        get_action_catalog as _get_action_catalog_dict,
        status_action_space,
    )
    from scroll.pointer_result import normalize as _ptr_normalize
    from scroll.window_args import resolve_window as _resolve_window
    from scroll.window_args import window_doc as _window_doc
    from file_transfer import download_chunk, stat_file, upload_chunk
    from recording.service import (
        RecordingService,
        error_result as _recording_error,
        source_factory_for_backend as _recording_source_factory,
    )
    from recording.models import RecordingModelError
    from recording.indicator import tkinter_indicator_factory as _recording_indicator_factory
    from recording.evidence import capture_redacted_window_frame as _capture_redacted_frame
    from recording.result_store import RecordingResultStore
    from recording.ui_controller import (
        RecorderPanelHost as _RecorderPanelHost,
        RecorderUIController as _RecorderUIController,
    )

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


# The unified dispatcher resolves the backend through a registered factory
# rather than importing this module, which would be circular. Without this
# registration every execute_action call fails with dispatch_target_missing —
# the dispatcher is the only path replay has, so it must be wired at import,
# not lazily on first use.
try:
    from .action_dispatcher import set_backend_resolver as _set_backend_resolver
except ImportError:  # Target-local deployment.
    from action_dispatcher import set_backend_resolver as _set_backend_resolver

_set_backend_resolver(lambda: _backend)

try:
    from . import product_policy as _product_policy
    from .action_dispatcher import cache_clear as _clear_dispatch_cache
except ImportError:  # target-local deployment
    import product_policy as _product_policy
    from action_dispatcher import cache_clear as _clear_dispatch_cache


def _clear_product_run_state() -> dict:
    """Run-lifetime cleanup: called on end/expiry/fault. Every step runs
    independently (finally/stepwise) so a failing unlock can never skip
    dropping THIS run's session, observation and binding memory; the
    unlock error is preserved for the caller, never swallowed
    (review 1845: an unlock RuntimeError left the old facade and its
    binding/token maps in place)."""
    global _window_session, _product_observation
    errors: list[str] = []
    try:
        _clear_dispatch_cache()
    except Exception as exc:  # noqa: BLE001 — cleanup must always continue
        errors.append(f"dispatch cache: {type(exc).__name__}: {exc}")
    try:
        if _backend is not None and hasattr(_backend, "unlock_window"):
            _backend.unlock_window()
    except Exception as exc:  # noqa: BLE001 — preserved, never swallowed
        errors.append(f"unlock_window: {type(exc).__name__}: {exc}")
    finally:
        # Binding memory is dropped even when the unlock raised — clearing
        # this run's state is unconditional and touches no other run.
        facade = _binding_facade_holder.get("facade")
        if facade is not None:
            try:
                facade.clear_run_state()
            except Exception as exc:  # noqa: BLE001
                errors.append(f"binding cleanup: {type(exc).__name__}: {exc}")
        _binding_facade_holder["facade"] = None
        if _binding_facade_holder.get("tracker") is not None:
            try:
                stop_result = _binding_facade_holder["tracker"].stop()
                # 结构化失败(unhook 失败/投递失败/worker 未退)透传:
                if isinstance(stop_result, dict) and \
                        stop_result.get("ok") is False:
                    errors.extend(stop_result.get("errors") or [])
            except Exception as exc:  # noqa: BLE001
                errors.append(f"tracker stop: {type(exc).__name__}: {exc}")
            _binding_facade_holder["tracker"] = None
        _window_session = None
        _product_observation = None
    return {"errors": errors}


# Registration is idempotent across module reloads: each reload of this
# module binds a NEW function object, so a plain append would stack one
# cleanup pass per reload (N reloads → N unlock calls during teardown).
_product_policy.replace_cleanup_hook(_clear_product_run_state)
if _backend is not None:
    _backend = _product_policy.GuardedBackend(_backend, _backend_kind)
_window_session = None
_product_observation = None
_gui_operation_mutex = threading.RLock()


def _serialized_product_gui(func):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        with _gui_operation_mutex:
            return func(*args, **kwargs)
    return wrapper


# Server bind metadata. Updated by main() so status() can report the actual
# runtime port instead of assuming the default 8765.
_server_host = "127.0.0.1"
_server_port = 8765

_recording_result_store = RecordingResultStore(
    Path(os.environ.get("EDR_WD_RECORDING_RESULTS",
                        str(Path.home() / ".edr-wd" / "recording-results")))
)

_recording_service = RecordingService(
    target_name=os.environ.get("EDR_WD_TARGET_NAME", "target"),
    backend=_backend_kind,
    source_factory=_recording_source_factory(_backend_kind, _backend),
    indicator_factory=_recording_indicator_factory,
    backend_object=_backend,
    result_store=_recording_result_store,
)

# Recorder panel (U1): the service-level entry on the interactive desktop.
# The backend stays None when the platform failed to load; window listing
# then reports a structured error instead of half-working.
_recording_ui_controller = _RecorderUIController(
    _recording_service, backend=_backend)
_recording_panel_host = _RecorderPanelHost(_recording_ui_controller)


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


def _legacy_gui_conflict(tool_name: str) -> str | None:
    try:
        _product_policy.authorize_legacy_gui_call(tool_name)
    except ValueError:
        return json.dumps({"ok": False, "code": "product_run_conflict",
                           "error": "legacy GUI operation is disabled during an active product run"},
                          ensure_ascii=False)
    return None


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
    handle: int | None = None,
) -> str:
    if _backend is None:
        return json.dumps({"ok": False, "error": "Automation backend not initialized"})
    if handle is not None:
        # Exact live selection for a recording agent when same-title menus
        # coexist. Never pick the first title match or launch another app.
        if (type(handle) is not int or handle <= 0 or type(pid) is not int or pid <= 0
                or not process_name or auto_activate or app_name or bundle_id):
            return json.dumps({'ok': False, 'code': 'exact_connect_context_required'})
        if (blocked := _legacy_gui_conflict('connect')) is not None:
            return blocked
        with _gui_operation_mutex:
            inventory = _backend.list_windows()
            candidates = [w for w in inventory.get('windows', [])
                          if w.get('handle') == handle and w.get('process_id') == pid
                          and w.get('visible') is True]
            actual = _backend.process_name_for_pid(pid)
            if (inventory.get('ok') is not True or len(candidates) != 1
                    or not actual or actual.casefold() != process_name.casefold()
                    or (title_re and not re.search(title_re, candidates[0].get('title') or ''))):
                return json.dumps({'ok': False, 'code': 'exact_connect_identity_mismatch'})
            exact = getattr(_backend, 'connect_window_identity', None)
            if not callable(exact):
                return json.dumps({'ok': False, 'code': 'exact_connect_unsupported'})
            return json.dumps(exact(handle, pid), ensure_ascii=False)

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
    if not result["ok"] and auto_activate and hasattr(_backend, "activate_edr"):
        activate_result = _backend.activate_edr()  # type: ignore[attr-defined]
        time.sleep(3)
        result = do_connect()
        if not result["ok"]:
            result["activate_result"] = activate_result
    return json.dumps(result, ensure_ascii=False)


@mcp.tool(
    name="lock_window",
    description=(
        "Lock subsequent click/drag/scroll actions to the connected or specified window. "
        "When a lock exists, pointer actions verify the active/frontmost window before executing."
    ),
)
def lock_window(
    title_re: str = None,
    process_name: str = None,
    pid: int = None,
    strict: bool = True,
    activate: bool = True,
) -> str:
    if _backend is None:
        return _backend_unavailable("lock_window")
    result = _backend.lock_window(
        title_re=title_re,
        process_name=process_name,
        pid=pid,
        strict=strict,
        activate=activate,
    )
    return json.dumps(result, ensure_ascii=False)


@mcp.tool(
    name="unlock_window",
    description="Clear the current target window lock.",
)
def unlock_window() -> str:
    if _backend is None:
        return _backend_unavailable("unlock_window")
    result = _backend.unlock_window()
    return json.dumps(result, ensure_ascii=False)


@mcp.tool(
    name="get_window_lock",
    description="Return the current target window lock snapshot, if any.",
)
def get_window_lock() -> str:
    if _backend is None:
        return _backend_unavailable("get_window_lock")
    result = _backend.get_window_lock()
    return json.dumps(result, ensure_ascii=False)


@mcp.tool(
    name="verify_window_lock",
    description=(
        "Verify the active/frontmost window still matches the current lock. "
        "If activate=true, the backend may try to bring the locked window forward once."
    ),
)
def verify_window_lock(activate: bool = True) -> str:
    if _backend is None:
        return _backend_unavailable("verify_window_lock")
    result = _backend.verify_window_lock(activate=activate)
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
    name="activate_product_app",
    description="Activate a locally registered product using its target-owned app catalog entry.",
)
@_serialized_product_gui
def activate_product_app(profile_id: str) -> str:
    if _backend is None:
        return _backend_unavailable("activate_product_app")
    try:
        _product_policy.require_product_run(None)
        capability = _product_policy.load_allowed_profiles().get(profile_id)
        if capability is None or capability.backend != _backend_kind:
            raise ValueError("product_profile_unknown")
        try:
            from .product_profiles import INSTALLED_PRODUCT_SPECS
        except ImportError:  # target-local deployment
            from product_profiles import INSTALLED_PRODUCT_SPECS
        application = INSTALLED_PRODUCT_SPECS[(profile_id, _backend_kind)]["application"]
        path = application["path"]
        if _backend_kind == "windows_pywinauto":
            executable = ntpath.basename(path)
            owners = {name.casefold() for role in capability.window_roles
                      for name in role.process_names}
            if executable.casefold() not in owners:
                raise ValueError("product_application_owner_mismatch")
            result = _backend.activate_registered_app(
                executable, _resolve=lambda _name: path)
        elif _backend_kind == "macos_accessibility":
            bundle_id = application.get("bundle_id")
            app_name = application.get("app_name")
            if not bundle_id and not app_name:
                raise ValueError("product_application_activation_missing")
            result = _backend.activate_app(app_name=app_name, bundle_id=bundle_id)
        else:
            raise ValueError("product_profile_backend_invalid")
    except (KeyError, ValueError) as exc:
        code = str(exc).split(":", 1)[0]
        return json.dumps({"ok": False, "code": code}, ensure_ascii=False)
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
    name="find_control",
    description=(
        "Find controls/components in the connected window by text, role, identifier, or title regex. "
        "Windows resolves this through dump_tree/UIA; macOS resolves it through System Events Accessibility."
    ),
)
def find_control(
    text: str = None,
    role: str = None,
    identifier: str = None,
    title_re: str = None,
    max_depth: int = 10,
) -> str:
    if _backend is None:
        return _backend_unavailable("find_control")
    if not hasattr(_backend, "find_control"):
        return json.dumps({
            "ok": False,
            "error": f"find_control is not supported by the {type(_backend).__name__} backend",
        })
    result = _backend.find_control(
        text=text,
        role=role,
        identifier=identifier,
        title_re=title_re,
        max_depth=max_depth,
    )
    return json.dumps(result, ensure_ascii=False)


@mcp.tool(
    name="click",
    description=(
        "Click a control by control_id, text, class_name, automation_id, or derived filters. "
        "control_id is the preferred identifier (unique within the window). "
        "New: auto_id_contains, auto_id_suffix, parent_of, control_type for flexible lookup. "
        "parent_fallback=True (default): if the found control is a Static/Text/Label/Pane leaf, "
        "click its parent container automatically — use this for Qt 'card button' patterns. "
        "For HiSec, always set expected_process_name to HisecEndpointAgent(.exe) or "
        "EDRClient(.exe) so clicks cannot cross application windows."
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
    expected_process_name: str = None,
) -> str:
    if _backend is None:
        return _backend_unavailable("click")
    result = _backend.click(
        control_id, text, class_name, parent_text, automation_id,
        auto_id_contains, auto_id_suffix, parent_of, control_type,
        parent_fallback, expected_process_name,
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
        "and parent_fallback=True (default, redirects Static/Text/Label/Pane to parent). "
        "HiSec calls must set expected_process_name to distinguish the entry and client windows."
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
    expected_process_name: str = None,
) -> str:
    if _backend is None:
        return _backend_unavailable("click_target")
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
        expected_process_name=expected_process_name,
    )
    return json.dumps(result, ensure_ascii=False)


@mcp.tool(
    name="click_at",
    description=(
        "Click absolute screen coordinates (x, y on the screen). "
        "Use when you have screen-space coordinates, e.g. from dump_tree window_rectangle + control rectangle center."
    ),
)
def click_at(x: int, y: int, expected_process_name: str = None) -> str:
    if _backend is None:
        return _backend_unavailable("click_at")
    result = _ptr_normalize(
        _backend.click_at(x, y, expected_process_name),
        tool="click_at",
    )
    return json.dumps(result, ensure_ascii=False)


@mcp.tool(
    name="double_click_at",
    description=(
        "Double click absolute screen coordinates (x, y). "
        "Useful as an action-space primitive when a semantic selector is unavailable."
    ),
)
def double_click_at(x: int, y: int, expected_process_name: str = None) -> str:
    if _backend is None:
        return _backend_unavailable("double_click_at")
    result = _backend.double_click_at(x, y, expected_process_name)
    return json.dumps(result, ensure_ascii=False)


@mcp.tool(
    name="right_click_at",
    description=(
        "Right click absolute screen coordinates (x, y). "
        "Useful for context menus and fallback action-space automation."
    ),
)
def right_click_at(x: int, y: int, expected_process_name: str = None) -> str:
    if _backend is None:
        return _backend_unavailable("right_click_at")
    result = _backend.right_click_at(x, y, expected_process_name)
    return json.dumps(result, ensure_ascii=False)


@mcp.tool(
    name="middle_click_at",
    description=(
        "Middle click absolute screen coordinates (x, y). "
        "Useful as an action-space primitive for tabs and custom widgets."
    ),
)
def middle_click_at(x: int, y: int, expected_process_name: str = None) -> str:
    if _backend is None:
        return _backend_unavailable("middle_click_at")
    result = _backend.middle_click_at(x, y, expected_process_name)
    return json.dumps(result, ensure_ascii=False)


@mcp.tool(
    name="hover_at",
    description=(
        "Move the mouse to absolute coordinates (x, y) without clicking. "
        "Useful for tooltips, hover reveals, and drag pre-positioning."
    ),
)
def hover_at(x: int, y: int) -> str:
    if _backend is None:
        return _backend_unavailable("hover_at")
    result = _backend.hover_at(x, y)
    return json.dumps(result, ensure_ascii=False)


@mcp.tool(
    name="drag",
    description=(
        "Drag the mouse from (x1, y1) to (x2, y2). "
        "Use for sliders, list reordering, selection rectangles, and drag-and-drop."
    ),
)
def drag(x1: int, y1: int, x2: int, y2: int, duration: float = 0.25) -> str:
    if _backend is None:
        return _backend_unavailable("drag")
    result = _ptr_normalize(
        _backend.drag(x1, y1, x2, y2, duration=duration),
        tool="drag",
    )
    return json.dumps(result, ensure_ascii=False)


@mcp.tool(
    name="scroll",
    description=(
        "LOW-LEVEL RAW PRIMITIVE: scroll the mouse wheel by a signed number of "
        "clicks. UNSCOPED — it acts on whatever top-level window currently owns "
        "the pointer; there is no window binding or ownership check. Optional "
        "x/y can reposition the pointer before scrolling (screen coords). "
        "For window-bound scroll with resolution + verification, use "
        "'scroll_window' instead."
    ),
)
def scroll(clicks: int, x: int = None, y: int = None) -> str:
    if _backend is None:
        return _backend_unavailable("scroll")
    result = _ptr_normalize(
        _backend.scroll(clicks, x=x, y=y),
        tool="scroll",
    )
    return json.dumps(result, ensure_ascii=False)


@mcp.tool(
    name="scroll_window",
    description=(
        "Window-scoped scroll (P0.1): scroll the mouse wheel inside a specific "
        "window by a signed number of clicks. Coordinates (x, y) are WINDOW-"
        "RELATIVE to the resolved window's top-left corner. The window is "
        "resolved uniquely from the `window` object (title_re / process_name / "
        "pid / handle) or the legacy window_title_re + expected_process_name + "
        "optional expected_pid, and the converted screen point is verified to "
        "belong to that window and NOT be occluded by another top-level window "
        "before any pointer movement. Stable error codes on failure: "
        "target_not_found / target_ambiguous / point_outside_window / "
        "target_occluded. Catalog action: pointer.scroll (A029). "
        + _window_doc("Selects the target window.")
    ),
)
def scroll_window(
    clicks: int,
    x: int,
    y: int,
    window: dict = None,
    window_title_re: str = None,
    expected_process_name: str = None,
    expected_pid: int = None,
) -> str:
    if _backend is None:
        return _backend_unavailable("scroll_window")
    _w = _resolve_window(
        window,
        window_title_re=window_title_re,
        expected_process_name=expected_process_name,
        expected_pid=expected_pid,
    )
    if hasattr(_backend, "scroll_window"):
        result = _ptr_normalize(
            _backend.scroll_window(
                clicks,
                x,
                y,
                window_title_re=_w["window_title_re"],
                expected_process_name=_w["process_name"],
                expected_pid=_w["pid"],
            ),
            tool="scroll_window",
            scope="window",
        )
    else:
        result = _ptr_normalize(
            {
                "ok": False,
                "code": "backend_unsupported",
                "error": "current backend does not implement window-scoped scroll",
            },
            tool="scroll_window",
            scope="window",
        )
    return json.dumps(result, ensure_ascii=False)


@mcp.tool(
    name="scroll_region",
    description=(
        "Detect the connected window's scroll structure and dispatch ONE bounded "
        "scroll/next action through the full composite chain "
        "(detect -> controller.plan -> real dispatcher -> verify). Unlike the raw "
        "'scroll' primitive, this never fabricates movement: it returns a "
        "ScrollResult (dispatched / moved / reason) and reports NOT_DISPATCHED "
        "when no enabled owner control exists or the backend is unavailable. "
        "Optional `window` object (or legacy window_title_re) overrides the "
        "currently connected window. "
        + _window_doc("Selects the target window.")
    ),
)
def scroll_region(
    window: dict = None,
    window_title_re: str = None,
    max_depth: int = 6,
    process_name: str = None,
    pid: int = None,
) -> str:
    """One bounded scroll/next cycle through the full chain.

    `run_scroll_region` converts the *expected* domain outcomes (backend
    unavailable, no targets/controls) into honest NOT_DISPATCHED results
    internally — it does not raise for those. Any exception escaping here is a
    genuine bug, so we let it propagate (MCP framework logs the true
    traceback) instead of masking it as a business `ok:false` payload, which
    would hide TypeError/AttributeError-style crashes from callers.

    DO NOT wrap this body in `except Exception -> ok:false`. There is no
    "known MCP error" to route to here: BackendUnavailableError and the
    no-controls ValueError are both consumed inside run_scroll_region, so any
    `except` we add today would be dead code, and tomorrow it would silently
    swallow real crashes. If you believe a NEW expected failure can reach this
    point, add a narrow `except <ThatSpecificError>:` (with a test) — never a
    blanket one. (See review: "tighten server exception handling".)
    """
    if _backend is None:
        return _backend_unavailable("scroll_region")
    from scroll.backend import ScrollBackendSource, run_scroll_region

    _w = _resolve_window(
        window,
        window_title_re=window_title_re,
        process_name=process_name,
        pid=pid,
    )
    source = ScrollBackendSource(
        _backend,
        window_title_re=_w["window_title_re"],
        max_depth=max_depth,
        process_name=_w["process_name"],
        pid=_w["pid"],
        native_window_id=_w["native_window_id"],
        host=os.environ.get("EDR_WD_HOST", "local"),
    )
    result = run_scroll_region(source)
    result["ok"] = True
    return json.dumps(result, ensure_ascii=False)


@mcp.tool(
    name="page_table",
    description=(
        "Turn ONE page of a paged table via a semantic pagination click only "
        "(gui.click / A020). Never emits pointer.scroll (A029) or pointer.drag "
        "(A028). Confirms the surface is PAGINATED or LOAD_MORE via PageDetector; "
        "returns NOT_DISPATCHED for FLAT/INFINITE/TREE_LAZY, a disabled "
        "next/prev, or a missing enabled owner. Verifies real page movement "
        "(content digest change) before reporting moved=True. direction: "
        "next|prev|first. One call = one page turn; draining all pages is a "
        "caller loop. Optional `window` object (or legacy window_title_re) "
        "selects the target window. "
        + _window_doc("Selects the target window.")
    ),
)
def page_table(
    direction: str = "next",
    window: dict = None,
    window_title_re: str = None,
    max_depth: int = 6,
    process_name: str = None,
    pid: int = None,
    verify: bool = True,
    max_attempts: int = None,
) -> str:
    """One semantic page turn through the paged-table-only composite chain.

    Mirrors `scroll_region`'s honest-error contract: backend-unavailable and
    no-controls outcomes are converted into NOT_DISPATCHED inside
    `run_page_table`. Any exception escaping here is a genuine bug and should
    propagate (see scroll_region docstring) — never a blanket `except`.
    """
    if _backend is None:
        return _backend_unavailable("page_table")
    from scroll.backend import ScrollBackendSource, run_page_table

    _w = _resolve_window(
        window,
        window_title_re=window_title_re,
        process_name=process_name,
        pid=pid,
    )
    source = ScrollBackendSource(
        _backend,
        window_title_re=_w["window_title_re"],
        max_depth=max_depth,
        process_name=_w["process_name"],
        pid=_w["pid"],
        native_window_id=_w["native_window_id"],
        host=os.environ.get("EDR_WD_HOST", "local"),
    )
    result = run_page_table(
        source,
        direction=direction,
        verify=verify,
        max_attempts=max_attempts,
    )
    result["ok"] = True
    return json.dumps(result, ensure_ascii=False)


@mcp.tool(
    name="scroll_until_visible",
    description=(
        "Target-driven bounded scroll: advance one direction (down|up) until a "
        "control whose text matches target_text_re is observed in the tree. "
        "Probes BEFORE the first dispatch — a target already visible performs "
        "ZERO input (reason=target_visible). Stops immediately on "
        "NOT_DISPATCHED or (verify=True) NO_SCROLL_EFFECT; counts only "
        "moved-but-unmatched steps against max_steps and terminates honestly "
        "with target_not_found when the bound is exhausted. down->next->negative "
        "wheel / pagination next; up->prev->positive wheel / pagination prev. "
        "Aligns action codes with the page_table/scroll semantics (A020 / A029). "
        "verify=False never reports moved=True. Optional `window` object (or "
        "legacy window_title_re) selects the target window. "
        + _window_doc("Selects the target window.")
    ),
)
def scroll_until_visible(
    target_text_re: str,
    direction: str = "down",
    window: dict = None,
    window_title_re: str = None,
    max_depth: int = 6,
    process_name: str = None,
    pid: int = None,
    verify: bool = True,
    max_steps: int = 8,
) -> str:
    """Scroll one direction until a text-identified target appears (bounded).

    Composes the real chain (snapshot -> detect -> PageController plan ->
    dispatcher -> probe) via `run_scroll_until_visible`. Backend-unavailable
    and no-controls outcomes are converted to NOT_DISPATCHED inside the
    facade; any exception escaping here is a genuine bug and should propagate.
    """
    if _backend is None:
        return _backend_unavailable("scroll_until_visible")
    from scroll.backend import ScrollBackendSource
    from scroll.scroll_until_visible import run_scroll_until_visible

    _w = _resolve_window(
        window,
        window_title_re=window_title_re,
        process_name=process_name,
        pid=pid,
    )
    source = ScrollBackendSource(
        _backend,
        window_title_re=_w["window_title_re"],
        max_depth=max_depth,
        process_name=_w["process_name"],
        pid=_w["pid"],
        native_window_id=_w["native_window_id"],
        host=os.environ.get("EDR_WD_HOST", "local"),
    )
    result = run_scroll_until_visible(
        source,
        target_text_re=target_text_re,
        direction=direction,
        max_steps=max_steps,
        verify=verify,
    )
    result["ok"] = True
    return json.dumps(result, ensure_ascii=False)


@mcp.tool(
    name="drag_target",
    description=(
        "One window/ownership-verified drag (A028 pointer.drag) from a resolved "
        "control to a release point. Resolves target_ref (target_id / automation_id "
        "/ text / control_type, ownership-gated by expected_process_name) to a "
        "control rectangle, grabs a safe point (grab=handle for thumb/slider/splitter, "
        "grab=center otherwise) and drags to either an absolute (x2, y2) or a "
        "(dx, dy) offset from the grab point. Verifies actual movement via the "
        "Observer (never reports moved=True without a verified change). For sliders, "
        "handles, splitter bars, resize grips and precise list reordering. "
        "Dispatches exactly one pointer.drag (A028); never a wheel or click. "
        "verify=False never reports moved=True. Optional `window` object (or "
        "legacy window_title_re / process_name / pid) selects the target "
        "window. "
        + _window_doc("Selects the target window.")
    ),
)
def drag_target(
    target_ref: dict,
    dx: int = None,
    dy: int = None,
    x2: int = None,
    y2: int = None,
    grab: str = "handle",
    duration: float = 0.25,
    window: dict = None,
    window_title_re: str = None,
    max_depth: int = 6,
    process_name: str = None,
    pid: int = None,
    verify: bool = True,
    expected_process_name: str = None,
) -> str:
    """Drag one control to a release point (A028), window/ownership verified.

    Mirrors the sibling composite tools' honest-error contract:
    backend-unavailable, no-controls and resolution-failure outcomes are
    converted to NOT_DISPATCHED inside the facade; any exception escaping
    here is a genuine bug and should propagate.
    """
    if _backend is None:
        return _backend_unavailable("drag_target")
    from scroll.backend import ScrollBackendSource
    from scroll.drag import run_drag_target

    _w = _resolve_window(
        window,
        window_title_re=window_title_re,
        process_name=process_name,
        pid=pid,
        expected_process_name=expected_process_name,
    )
    source = ScrollBackendSource(
        _backend,
        window_title_re=_w["window_title_re"],
        max_depth=max_depth,
        process_name=_w["process_name"],
        pid=_w["pid"],
        native_window_id=_w["native_window_id"],
        host=os.environ.get("EDR_WD_HOST", "local"),
    )
    result = run_drag_target(
        source,
        target_ref=target_ref,
        dx=dx,
        dy=dy,
        x2=x2,
        y2=y2,
        grab=grab,
        duration=duration,
        verify=verify,
        expected_process_name=_w["process_name"],
    )
    result["ok"] = True
    return json.dumps(result, ensure_ascii=False)


@mcp.tool(
    name="click_window_at",
    description=(
        "Click window-relative coordinates (x, y) converted to screen space using the connected window's rectangle. "
        "Use when you have coordinates relative to the window's top-left corner. "
        "If no `window` object or window_title_re is given, uses the currently connected window. "
        + _window_doc("Selects the target window.")
    ),
)
def click_window_at(
    x: int,
    y: int,
    window: dict = None,
    window_title_re: str = None,
    expected_process_name: str = None,
) -> str:
    if _backend is None:
        return _backend_unavailable("click_window_at")
    _w = _resolve_window(
        window,
        window_title_re=window_title_re,
        expected_process_name=expected_process_name,
    )
    result = _ptr_normalize(
        _backend.click_window_at(
            x,
            y,
            _w["window_title_re"],
            _w["process_name"],
        ),
        tool="click_window_at",
    )
    return json.dumps(result, ensure_ascii=False)


@mcp.tool(
    name="type_text",
    description=(
        "Type text into an edit control. "
        "Finds the control by automation_id (+ control_type), control_id "
        "(preferred), text, or class_name, then sets the text content."
    ),
)
def type_text(
    control_id: int = None,
    text: str = None,
    class_name: str = None,
    string: str = "",
    automation_id: str = None,
    control_type: str = None,
) -> str:
    if _backend is None:
        return _backend_unavailable("type_text")
    # kwargs 调用：适配层首参 `text` 是输入值（统一 backend 契约），而 MCP
    # legacy 的 `text` 是定位文本、`string` 是输入值——位置传参会错绑。
    result = _backend.type_text(
        text=text, control_id=control_id, class_name=class_name,
        string=string, automation_id=automation_id, control_type=control_type,
    )
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
    if _backend is None:
        return _backend_unavailable("select")
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
    if _backend is None:
        return _backend_unavailable("get_text")
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


# ---------------------------------------------------------------------------
# Sandboxed file transfer
# ---------------------------------------------------------------------------

@mcp.tool(
    name="transfer_upload",
    description=(
        "Upload one base64-encoded file chunk to the target transfer sandbox. "
        "Paths are relative to EDR_WD_TRANSFER_DIR; absolute paths, traversal, "
        "and symlinks are rejected. Start at offset=0, use overwrite=true only "
        "when replacement is intended, then send contiguous offsets. Set final=true "
        "and expected_sha256 on the last chunk to verify the complete file."
    ),
)
def transfer_upload(
    relative_path: str,
    content_base64: str,
    offset: int = 0,
    overwrite: bool = False,
    final: bool = False,
    expected_sha256: str = None,
) -> str:
    return json.dumps(upload_chunk(
        relative_path,
        content_base64,
        offset=offset,
        overwrite=overwrite,
        final=final,
        expected_sha256=expected_sha256,
    ), ensure_ascii=False)


@mcp.tool(
    name="transfer_download",
    description=(
        "Download one base64-encoded chunk from the target transfer sandbox. "
        "Use next_offset until eof=true. The final response includes SHA-256. "
        "A single chunk is limited to 1 MiB."
    ),
)
def transfer_download(
    relative_path: str,
    offset: int = 0,
    max_bytes: int = 256 * 1024,
) -> str:
    return json.dumps(download_chunk(
        relative_path, offset=offset, max_bytes=max_bytes,
    ), ensure_ascii=False)


@mcp.tool(
    name="transfer_stat",
    description=(
        "Return size, SHA-256, and modification time for a file in the target "
        "transfer sandbox. No absolute target path is exposed."
    ),
)
def transfer_stat(relative_path: str) -> str:
    return json.dumps(stat_file(relative_path), ensure_ascii=False)


# ---------------------------------------------------------------------------
# Scoped desktop recording management
# ---------------------------------------------------------------------------

@mcp.tool(name="start_recording", description="Start a target-local, process/window-scoped desktop recording session. Fails if the platform capture source or required permissions are unavailable.")
def start_recording(name: str, process_name: str, window_title: str, lease_seconds: float = 300.0) -> str:
    if (blocked := _legacy_gui_conflict("start_recording")) is not None:
        return blocked
    try:
        result = _recording_service.start(name=name, process_name=process_name, window_title=window_title, lease_seconds=lease_seconds)
    except Exception as exc:
        result = _recording_error(exc)
    return json.dumps(result, ensure_ascii=False)


@mcp.tool(name="recording_status", description="Return recording state and optionally renew the target-local capture lease.")
def recording_status(heartbeat: bool = False) -> str:
    try:
        result = _recording_service.status(heartbeat=heartbeat)
    except Exception as exc:
        result = _recording_error(exc)
    return json.dumps(result, ensure_ascii=False)


@mcp.tool(name="pause_recording", description="Pause the active recording. Paused input is not persisted.")
def pause_recording() -> str:
    try: result = _recording_service.pause()
    except Exception as exc: result = _recording_error(exc)
    return json.dumps(result, ensure_ascii=False)


@mcp.tool(name="resume_recording", description="Resume the paused recording and renew its lease.")
def resume_recording() -> str:
    try: result = _recording_service.resume()
    except Exception as exc: result = _recording_error(exc)
    return json.dumps(result, ensure_ascii=False)


@mcp.tool(name="add_recording_assertion", description="Open the target-local explicit assertion editor for the active recording.")
def add_recording_assertion() -> str:
    try: result = _recording_service.assertion()
    except Exception as exc: result = _recording_error(exc)
    return json.dumps(result, ensure_ascii=False)


@mcp.tool(name="stop_recording", description="Stop recording and return the strict raw recording document.")
def stop_recording() -> str:
    try: result = _recording_service.stop()
    except Exception as exc: result = _recording_error(exc)
    return json.dumps(result, ensure_ascii=False)


@mcp.tool(name="get_recording_capture", description="Fetch one source-redacted PNG referenced by a stopped recording event. Memory first; on a miss the durable per-session store is consulted. session_id pins the owning session (recommended); the legacy no-session call only resolves when the capture id has exactly one unambiguous owner.")
def get_recording_capture(capture_id: str, session_id: str | None = None) -> str:
    try: result = _recording_service.capture(capture_id=capture_id, session_id=session_id)
    except Exception as exc: result = _recording_error(exc)
    return json.dumps(result, ensure_ascii=False)


@mcp.tool(name="open_recording_ui", description="Show or restore the target-local recorder panel on the interactive desktop. Returns the current panel snapshot; fails with interactive_session_unavailable when no visible desktop exists.")
def open_recording_ui() -> str:
    if (blocked := _legacy_gui_conflict("open_recording_ui")) is not None:
        return blocked
    try: result = _recording_panel_host.start()
    except Exception as exc: result = _recording_error(exc)
    return json.dumps(result, ensure_ascii=False)


@mcp.tool(
    name="region_state_query",
    description=(
        "F44 read-only probe of the region-assertion flow state on the "
        "interactive desktop: whether the selection mask is open, whether a "
        "drag selection has been completed, and the controller state literal. "
        "No side effects. Use it to decide when to call region_confirm_action."
    ),
)
def region_state_query() -> str:
    try:
        result = _recording_ui_controller.apply_ui_request(
            "region_state", {})
    except Exception as exc:
        result = _recording_error(exc)
    return json.dumps(result, ensure_ascii=False)


@mcp.tool(
    name="region_confirm_action",
    description=(
        "F44 API-driven confirmation of the frozen drag selection in the "
        "region-assertion flow. Equivalent to the confirm button, exposed "
        "because the local OS/Tk input stack swallows real button/keyboard "
        "events (root cause unknown, reported honestly). The drag selection "
        "itself must remain real hardware input; this endpoint only advances "
        "the already-captured selection. Returns the frozen selection."
    ),
)
def region_confirm_action() -> str:
    import uuid as _uuid
    request_id = f"api-{_uuid.uuid4().hex[:8]}"
    try:
        result = _recording_ui_controller.apply_ui_request(
            "region_confirm", {"requestId": request_id})
    except Exception as exc:
        result = _recording_error(exc)
    # F50/F52：确认应答按面板既有 render 路径推送——遮罩关闭+向导打开=
    # 真实面板 UI 流（状态驱动的面板自身决策，非伪造输入）。
    # F52：push 结果如实入响应（panelNotified），不再静默吞——诊断链可见。
    try:
        region = (result or {}).get("region") or {}
        if isinstance(result, dict) and result.get("ok") is True \
                and region.get("state") == "confirmed":
            pushed = _recording_panel_host.push_to_panel(
                {"ack": request_id, "result": result})
            if isinstance(result, dict):
                # F73：严格 token==request_id 才透传分层（防串号）
                _pr = getattr(_recording_panel_host, "_last_push_result", None)
                _tok_ok = isinstance(_pr, dict) and _pr.get("token") == request_id
                _push_ok = bool(pushed) if not isinstance(pushed, dict) else bool(pushed.get("ok"))
                result["panelNotified"] = _push_ok
                if _tok_ok:
                    # F72：分层透传——deliveryAck（送达）/rendered（渲染完
                    # 成）/wizardPhase（向导就绪/失败）不混同，调用者不得把
                    # 送达当完成。
                    result["panelDeliveryAck"] = bool(_pr.get("deliveryAck"))
                    result["panelRendered"] = bool(_pr.get("rendered"))
                    result["panelWizardPhase"] = _pr.get("wizardPhase")
                if not _push_ok:
                    # F73：按真实 ok/失败填写（dict/bool 双兼容）
                    result["panelPushReason"] = (
                        getattr(_recording_panel_host, "_last_send_error", None)
                        or "ack-timeout-no-echo")
    except Exception as _push_exc:
        if isinstance(result, dict):
            result["panelNotified"] = False
            result["panelPushError"] = str(_push_exc)
    return json.dumps(result, ensure_ascii=False)


@mcp.tool(
    name="region_save_action",
    description=(
        "F44 API-driven save of the region assertion wizard draft "
        "(equivalent to the wizard save button) for environments whose "
        "local input stack is unavailable. Pass the draft as a JSON object "
        "with targetMode/quantifier/fields exactly as the wizard would emit."
    ),
)
def region_save_action(draft: str) -> str:
    import uuid as _uuid
    try:
        parsed = json.loads(draft) if isinstance(draft, str) else draft
        if not isinstance(parsed, dict):
            raise ValueError("draft_must_be_json_object")
        result = _recording_ui_controller.apply_ui_request(
            "region_save",
            {"requestId": f"api-{_uuid.uuid4().hex[:8]}", "draft": parsed})
    except Exception as exc:
        result = _recording_error(exc)
    return json.dumps(result, ensure_ascii=False)


@mcp.tool(
    name="region_update_action",
    description=(
        "F49 API-driven completion of the region drag selection: fix the "
        "rubber-band rect (left, top, right, bottom) that the real hardware "
        "drag captured on the interactive desktop, and preselect the "
        "intersecting components. Equivalent to the release frame; exposed "
        "because the local input-state query layer is broken (honestly "
        "reported). The drag itself must have really happened."
    ),
)
def region_update_action(left: int, top: int, right: int, bottom: int) -> str:
    import uuid as _uuid
    try:
        result = _recording_ui_controller.apply_ui_request(
            "region_update",
            {"requestId": f"api-{_uuid.uuid4().hex[:8]}",
             "rect": [int(left), int(top), int(right), int(bottom)]})
    except Exception as exc:
        result = _recording_error(exc)
    return json.dumps(result, ensure_ascii=False)


@mcp.tool(
    name="region_toggle_action",
    description=(
        "F51 API-driven selection of the container (or one component) inside "
        "the frozen drag rect. With container=true the largest recognizable "
        "container (list/table/panel) intersecting the rect is selected — "
        "required before region_save_action so the saved draft carries a "
        "stable container identity (the compiler refuses a region query "
        "without one)."
    ),
)
def region_toggle_action(container: bool, key: str = "") -> str:
    import uuid as _uuid
    try:
        result = _recording_ui_controller.apply_ui_request(
            "region_toggle",
            {"requestId": f"api-{_uuid.uuid4().hex[:8]}",
             "container": bool(container), "key": str(key or "")})
    except Exception as exc:
        result = _recording_error(exc)
    return json.dumps(result, ensure_ascii=False)


@mcp.tool(name="get_recording_result", description="Return one stopped recording's durable, source-redacted raw document by its own session id. Sessions are isolated by the storage key; an unknown id is reported as missing, never as another session's data.")
def get_recording_result(session_id: str) -> str:
    try: result = _recording_service.get_result(session_id=session_id)
    except Exception as exc: result = _recording_error(exc)
    return json.dumps(result, ensure_ascii=False)


@mcp.tool(name="list_recording_results", description="List session ids holding durable, uncollected stopped recordings on this target.")
def list_recording_results() -> str:
    try: result = {"ok": True, "pending": _recording_service.list_pending_results()}
    except Exception as exc: result = _recording_error(exc)
    return json.dumps(result, ensure_ascii=False)


@mcp.tool(name="mark_recording_collected", description="Mark a stopped recording's durable result as collected after the Agent verified its artifacts locally.")
def mark_recording_collected(session_id: str) -> str:
    try: result = _recording_service.mark_result_collected(session_id=session_id)
    except Exception as exc: result = _recording_error(exc)
    return json.dumps(result, ensure_ascii=False)


@mcp.tool(
    name="replay_capture",
    description=(
        "Return one source-redacted PNG of the locked window for replay evidence. "
        "Protected controls and the recorder UI are painted out on the target "
        "before the frame is encoded, so the caller may persist it into an "
        "execution report. Requires a verified window lock."
    ),
)
def replay_capture() -> str:
    if _backend is None:
        return _backend_unavailable("replay_capture")
    try:
        png, metadata = _capture_redacted_frame(_backend)
    except Exception as exc:
        return json.dumps({
            "ok": False,
            "code": "replay_capture_unavailable",
            "error": str(exc),
        }, ensure_ascii=False)
    return json.dumps({
        "ok": True,
        "capture_scope": "window",
        "image_b64": base64.b64encode(png).decode("ascii"),
        **metadata,
    }, ensure_ascii=False)


@mcp.tool(name="observe_window_bundle", description="Capture one locked-window tree and optional source-redacted image/OCR with identity recheck.")
@_serialized_product_gui
def observe_window_bundle(run_token: str | None = None, capture_image: bool = False,
                          include_ocr: bool = False, read_only: bool = False) -> str:
    global _product_observation
    if _backend is None:
        return _backend_unavailable("observe_window_bundle")
    try:
        from .windowing.observation import observe_window_bundle as _observe
    except ImportError:
        from windowing.observation import observe_window_bundle as _observe
    try:
        context = _product_policy.require_product_run(run_token)
        if read_only and context is None:
            raise ValueError('product_run_required')
        before_registry = _window_session.verify_locked() if context and _window_session else None
        if context and before_registry is None:
            raise ValueError("window_identity_stale")
        try:
            from .automation.ocr import extract_redacted_text as _ocr_extract
        except ImportError:
            from automation.ocr import extract_redacted_text as _ocr_extract
        result = _observe(
            _backend, session_id=context.run_id if context else "legacy",
            backend_kind=_backend_kind, run_token=run_token,
            capture_image=capture_image, include_ocr=include_ocr,
            capture_redacted_frame=_capture_redacted_frame,
            ocr_extract=_ocr_extract if include_ocr else None,
            identity_provider=(_window_session.verify_locked if read_only else None),
        )
        if before_registry is not None:
            after_registry = _window_session.verify_locked()
            observed = result["window_identity"]
            if (after_registry.binding != before_registry.binding or
                after_registry.native_id != observed["native_id"] or
                after_registry.generation != observed["generation"] or
                after_registry.rect != tuple(observed["rect"])):
                raise ValueError("observation_window_changed")
            try:
                from .windowing.observation import normalize_observed_control
            except ImportError:
                from windowing.observation import normalize_observed_control
            controls = {
                f"T{index:04d}": normalize_observed_control(source, observed, index)
                for index, source in enumerate(result["tree"]["controls"], start=1)
                if isinstance(source, dict)
            }
            _product_observation = {
                "run_id": context.run_id,
                "observation_id": result["observation_id"],
                "window_binding": after_registry.binding,
                "window_rect": after_registry.rect,
                "controls": controls,
            }
    except Exception as exc:
        # Only stable codes cross the wire; never return raw tree/image on error.
        code = str(exc).split(":", 1)[0]
        return json.dumps({"ok": False, "code": code, "error": code}, ensure_ascii=False)
    return json.dumps(result, ensure_ascii=False)


@mcp.tool(name="observe_product_windows", description="Read the active product run's registered, path-owned windows without changing the GUI lock.")
@_serialized_product_gui
def observe_product_windows(run_token: str) -> str:
    if _backend is None:
        return _backend_unavailable("observe_product_windows")
    try:
        context = _product_policy.require_product_run(run_token)
        if context is None or _window_session is None:
            raise ValueError("product_run_expired")
        try:
            from .product_profiles import INSTALLED_PRODUCT_SPECS
        except ImportError:
            from product_profiles import INSTALLED_PRODUCT_SPECS
        spec = INSTALLED_PRODUCT_SPECS.get((context.capability.profile_id, _backend_kind))
        if spec is None:
            raise ValueError("product_window_path_unavailable")
        windows = _window_session.observe_product_windows(
            context.capability, spec["application"],
        )
    except Exception as exc:
        code = str(exc).split(":", 1)[0]
        return json.dumps({"ok": False, "code": code}, ensure_ascii=False)
    return json.dumps({"ok": True, "windows": [window.__dict__ for window in windows]},
                      ensure_ascii=False)


# -- Region assertion row sampling (batch 2, 06 §5) ---------------------------
# 只读、同帧、不滚动：行/组件查询发生在已建立的窗口锁内（产品 run 走
# registry 复核，legacy 走 verify_window_lock），绝不绕锁独立查询；行字段
# 白名单与 evaluate_region_conditions 的行字段对齐；受保护控件（password）
# 的 value/text 一律置空，且 wire 上不出现 handle/pid/坐标。

_REGION_ROW_FIELDS = ("name", "text", "value", "checked", "enabled", "time")


def _region_container_identity(container) -> dict:
    """接受 draft/match 两种拼写，统一成稳定身份；无稳定身份显式拒绝。"""
    if not isinstance(container, dict):
        raise ValueError("container_identity_unstable")
    identity = {
        "controlType": container.get("controlType") or container.get("control_type"),
        "automationId": container.get("automationId") or container.get("automation_id"),
        "identifier": container.get("identifier"),
        "name": container.get("name") or container.get("text"),
    }
    if not (identity["automationId"] or identity["identifier"]
            or (identity["controlType"] and identity["name"])):
        raise ValueError("container_identity_unstable")
    return identity


def _region_row_fields(fields) -> list:
    if fields is None:
        return list(_REGION_ROW_FIELDS)
    if not isinstance(fields, (list, tuple)):
        raise ValueError("field_not_supported")
    wanted = []
    for field in fields:
        if field not in _REGION_ROW_FIELDS:
            raise ValueError("field_not_supported")
        if field not in wanted:
            wanted.append(field)
    return wanted


def _region_backend_error(result) -> str:
    code = result.get("code") if isinstance(result, dict) else None
    if isinstance(code, str) and code:
        return code
    error = result.get("error") if isinstance(result, dict) else None
    if isinstance(error, str) and error:
        if "not supported" in error:
            return "region_query_unsupported"
        return str(error).split(":", 1)[0]
    return "region_query_failed"


def _redact_region_rows(result, wanted) -> dict:
    if not isinstance(result, dict) or not result.get("ok"):
        raise ValueError(_region_backend_error(result))
    rows = result.get("rows")
    if not isinstance(rows, list) or any(
            not isinstance(row, dict) for row in rows):
        raise ValueError("container_rows_payload_invalid")
    clean_rows = []
    for row in rows:
        clean = {"rowIndex": row.get("rowIndex")}
        protected = row.get("protected") is True
        for field in wanted:
            value = row.get(field)
            # 脱敏边界与 capture_redacted_window_frame 同一原则：受保护控件
            # 的取值/文本绝不跨越 wire。
            if protected and field in ("value", "text"):
                value = None
            clean[field] = value
        if protected:
            clean["protected"] = True
        clean_rows.append(clean)
    return {"rows": clean_rows, "container": result.get("container")}


def _region_component_specs(components) -> list:
    if not isinstance(components, (list, tuple)):
        raise ValueError("component_identity_unstable")
    specs = []
    for component in components:
        if not isinstance(component, dict) or not isinstance(
                component.get("field"), str) or not component["field"]:
            raise ValueError("component_identity_unstable")
        try:
            identity = _region_container_identity(component.get("identity"))
        except ValueError:
            raise ValueError("component_identity_unstable") from None
        spec: dict = {"field": component["field"], "identity": identity}
        # P2g：稳定键透传（唯一/非空校验——重复键=覆盖歧义=拒绝）。
        if component.get("key") is not None:
            key = str(component["key"]).strip()
            if not key:
                raise ValueError("component_key_empty")
            if any(s.get("key") == key for s in specs):
                raise ValueError("component_key_duplicate")
            spec["key"] = key
        specs.append(spec)
    return specs


def _redact_region_values(result, specs=None) -> dict:
    if not isinstance(result, dict) or not result.get("ok"):
        raise ValueError(_region_backend_error(result))
    values = result.get("values")
    if not isinstance(values, list) or any(
            not isinstance(item, dict) for item in values):
        raise ValueError("component_values_payload_invalid")
    # For keyed requests, verify the backend response against the request
    # before exposing or evaluating it.  Otherwise duplicated, missing or
    # substituted keys could attribute a different component's value to a
    # condition.  Legacy unkeyed requests retain their existing wire format.
    expected = {s["key"]: s["field"] for s in (specs or []) if "key" in s}
    if expected:
        if len(values) != len(specs):
            raise ValueError("component_values_key_mismatch")
        seen = set()
        for item in values:
            key = item.get("key")
            if key is None:
                continue  # an unkeyed legacy component may share this request
            if not isinstance(key, str) or key not in expected or key in seen:
                raise ValueError("component_values_key_mismatch")
            if item.get("field") != expected[key]:
                raise ValueError("component_values_key_mismatch")
            seen.add(key)
        if seen != set(expected):
            raise ValueError("component_values_key_mismatch")
    clean_values = []
    for item in values:
        clean = {"field": item.get("field"), "value": item.get("value")}
        # P2g：provider 响应 key 透传（缺失/保护结果也保 key 并 value=None
        # 已然如此——不伪造值；清洗不减字段）。
        if item.get("key") is not None:
            clean["key"] = str(item["key"])
        if item.get("reason") is not None:
            clean["reason"] = item.get("reason")
        if item.get("protected") is True:
            clean["value"] = None
            clean["protected"] = True
        clean_values.append(clean)
    return {"values": clean_values}


def _verify_region_observation_context(run_token):
    """沿既有窗口锁/归属链复核观察上下文；返回 (context, before_registry)。

    产品 run：require_product_run + WindowSession.verify_locked（双端复核由
    调用点在查询后再做一次）。legacy：必须存在且验证通过的窗口锁 —— 没有
    connect→lock_window 就没有行查询。
    """
    context = _product_policy.require_product_run(run_token)
    before_registry = (
        _window_session.verify_locked() if context and _window_session else None
    )
    if context and before_registry is None:
        raise ValueError("window_identity_stale")
    if not context:
        locked = _backend.verify_window_lock(activate=False)
        if not isinstance(locked, dict) or not locked.get("ok"):
            raise ValueError("window_lock_required")
    return context, before_registry


def _verify_region_window_unchanged(before_registry) -> None:
    if before_registry is None:
        return
    after_registry = _window_session.verify_locked()
    if (after_registry.binding != before_registry.binding
            or after_registry.native_id != before_registry.native_id
            or after_registry.generation != before_registry.generation):
        raise ValueError("observation_window_changed")


@mcp.tool(
    name="observe_container_rows",
    description=(
        "Read one stable-identity container's current row fields inside the "
        "locked window (region assertion sampling; read-only, same-frame, "
        "no lock bypass; protected values are redacted)."
    ),
)
@_serialized_product_gui
def observe_container_rows(run_token: str = None, container: dict = None,
                           fields: list = None) -> str:
    if _backend is None:
        return _backend_unavailable("observe_container_rows")
    try:
        context, before_registry = _verify_region_observation_context(run_token)
        identity = _region_container_identity(container)
        wanted = _region_row_fields(fields)
        result = _backend.query_container_rows(identity, wanted)
        payload = _redact_region_rows(result, wanted)
        _verify_region_window_unchanged(before_registry)
    except Exception as exc:
        # 只有稳定错误码跨 wire；错误路径绝不回传行内容。
        code = str(exc).split(":", 1)[0]
        return json.dumps({"ok": False, "code": code, "error": code},
                          ensure_ascii=False)
    return json.dumps({"ok": True, "sampling": "locked_window_rows", **payload},
                      ensure_ascii=False)


@mcp.tool(
    name="observe_component_values",
    description=(
        "Read current values of fixed identity-addressed components inside "
        "the locked window (region assertion sampling; read-only, same-frame, "
        "no lock bypass; protected values are redacted)."
    ),
)
@_serialized_product_gui
def observe_component_values(run_token: str = None,
                             components: list = None) -> str:
    if _backend is None:
        return _backend_unavailable("observe_component_values")
    try:
        context, before_registry = _verify_region_observation_context(run_token)
        specs = _region_component_specs(components)
        result = _backend.read_component_values(specs)
        payload = _redact_region_values(result, specs)
        _verify_region_window_unchanged(before_registry)
    except Exception as exc:
        code = str(exc).split(":", 1)[0]
        return json.dumps({"ok": False, "code": code, "error": code},
                          ensure_ascii=False)
    return json.dumps({"ok": True, "sampling": "locked_window_values",
                       **payload}, ensure_ascii=False)


@mcp.tool(
    name="activate_edr",
    description=(
        "HiSec EDR specific. On Windows, ensures HisecEndpointAgent is visible "
        "with 'cmd ui', then prefers EDRClient.exe 17 --show and falls back to "
        "clicking edrWidget. By default waits up to 15 s for the EDRClient "
        "window to appear. The entry executable path must match the target's "
        "locally installed application catalog."
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
    try:
        try:
            from .product_profiles import INSTALLED_PRODUCT_SPECS
        except ImportError:  # target-local deployment
            from product_profiles import INSTALLED_PRODUCT_SPECS
        profile_id = {"windows_pywinauto": "windows_hisec",
                      "macos_accessibility": "macos_hisec"}[_backend_kind]
        configured_path = INSTALLED_PRODUCT_SPECS[(profile_id, _backend_kind)][
            "application"]["entry_path"]
        normalise = (ntpath.normcase if _backend_kind == "windows_pywinauto"
                     else posixpath.normpath)
        if exe_path is not None and normalise(exe_path) != normalise(configured_path):
            raise ValueError("product_application_path_unregistered")
    except (KeyError, ValueError) as exc:
        return json.dumps({"ok": False, "code": str(exc).split(":", 1)[0]},
                          ensure_ascii=False)
    result = _backend.activate_edr(  # type: ignore[attr-defined]
        exe_path=configured_path, wait=wait, timeout=timeout,
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
    if _backend is None:
        return _backend_unavailable("is_window_open")
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
    if _backend is None:
        return _backend_unavailable("wait_window")
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
    if (blocked := _legacy_gui_conflict("restore_edr")) is not None:
        return blocked
    try:
        def _value(v):
            return v() if callable(v) else v

        connected_app = getattr(_backend, "connected_app_instance", None) or getattr(_backend, "connected_app", None)
        if connected_app is None:
            return json.dumps({"ok": False, "error": "Not connected"})

        wins = connected_app.windows()
        if not wins:
            return json.dumps({"ok": False, "error": "No windows found"})

        win = wins[0]
        rect_attr = getattr(win, "rectangle", None)
        rect = rect_attr() if callable(rect_attr) else rect_attr
        # Force-refresh window state
        try:
            win.wait_for_minimized(timeout=0.5)
            win.restore()
            win.wait_for_not_minimized(timeout=5)
        except Exception:
            pass  # Not minimized, continue

        if rect is None:
            return json.dumps({"ok": False, "error": "Window rectangle unavailable"})
        width = _value(getattr(rect, "width", 0))
        height = _value(getattr(rect, "height", 0))
        return json.dumps({
            "ok": True,
            "rectangle": {"x": _value(getattr(rect, "left", 0)), "y": _value(getattr(rect, "top", 0)), "w": width, "h": height},
            "is_minimized": win.is_minimized()
        })
    except Exception as e:
        logger.exception("restore_edr failed")
        return json.dumps({"ok": False, "error": str(e)})


# ---------------------------------------------------------------------------
# Action catalog (P0.1)
# ---------------------------------------------------------------------------

@mcp.tool(
    name="get_action_catalog",
    description=(
        "Return the V1 action catalog: semantic action_id, optional "
        "action_code, MCP tool_name, description, JSON input/result "
        "schema, supported backends, requires, side_effect/risk/"
        "rollback_class, default screenshot, transition_policy, "
        "preferred_over, and live enablement for the current backend. "
        "Use this before planning to confirm which actions are available."
    ),
)
def get_action_catalog(backend: str = None, include_disabled: bool = False) -> str:
    """Return the immutable V1 action catalog.

    Parameters
    ----------
    backend : str | None
        Backend name to compute static enablement for. When None,
        `enabled` is reported as null and the planner must re-call with
        a backend.
    include_disabled : bool
        When True, disabled actions are also included in the response.

    Note: the tool body is purely a catalog → JSON projection (P0.1).
    Runtime backend probing is P1.1 dispatcher territory and is not
    accessible from this tool. See `action_catalog.runtime_capability`
    for the runtime probe API (unused in P0.1).
    """
    if backend is None and _backend is not None:
        backend = _backend_kind
    payload = _get_action_catalog_dict(
        backend=backend,
        include_disabled=include_disabled,
    )
    return json.dumps(payload, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

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

    # Default to the resolved startup kind so a failure reading `.backend`
    # below still reports something useful instead of "unknown".
    backend_name = _backend_kind
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
        # Catalog version/digest (P0.1). Plans must carry the matching
        # pair; a mismatch is a structured error in P0.2 validation.
        # Nested under `metadata` to keep top-level keys stable for
        # legacy consumers and to leave room for future metadata fields
        # (renderer_version, schema_versions) without re-shuffling.
        "metadata": {
            "catalog_version": CATALOG_VERSION,
            "catalog_digest": catalog_digest(),
        },
        # HiSec-specific; granular process/window detection
        "hisec_agent_process_found": False,
        "hisec_main_window_found": False,
        "edr_client_process_found": False,
        "edr_client_window_found": False,
        "interactive_session": os.environ.get("SESSIONNAME", ""),
    }

    if _backend is not None:
        # Catalog-driven status.action_space (P0.1). The map is generated
        # from the V1 catalog; `restore_edr` has
        # `execution_provider == "server_inline"` and is implemented
        # inline in this module. The catalog is the single source of
        # truth — no override is applied here.
        # `.backend` is the automation engine selected inside an adapter
        # (for pywinauto this is `uia`), not the canonical backend kind
        # understood by the action catalog. Keep it diagnostic, but query the
        # catalog by the startup-resolved kind.
        result["action_space"] = status_action_space(_backend_kind)
        if hasattr(_backend, "get_window_lock"):
            try:
                result["window_lock"] = _backend.get_window_lock()
            except Exception as e:
                result["window_lock"] = {"ok": False, "error": str(e)}

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
    if (blocked := _legacy_gui_conflict("run_powershell")) is not None:
        return blocked
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
    if (blocked := _legacy_gui_conflict("start_powershell")) is not None:
        return blocked
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

    try:
        from .automation.macos_accessibility import diagnose_windows as _diag
    except ImportError:
        from automation.macos_accessibility import diagnose_windows as _diag
    result = _diag()
    return json.dumps(result, ensure_ascii=False)


# ---------------------------------------------------------------------------
# P1.1 unified dispatcher: execute_action MCP tool
# ---------------------------------------------------------------------------


def _verify_product_target_ref(context, target_ref: dict) -> None:
    """Reject stale semantic targets before any backend click is attempted."""
    observation = _product_observation
    if (observation is None or observation["run_id"] != context.run_id
        or target_ref.get("snapshot_id") != observation["observation_id"]):
        raise ValueError("observation_stale")
    saved = observation["controls"].get(target_ref.get("target_id"))
    if (saved is None or not target_ref.get("fingerprint")
        or saved["fingerprint"] != target_ref["fingerprint"]
        or saved.get("rect") is None):
        raise ValueError("observation_stale")
    if _window_session is None:
        raise ValueError("window_identity_stale")
    locked = _window_session.verify_locked()
    if (locked.binding != observation["window_binding"]
            or locked.rect != tuple(observation["window_rect"])):
        raise ValueError("window_identity_stale")
    current = _backend.dump_tree(max_depth=15)
    if not isinstance(current, dict) or not current.get("ok"):
        raise ValueError("observation_stale")
    try:
        from .windowing.observation import normalize_observed_control
        from .windowing.guard import WindowGuard
    except ImportError:
        from windowing.observation import normalize_observed_control
        from windowing.guard import WindowGuard
    identity = _window_session.verify_locked().__dict__
    matches = [normalize_observed_control(source, identity, index)
               for index, source in enumerate(current.get("controls") or (), start=1)
               if isinstance(source, dict)]
    matches = [item for item in matches if item["fingerprint"] == saved["fingerprint"]]
    if len(matches) != 1 or matches[0].get("rect") is None:
        raise ValueError("observation_stale")
    WindowGuard.verify_target_before_injection(
        {"observation_id": target_ref["snapshot_id"], "target_rect": saved["rect"]},
        observation["observation_id"], tuple(matches[0]["rect"]),
    )


@_serialized_product_gui
def execute_action(
    action_id: str,
    action_code: str | None = None,
    args: dict | None = None,
    target_ref: dict | None = None,
    request_id: str | None = None,
    run_token: str | None = None,
) -> str:
    """Single unified dispatcher (P1.1).

    Returns the JSON-encoded ActionReceipt. The receipt is the
    canonical envelope for both success and failure paths; legacy
    tools remain byte-compatible with their previous return shape.
    """
    # Lazy import to keep the module import surface stable.
    try:
        from .action_dispatcher import dispatch
        from .action_catalog import ACTIONS_V1, get_spec
        from .action_dispatcher.receipts import ActionReceipt
    except ImportError:
        from action_dispatcher import dispatch
        from action_catalog import ACTIONS_V1, get_spec
        from action_dispatcher.receipts import ActionReceipt

    # A product run binds actions to the current native window generation.
    # The backend pointer guard then performs the final foreground/hit-test
    # check at the injection boundary. Legacy calls keep their old contract.
    try:
        context = _product_policy.require_product_run(run_token)
    except ValueError:
        context = None  # dispatcher emits the canonical token error below
    if context is not None:
        spec = get_spec(ACTIONS_V1, action_id)
        if request_id:
            try:
                from .action_dispatcher.cache import lookup, scoped_request_key
            except ImportError:
                from action_dispatcher.cache import lookup, scoped_request_key
            if lookup(scoped_request_key(context.run_id, request_id)) is not None:
                return dispatch(action_id=action_id, action_code=action_code,
                                args=args, target_ref=target_ref,
                                request_id=request_id, run_token=run_token).to_json()
        if spec is not None and "window_lock" in spec.requires:
            try:
                if _window_session is None:
                    raise ValueError("window_identity_stale")
                _window_session.verify_locked()
            except Exception:
                return ActionReceipt.from_error(
                    code="window_identity_stale", action_id=action_id,
                    action_code=action_code, request_id=request_id,
                    message="native window identity changed before dispatch",
                ).to_json()
        if spec is not None and "target_ref" in spec.requires and target_ref:
            try:
                _verify_product_target_ref(context, target_ref)
            except Exception as exc:
                return ActionReceipt.from_error(
                    code="window_identity_stale" if str(exc) == "window_identity_stale" else "observation_stale",
                    action_id=action_id, action_code=action_code,
                    request_id=request_id,
                    message="semantic target changed before dispatch",
                ).to_json()

    receipt = dispatch(
        action_id=action_id,
        action_code=action_code,
        args=args,
        target_ref=target_ref,
        request_id=request_id,
        run_token=run_token,
    )
    if context is not None and spec is not None and spec.side_effect != "none":
        # Even an error may have occurred after an OS input was sent; a new
        # observation is required before another target-bearing action.
        global _product_observation
        _product_observation = None
    if receipt.ok and action_id == "session.window_lock.set" and run_token:
        try:
            if _window_session is None:
                raise ValueError("window_identity_stale")
            _window_session.bind_backend_lock()
        except Exception:
            with _product_policy.authorized_dispatch(run_token):
                _backend.unlock_window()
            try:
                from .action_dispatcher.receipts import ActionReceipt as _ActionReceipt
                from .action_dispatcher.cache import scoped_request_key, store
            except ImportError:
                from action_dispatcher.receipts import ActionReceipt as _ActionReceipt
                from action_dispatcher.cache import scoped_request_key, store
            receipt = _ActionReceipt.from_error(
                code="window_identity_stale", action_id=action_id,
                action_code=action_code, request_id=request_id,
                message="locked window could not be uniquely bound to its native identity",
            )
            if request_id:
                context = _product_policy.require_product_run(run_token)
                store(scoped_request_key(context.run_id, request_id), receipt)
    return receipt.to_json()


# Register without replacing the module-level function. FastMCP versions differ
# in whether decorator syntax returns the original callable or a FunctionTool;
# keeping the callable stable preserves direct Python and compatibility tests.
mcp.tool(
    name="execute_action",
    description=(
        "Unified dispatcher entry point (P1.1). Routes action_id to "
        "the corresponding backend method, validates preconditions, "
        "checks ownership, normalises the result into an ActionReceipt, and "
        "returns the cached receipt on duplicate request_id (FR-P1.1-01..02)."
    ),
)(execute_action)


@mcp.tool(name="begin_product_run", description="Acquire an exclusive, leased product GUI run.")
@_serialized_product_gui
def begin_product_run(profile_id: str, digest: str, lease_seconds: float = 300) -> str:
    global _window_session
    try:
        recording = _recording_service.status(heartbeat=False)
    except RecordingModelError as exc:
        if exc.code != "recording_session_missing":
            return json.dumps({"ok": False, "code": "recording_status_unavailable"}, ensure_ascii=False)
        recording = {"state": "idle"}
    except Exception:
        return json.dumps({"ok": False, "code": "recording_status_unavailable"}, ensure_ascii=False)
    if recording.get("state") in {"recording", "paused"}:
        return json.dumps({"ok": False, "code": "recording_session_active"}, ensure_ascii=False)
    try:
        context = _product_policy.begin_product_run(profile_id, digest,
                                                    lease_seconds=lease_seconds,
                                                    backend_kind=_backend_kind)
    except ValueError as exc:
        return json.dumps({"ok": False, "code": str(exc).split(":", 1)[0]}, ensure_ascii=False)
    try:
        from .windowing.runtime import WindowSession
    except ImportError:
        from windowing.runtime import WindowSession
    _window_session = WindowSession(context.run_id, _backend_kind, _backend)
    try:
        _window_session.bind_backend_lock()
    except Exception:
        # A run can begin before any connect/lock; the first role focus or
        # session.window_lock.set will establish the authoritative binding.
        pass
    global _binding_facade_holder
    try:
        from .product_profiles import get_capability as _get_capability
    except ImportError:
        from product_profiles import get_capability as _get_capability
    try:
        supported = False
        tracked_generation = 0
        coverage = "unknown"
        backend_decides = False  # backend 中性自报(能力交由跟踪事实)
        if _backend is not None:
            cap_probe = getattr(_backend, "window_sequence_capability", None)
            structured = cap_probe() if callable(cap_probe) else None
            has_stream_factory = callable(
                getattr(_backend, "lifecycle_event_stream", None))
            if isinstance(structured, dict):
                if structured.get("decided_by") == "instance-tracker":
                    # §8 生产协商(0346 修订):仅接受**显式协议标记**
                    # decided_by=instance-tracker — 不从函数存在/false/
                    # None/字符串隐推中性。显式 unsupported 始终否决;
                    # 自报 strong(独立证据)仍走严格门(1744 不回退)。
                    backend_decides = True
                else:
                    # 非 decided_by 协议:自报即权威 — 显式 false /
                    # 弱覆盖 / 缺失 / 非法字段 → 拒绝(否决权保留,
                    # 即使有事件源工厂)。
                    supported = structured.get("supported") is True
                    tracked_generation = int(structured.get("generation") or 0)
                    coverage = str(structured.get("coverage") or "unknown")
        # 本 run 强实例跟踪底座(§7.1/§8):backend 暴露本 run 生命周期
        # 事件源时建立跟踪并以其结构化事实协商;无事件源 → 维持
        # unsupported(fail closed,不硬编码、不用 polling 伪造)。
        run_tracker = None
        if _backend is not None:
            stream_factory = getattr(_backend, "lifecycle_event_stream", None)
            if callable(stream_factory):
                try:
                    run_tracker = _RunInstanceTracker(
                        event_source=stream_factory(),
                        run_token=context.run_token)
                    run_tracker.register_with_policy(_product_policy)
                    run_tracker.start()
                except Exception:  # noqa: BLE001 — 订阅失败如实降级
                    run_tracker = None
        if run_tracker is not None:
            tracker_cap = run_tracker.capability()
            if backend_decides:
                # 生产协商:本 run 跟踪事实是唯一权威(事件源/代际/覆盖
                # 共同证明);tracker 不满足即 unsupported。
                supported = tracker_cap.get("supported") is True
                coverage = str(tracker_cap.get("coverage") or "unknown")
                tracked_generation = int(tracker_cap.get("generation") or 0)
            else:
                # backend 自报 strong:严格门保持(1744 契约不回退)。
                if coverage != "strong" or tracked_generation <= 0:
                    supported = False
                else:
                    supported = (supported
                                 and tracker_cap.get("supported") is True)
                coverage = (tracker_cap.get("coverage")
                            if not supported or coverage == "unknown"
                            else coverage)
                tracked_generation = max(
                    tracked_generation,
                    int(tracker_cap.get("generation") or 0))
        elif not backend_decides:
            # §8 必要条件:本 run 事件源缺失(或订阅失败)→ 不放行,
            # backend 自报 strong 不充分(review 2112/任务契约)。
            supported = False
            coverage = "unavailable"
        _binding_facade_holder["facade"] = _WindowBindingFacade(
            session=_window_session, policy=_product_policy,
            lease_token=context.run_token,
            declared_windows=_binding_declared_windows(context),
            tracked_generation=tracked_generation, coverage=coverage,
            supported=supported,
            installed_paths=_installed_product_paths(context),
            tracker=run_tracker)
        _binding_facade_holder["tracker"] = run_tracker
    except Exception:
        _binding_facade_holder["facade"] = None
    return json.dumps({
        "ok": True, "run_id": context.run_id, "run_token": context.run_token,
        "profile_id": context.capability.profile_id, "expires_in": lease_seconds,
    }, ensure_ascii=False)


@mcp.tool(name="end_product_run", description="Release the active product GUI run.")
@_serialized_product_gui
def end_product_run(run_token: str) -> str:
    try:
        cleanup_results = _product_policy.end_product_run(run_token)
    except ValueError as exc:
        return json.dumps({"ok": False, "code": str(exc).split(":", 1)[0]}, ensure_ascii=False)
    # The real cleanup result — collected through the existing token-
    # validated hook path, not by pre-unlocking speculatively — is surfaced
    # here: a failing OS unlock appears in cleanup_errors while the run's
    # memory is still dropped unconditionally.
    errors = []
    for result in cleanup_results or []:
        if isinstance(result, dict):
            errors.extend(result.get("errors") or [])
    payload = {"ok": True}
    if errors:
        payload["cleanup_errors"] = errors
        payload["message"] = ("run released; one or more cleanup steps "
                              "failed and the errors are preserved above")
    return json.dumps(payload, ensure_ascii=False)


# -- S3 window-binding tools (09 §7/§8) ---------------------------------------
# The facade is per-run (it holds the lease token), so begin_product_run
# creates it and _binding_facade returns it only while that run is active.
# Registration is module-level: the three MCP tools exist as soon as the
# server starts, and each call resolves the active run's facade or refuses.

try:
    from .windowing.binding import (
        WindowBindingFacade as _WindowBindingFacade,
        WindowBindingRefusal as _WindowBindingRefusal,
    )
    from .windowing.instance_tracker import (
        RunInstanceTracker as _RunInstanceTracker,
    )
except ImportError:  # Target-local deployment (target/ on sys.path).
    from windowing.binding import (
        WindowBindingFacade as _WindowBindingFacade,
        WindowBindingRefusal as _WindowBindingRefusal,
    )
    from windowing.instance_tracker import (
        RunInstanceTracker as _RunInstanceTracker,
    )

_binding_facade_holder: dict = {"facade": None, "tracker": None}


def _binding_facade_for_run(run_token: str) -> _WindowBindingFacade:
    facade = _binding_facade_holder.get("facade")
    if facade is None or facade._lease_token != run_token:
        raise _WindowBindingRefusal(
            "window_binding_lease_refused",
            "no active window-binding facade under this run token")
    # The holder alone is not proof the run is alive: after a real
    # end_product_run (or lazy lease expiry) the stale facade must refuse
    # every call, not keep serving BOUND state.
    try:
        context = _product_policy.require_product_run(run_token)
    except ValueError as exc:
        raise _WindowBindingRefusal(
            "window_binding_lease_refused", str(exc)) from exc
    if context is None:
        raise _WindowBindingRefusal(
            "window_binding_lease_refused",
            "the product-run lease is no longer active under this token")
    return facade


def _binding_declared_windows(context) -> dict:
    """The declared windows come from the installed product profile's
    window roles (root/title identity per role), not from the caller.
    Takes the active run context directly — it is also used while the
    facade is being constructed, so it must not read the holder."""
    if context is None:
        return {}
    try:
        from .product_profiles import INSTALLED_PRODUCT_SPECS
    except ImportError:
        from product_profiles import INSTALLED_PRODUCT_SPECS
    spec = INSTALLED_PRODUCT_SPECS.get(
        (context.capability.profile_id, _backend_kind))
    if spec is None:
        return {}
    declared = {}
    for role in context.capability.window_roles:
        identity: dict = {}
        if role.process_names:
            identity["processName"] = role.process_names[0]
        if role.root_control_ids:
            identity["rootAutomationId"] = role.root_control_ids[0]
        if role.title_pattern:
            identity["titleRegex"] = role.title_pattern
        declared[role.role] = identity
    return declared


def _installed_product_paths(context) -> tuple:
    """Installed application paths of the active run's product profile —
    the authorization domain for candidate-process ownership checks."""
    if context is None:
        return ()
    try:
        from .product_profiles import INSTALLED_PRODUCT_SPECS
    except ImportError:
        from product_profiles import INSTALLED_PRODUCT_SPECS
    spec = INSTALLED_PRODUCT_SPECS.get(
        (context.capability.profile_id, _backend_kind))
    if spec is None:
        return ()
    application = spec.get("application") or {}
    return tuple(application.get(key) for key in ("path", "entry_path")
                 if isinstance(application.get(key), str)
                 and application.get(key))


def server_observation_controls() -> dict:
    """The projected control map (target_id → normalized projection) of
    the active observation — the same map the dispatcher validates
    target_ref fingerprints against."""
    observation = globals().get("_product_observation")
    if not isinstance(observation, dict):
        return {}
    return observation.get("controls") or {}


@mcp.tool(name="bind_entry_window", description="Bind a golden window declaration to the running window instance under the product lease.")
@_serialized_product_gui
def bind_entry_window(window_id: str, identity: dict, run_token: str,
                      control_selector: dict | None = None) -> str:
    try:
        facade = _binding_facade_for_run(run_token)
    except _WindowBindingRefusal as exc:
        return json.dumps({"ok": False, "code": exc.code, "error": str(exc)},
                          ensure_ascii=False)
    try:
        result = facade.bind(window_id, identity, run_token=run_token,
                             control_selector=control_selector)
    except _WindowBindingRefusal as exc:
        return json.dumps({"ok": False, "code": exc.code, "error": str(exc)},
                          ensure_ascii=False)
    return json.dumps(result, ensure_ascii=False)


@mcp.tool(name="register_window_declaration", description="Register THIS run's golden window declarations (compiled-trace W references). Each entry is validated inside the installed product's authorized window-role domain; a golden W is a reference, never an authorization.")
@_serialized_product_gui
def register_window_declaration(declarations_json: str, run_token: str) -> str:
    try:
        facade = _binding_facade_for_run(run_token)
    except _WindowBindingRefusal as exc:
        return json.dumps({"ok": False, "code": exc.code, "error": str(exc)},
                          ensure_ascii=False)
    try:
        declarations = json.loads(declarations_json)
        if not isinstance(declarations, dict):
            raise ValueError("window_declaration_invalid")
        result = facade.register_golden_windows(declarations)
    except _WindowBindingRefusal as exc:
        return json.dumps({"ok": False, "code": exc.code, "error": str(exc)},
                          ensure_ascii=False)
    except Exception as exc:  # noqa: BLE001 — stable codes only
        return json.dumps({"ok": False, "code": "window_declaration_invalid",
                           "error": str(exc)}, ensure_ascii=False)
    return json.dumps(result, ensure_ascii=False)


@mcp.tool(name="reverify_bound_window", description="Re-verify a bound window against the live inventory without dispatching any action (§7.2: consecutive steps re-verify; caching may skip activation, never the checks).")
@_serialized_product_gui
def reverify_bound_window(window_id: str, binding_token: str,
                          run_token: str) -> str:
    try:
        facade = _binding_facade_for_run(run_token)
    except _WindowBindingRefusal as exc:
        return json.dumps({"ok": False, "code": exc.code, "error": str(exc)},
                          ensure_ascii=False)
    try:
        result = facade.reverify(window_id, binding_token,
                                 run_token=run_token)
    except _WindowBindingRefusal as exc:
        return json.dumps({"ok": False, "code": exc.code, "error": str(exc)},
                          ensure_ascii=False)
    return json.dumps(result, ensure_ascii=False)


@mcp.tool(name="focus_bound_window", description="Focus, authoritative-lock and re-verify a bound window in the same lease boundary; a refused verification executes zero actions. With action_id set, dispatches through the unified dispatcher's existing gates (target_ref passed through). With observe=true and no action_id, performs the lock + a fresh observation of THIS window and returns the projected controls for building target_ref values; no action executes.")
@_serialized_product_gui
def focus_bound_window(window_id: str, binding_token: str, run_token: str,
                       action_id: str | None = "gui.click",
                       action_args: dict | None = None,
                       request_id: str | None = None,
                       target_ref: dict | None = None,
                       observe: bool = False, read_only: bool = False) -> str:
    try:
        facade = _binding_facade_for_run(run_token)
    except _WindowBindingRefusal as exc:
        return json.dumps({"ok": False, "code": exc.code, "error": str(exc),
                           "executed": False}, ensure_ascii=False)
    if read_only:
        # Post-action evidence must not focus/reconnect/activate the parent:
        # doing so dismisses Qt menus and destroys the effect being measured.
        if action_id is not None or not observe:
            return json.dumps({'ok': False, 'code': 'readonly_action_forbidden',
                               'executed': False})
        try:
            with facade._lock, _product_policy.authorized_dispatch(run_token):
                facade.reverify(window_id, binding_token, run_token=run_token)
                observed = json.loads(observe_window_bundle(run_token, read_only=True))
                return json.dumps({**observed, 'executed': False,
                                   'controls': server_observation_controls()
                                   if observed.get('ok') else {}}, ensure_ascii=False)
        except _WindowBindingRefusal as exc:
            return json.dumps({'ok': False, 'code': exc.code,
                               'error': str(exc), 'executed': False})
    # Execution is tri-state: "never called" (executed=false) / "entered
    # dispatch, result unknown" (executed=unknown — the callback may have
    # had side effects before failing) / "deterministic receipt"
    # (executed=true). Any exception after entering dispatch — a refusal
    # or a generic error — never downgrades to zero execution and never
    # hides its cause.
    dispatches: list[str] = []
    entered_dispatch: list[bool] = []

    def _dispatch() -> str:
        if observe:
            # §7.2 step 4: refresh the observation of THIS window inside
            # the same lease/serialization boundary after the authoritative
            # lock. A supplied target_ref must reference THIS fresh
            # observation; stale or foreign-window refs fail the
            # dispatcher's existing gates with zero input — kept intact.
            # These are PRE-DISPATCH preparation failures: they must report
            # executed=false (review 1908: focus/observation side effects
            # are not a dispatched business action).
            observation = json.loads(observe_window_bundle(run_token))
            if not observation.get("ok"):
                raise _WindowBindingRefusal(
                    "observation_stale",
                    f"post-lock observation failed: "
                    f"{observation.get('code')}")
            if target_ref is not None and target_ref.get(
                    "snapshot_id") != observation.get("observation_id"):
                raise _WindowBindingRefusal(
                    "observation_stale",
                    "target_ref does not reference the fresh post-lock "
                    "observation; re-observe before dispatching")
        # entered_dispatch is recorded only at the actual dispatcher call
        # boundary — never for the observation/preparation step above.
        entered_dispatch.append(True)
        receipt_json = execute_action(
            action_id=action_id, args=action_args, request_id=request_id,
            target_ref=target_ref, run_token=run_token)
        dispatches.append(receipt_json)
        return receipt_json

    if action_id is None:
        # Lock + observe only: no action_id means no dispatch — the
        # caller receives the fresh observation and projected controls
        # (the same projection the dispatcher validates target_ref
        # against) to build a semantic reference for the next call.
        observed: dict = {}
        observed_flag: list[bool] = []

        def _observe_only() -> str:
            observed_flag.append(True)
            observed.update(json.loads(observe_window_bundle(run_token)))
            return json.dumps({"ok": observed.get("ok") is True})

        try:
            facade.focus_and_execute(
                window_id, binding_token, run_token=run_token,
                dispatch_action=_observe_only)
        except _WindowBindingRefusal as exc:
            return json.dumps({"ok": False, "code": exc.code,
                               "error": str(exc), "executed": False},
                              ensure_ascii=False)
        if not observed_flag or not observed.get("ok"):
            return json.dumps({"ok": False,
                               "code": (observed.get("code")
                                        if observed else
                                        "observation_not_captured"),
                               "executed": False}, ensure_ascii=False)
        return json.dumps({**observed, "executed": False,
                           "controls": server_observation_controls()},
                          ensure_ascii=False)

    try:
        facade.focus_and_execute(
            window_id, binding_token, run_token=run_token,
            dispatch_action=_dispatch)
    except _WindowBindingRefusal as exc:
        if dispatches:
            return json.dumps({"ok": False, "code": exc.code,
                               "error": str(exc), "executed": True},
                              ensure_ascii=False)
        if entered_dispatch:
            return json.dumps({"ok": False, "code": exc.code,
                               "error": str(exc), "executed": "unknown",
                               "message": "the action entered dispatch; "
                                          "receipt unknown"},
                              ensure_ascii=False)
        return json.dumps({"ok": False, "code": exc.code, "error": str(exc),
                           "executed": False}, ensure_ascii=False)
    except Exception as exc:  # noqa: BLE001 — structured unknown, never swallow
        if entered_dispatch:
            return json.dumps({"ok": False,
                               "code": "window_binding_dispatch_unknown",
                               "error": f"{type(exc).__name__}: {exc}",
                               "executed": "unknown",
                               "message": "the action entered dispatch; "
                                          "receipt unknown"},
                              ensure_ascii=False)
        raise
    # The dispatch receipt IS the result: focus_and_execute returns whatever
    # the dispatch callback produced (the ActionReceipt JSON envelope).
    return dispatches[-1] if dispatches else json.dumps(
        {"ok": False, "code": "window_binding_dispatch_unavailable",
         "executed": False}, ensure_ascii=False)


@mcp.tool(name="window_binding_state", description="Read the current binding state for a bound window.")
@_serialized_product_gui
def window_binding_state(window_id: str, binding_token: str, run_token: str) -> str:
    try:
        facade = _binding_facade_for_run(run_token)
    except _WindowBindingRefusal as exc:
        return json.dumps({"ok": False, "code": exc.code, "error": str(exc)},
                          ensure_ascii=False)
    binding = facade._bindings.get(window_id)
    if binding is None or binding.token != binding_token:
        return json.dumps({"ok": False, "code": "window_binding_not_found"},
                          ensure_ascii=False)
    return json.dumps({"ok": True, "state": binding.state,
                       "revision": binding.revision,
                       "generation": binding.generation},
                      ensure_ascii=False)


@mcp.tool(name="focus_window_role", description="Resolve, focus and lock one role from the active product profile.")
@_serialized_product_gui
def focus_window_role(role: str, run_token: str) -> str:
    global _product_observation
    try:
        context = _product_policy.require_product_run(run_token)
        if context is None or _window_session is None:
            raise ValueError("product_run_expired")
        identity = _window_session.focus_and_lock_role(role, context.capability, run_token)
        _product_observation = None
    except Exception as exc:
        code = str(exc).split(":", 1)[0]
        return json.dumps({"ok": False, "code": code}, ensure_ascii=False)
    return json.dumps({"ok": True, "identity": identity.__dict__}, ensure_ascii=False)


@mcp.tool(name="focus_window_selector", description="Focus and lock one path-owned product window by its stable UIA root identity.")
@_serialized_product_gui
def focus_window_selector(process_name: str, title_regex: str,
                          root_control_id: str, run_token: str) -> str:
    global _product_observation
    try:
        context = _product_policy.require_product_run(run_token)
        if context is None or _window_session is None:
            raise ValueError("product_run_expired")
        try:
            from .product_profiles import INSTALLED_PRODUCT_SPECS
        except ImportError:
            from product_profiles import INSTALLED_PRODUCT_SPECS
        spec = INSTALLED_PRODUCT_SPECS.get((context.capability.profile_id, _backend_kind))
        if spec is None:
            raise ValueError("product_window_path_unavailable")
        identity = _window_session.focus_and_lock_selector(
            process_name, title_regex, root_control_id,
            context.capability, spec["application"], run_token,
        )
        _product_observation = None
    except Exception as exc:
        code = str(exc).split(":", 1)[0]
        return json.dumps({"ok": False, "code": code}, ensure_ascii=False)
    return json.dumps({"ok": True, "identity": identity.__dict__}, ensure_ascii=False)


@mcp.tool(name="focus_entry_window", description=(
    "Replay entry-window controlled recovery (design doc 08, R2), under the "
    "held product-run lease. Read-only enumerates windows matching the "
    "declared entry identity FIRST; when root_automation_id is declared, each "
    "candidate's live window root must match too (never downgraded to a "
    "title-only match). Only when exactly one window matches the FULL "
    "declared identity, connects that exact HWND, re-verifies the identity "
    "under the same authorized dispatch (replacement race guard), locks it "
    "strictly with activation, and binds it to the window session. Never "
    "resolves or launches an application, never starts a service, never "
    "clicks a control. Structured dispositions instead of guessing: "
    "entry_window_absent / entry_window_ambiguous / entry_root_unverifiable / "
    "entry_window_stale / acquired."))
@_serialized_product_gui
def focus_entry_window(process_name: str, title_regex: str = None, run_token: str = None,
                       root_automation_id: str = None) -> str:
    if _backend is None:
        return _backend_unavailable("focus_entry_window")
    try:
        from .product_policy import authorized_dispatch
    except ImportError:
        from product_policy import authorized_dispatch

    def _reject(code, error, **extra):
        return json.dumps({"ok": False, "code": code, "error": error, **extra},
                          ensure_ascii=False)

    try:
        context = _product_policy.require_product_run(run_token)
        if context is None:
            raise ValueError("product_run_expired")
        # A declared root identity is only verifiable through an enumeration
        # that exposes each window's own root automation id. Without that
        # capability the declared field cannot be checked, and the contract
        # forbids silently falling back to a title-only match.
        root_declared = bool(root_automation_id)
        root_enumeration = getattr(_backend, "_enumerate_top_windows_windows", None)
        if root_declared and not callable(root_enumeration):
            return _reject(
                "entry_root_unverifiable",
                "the backend cannot verify a declared window root identity; "
                "refusing rather than matching on the title alone")
        # Read-only enumeration first: existence and uniqueness are established
        # BEFORE any mutation. This is the re-confirmation the design requires
        # -- we never act on a stale connect result, only on a fresh unique
        # match of the full declared identity.
        found = _backend.is_window_open(title_regex, process_name, None)
        if not isinstance(found, dict) or not found.get("ok"):
            code = found.get("code") if isinstance(found, dict) else None
            return _reject(code or "entry_window_probe_failed",
                           (found.get("error") if isinstance(found, dict)
                            else "window enumeration failed"))
        windows = found.get("windows") or []
        if root_declared:
            roots = {}
            for item in root_enumeration():
                roots[item.get("handle")] = str(item.get("root_control_id") or "")
            windows = [w for w in windows
                       if roots.get(w.get("handle")) == str(root_automation_id)]
            if not windows:
                return _reject(
                    "entry_window_absent",
                    "no window matches the declared entry identity "
                    "(process, title and root)", disposition="entry_window_absent")
        elif not windows:
            return _reject("entry_window_absent",
                           "no window matches the recorded entry identity",
                           disposition="entry_window_absent")
        if len(windows) > 1:
            return json.dumps({
                "ok": False, "code": "entry_window_ambiguous",
                "disposition": "entry_window_ambiguous",
                "error": "several windows match the declared entry identity",
                "candidates": [
                    {"pid": w.get("process_id"), "title": w.get("title"),
                     "handle": w.get("handle")} for w in windows[:8]],
            }, ensure_ascii=False)
        window = windows[0]
        handle = int(window.get("handle"))
        pid = int(window.get("process_id"))
        title = str(window.get("title") or "")
        title_re = "^" + re.escape(title) + "$" if title else None
        # Connect + strict lock + activation on the exact live HWND, under the
        # lease's authorized dispatch (legacy GUI mutations are otherwise
        # refused while a run is active). lock_window verifies internally and
        # clears itself on refusal; nothing here launches or clicks.
        with authorized_dispatch(run_token):
            connected = _backend.connect_window_identity(handle, pid)
            if not isinstance(connected, dict) or not connected.get("ok"):
                return _reject("entry_window_connect_failed",
                               (connected or {}).get("error", "connect failed")
                               if isinstance(connected, dict) else "connect failed")
            # Replacement race guard: between the enumeration and this lock the
            # HWND may have been destroyed and reused by a different window.
            # Re-verify the full declared identity against the connected lock
            # snapshot; a stale match is refused, never locked.
            if root_declared:
                current_roots = {
                    item.get("handle"): str(item.get("root_control_id") or "")
                    for item in root_enumeration()
                }
                if current_roots.get(handle) != str(root_automation_id):
                    _backend.unlock_window()
                    return _reject(
                        "entry_window_stale",
                        "the matching window was replaced between enumeration "
                        "and acquisition; the identity no longer verifies")
            locked = _backend.lock_window(pid=pid, title_re=title_re,
                                          process_name=str(process_name),
                                          strict=True, activate=True)
            if not isinstance(locked, dict) or not locked.get("ok"):
                code = locked.get("code") if isinstance(locked, dict) else None
                return _reject(code or "entry_window_lock_failed",
                               (locked.get("error") if isinstance(locked, dict)
                                else "lock failed"))
            if _window_session is not None:
                try:
                    _window_session.bind_backend_lock()
                except Exception:
                    # A lock the product registry cannot bind cannot serve
                    # product-run observations; release and report honestly.
                    _backend.unlock_window()
                    return _reject(
                        "entry_window_bind_failed",
                        "locked window is not a registry-bindable product window")
        return json.dumps({"ok": True, "disposition": "acquired",
                           "window": {"pid": pid, "title": title, "handle": handle},
                           "run_id": context.run_id},
                          ensure_ascii=False)
    except Exception as exc:
        code = str(exc).split(":", 1)[0]
        return json.dumps({"ok": False, "code": code}, ensure_ascii=False)


def _restore_python_tool_callables() -> None:
    """Keep direct Python calls stable across FastMCP decorator versions.

    FastMCP already retains each ``FunctionTool`` in ``mcp``. Some releases
    return that object from decorator syntax while older releases return the
    original function. Restore only module globals that expose a callable
    ``.fn``; MCP registration remains unchanged.
    """
    for name, value in list(globals().items()):
        fn = getattr(value, "fn", None)
        if callable(fn):
            globals()[name] = fn


_restore_python_tool_callables()


# ---------------------------------------------------------------------------
# DPI awareness (design 11 D2 replay verification, live 47e0aac2): without a
# process-wide declaration the server's GUI process runs DPI-virtualized —
# every UIA rectangle reads scaled coordinates and pywinauto's click_input
# lands OFF the target control (a physical click at the recorded settingBtn
# coordinates opened nothing while SendInput at the same screen point
# worked). The recorder panel already declares this for its own window; the
# automation backend needs it process-wide before any control geometry is
# read or clicked.
# ---------------------------------------------------------------------------

def _declare_dpi_awareness() -> None:
    if sys.platform != "win32":
        return
    import ctypes
    try:
        # Per-monitor-v2 (Windows 10 1703+); falls back gracefully.
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(-4):
            return
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    _declare_dpi_awareness()
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
