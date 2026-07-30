"""
target_manager.py — Multi-target lifecycle management for EDR-WD.

Orchestrates target selection, server lifecycle (install/ensure/stop),
health checking. Delegates SSH to ssh_runner, MCP to mcp_manager, and
per-platform lifecycle operations to agent/lifecycle/<platform>.py.

Interface:
    list_targets()
    get_target(name=None)
    check_server_health(name=None)
    install_target_task(name=None)
    ensure_server_running(name=None)
    stop_server(name=None)
    restart_server(name=None)

All public methods return a structured result dict:
    {"ok": bool, "target": str, "stage": str, "data": ..., "error": str}

SSHAuthError / UnsupportedAuthType / UnsupportedPlatformError are caught
and returned as structured errors (not raised), so callers never see raw
exceptions.
"""

from __future__ import annotations

import socket
import time
from pathlib import Path
from typing import Optional

from .target_config import (
    ConfigNotFound,
    ConfigError,
    TargetConfig,
)
from .ssh_runner import (
    SSHAuthError,
)
from .lifecycle import get_lifecycle, UnsupportedPlatformError

# ── Constants ──────────────────────────────────────────────────────────────────

AGENT_ROOT = Path(__file__).parent.parent          # project root
REMOTE_SCRIPTS_DIR = "scripts"                     # relative to target_root
MCP_HEALTH_TIMEOUT = 10                            # seconds to wait for MCP to respond


# ── Result helpers ────────────────────────────────────────────────────────────

def _ok(target: Optional[str], stage: str, data=None) -> dict:
    return {"ok": True, "target": target or "?", "stage": stage, "data": data}


def _err(target: Optional[str], stage: str, error: str) -> dict:
    return {"ok": False, "target": target or "?", "stage": stage, "error": error}


def _catch(target_name: str, stage: str, func, *args, **kwargs) -> dict:
    """
    Wrapper that catches known exceptions and returns structured errors.
    Unknown exceptions are re-raised.
    """
    try:
        return func(*args, **kwargs)
    except SSHAuthError as e:
        return _err(target_name or "?", stage, f"SSH auth error: {e}")
    except UnsupportedPlatformError as e:
        return _err(target_name or "?", stage, f"Platform error: {e}")
    except ConfigNotFound as e:
        return _err(target_name or "?", stage, f"Config error: {e}")
    except ConfigError as e:
        return _err(target_name or "?", stage, f"Config error: {e}")
    except Exception:
        # Re-raise unknown errors — programming mistakes should not be swallowed
        raise


def _dispatch_lifecycle(cfg: dict):
    """
    Resolve the lifecycle backend for a target's platform.
    Returns (lifecycle, error_dict). On error, lifecycle is None and
    error_dict is a fully-formed _err() response.
    """
    platform = cfg.get("platform", "windows")
    try:
        return get_lifecycle(platform), None
    except UnsupportedPlatformError as e:
        target_name = cfg.get("name") or cfg.get("_target_name") or "?"
        return None, _err(target_name, "platform", str(e))


# ── TCP port check ─────────────────────────────────────────────────────────────

def _is_port_listening(host: str, port: int, timeout: float = 1.0) -> bool:
    """Return True if a TCP connection can be established to host:port."""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        result = sock.connect_ex((host, port))
        sock.close()
        return result == 0
    except Exception:
        return False


def _kill_ports_on_target(cfg: dict, ports: list[int]) -> None:
    """Kill processes listening on the given TCP ports on the remote target.

    This is a best-effort operation — SSH errors are logged but not raised.
    """
    from agent.ssh_runner import run_ssh

    ssh_cfg = cfg.get("ssh", {})
    if not ssh_cfg.get("host"):
        return
    platform = (cfg.get("platform") or "windows").lower()
    import logging
    logger = logging.getLogger(__name__)

    for p in ports:
        if platform == "macos":
            cmd = (
                f"lsof -tiTCP:{p} -sTCP:LISTEN 2>/dev/null "
                f"| xargs -r kill -TERM 2>/dev/null; "
                f"sleep 0.5; "
                f"lsof -tiTCP:{p} -sTCP:LISTEN 2>/dev/null "
                f"| xargs -r kill -KILL 2>/dev/null; true"
            )
        else:
            cmd = (
                f'powershell -Command "'
                f'Get-NetTCPConnection -LocalPort {p} -ErrorAction SilentlyContinue '
                f'| Select-Object -ExpandProperty OwningProcess '
                f'| ForEach-Object {{ Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue }}'
                f'"'
            )
        try:
            run_ssh(ssh_cfg, cmd, timeout=30)
        except Exception as exc:
            logger.warning("Failed to kill port %d on %s: %s", p, ssh_cfg.get("host"), exc)
    time.sleep(1)


# ── Script discovery ───────────────────────────────────────────────────────────

def _local_script(name: str) -> Path | None:
    """Return the absolute path of a local script if it exists."""
    # target/scripts lives at project_root/target/scripts/
    candidates = [
        AGENT_ROOT / "target" / "scripts" / name,
        AGENT_ROOT / "scripts" / name,
    ]
    for p in candidates:
        if p.exists():
            return p
    return None


# ── Remote paths ───────────────────────────────────────────────────────────────

def _remote_scripts_path(target_root: str) -> str:
    """Build the absolute remote scripts directory path."""
    root = target_root.rstrip("\\/")
    return f"{root}\\{REMOTE_SCRIPTS_DIR}"


def _remote_script(target_root: str, name: str) -> str:
    return f"{_remote_scripts_path(target_root)}\\{name}"


# ── MCP health detail ───────────────────────────────────────────────────────────

def health_detail(target_name: str) -> dict:
    """
    Delegate GUI-readiness check to mcp_manager, which correctly handles
    the MCP SSE/Session-Id protocol.
    """
    try:
        from agent import mcp_manager
        return mcp_manager.health_detail(target_name)
    except Exception as e:
        return {
            "ok": False,
            "ready_level": "unreachable",
            "error": f"mcp_manager.health_detail failed: {e}",
        }


# ── Public API ─────────────────────────────────────────────────────────────────

# Structured error codes used across all stages
_ERROR_CODES = frozenset({
    "ssh_auth_failed",
    "ssh_connection_failed",
    "python_not_found",
    "powershell_not_found",
    "backend_mismatch",
    "deploy_failed",
    "deploy_incomplete",
    "deploy_nested_path_error",
    "server_start_failed",
    "server_start_timeout",
    "server_not_running",
    "session0_or_desktop_unavailable",
    "desktop_session_disconnected",
    "gui_not_ready",
    "mcp_initialize_failed",
    "launchagent_registration_failed",
    "task_registration_failed",
    "script_upload_failed",
    "local_script_missing",
    "unsupported_platform",
})


def _is_gui_error_code(code: str) -> bool:
    """Return True for codes that indicate a GUI/session problem requiring user action."""
    return code in {
        "session0_or_desktop_unavailable",
        "desktop_session_disconnected",
        "gui_not_ready",
    }


def list_targets() -> dict:
    """List all configured targets. Returns {"ok": True, "targets": {...}, "default": str}."""
    tn = "?"
    def _impl() -> dict:
        tc = TargetConfig()
        targets = tc.list_targets()
        default = tc.get_default_target()
        return {"ok": True, "targets": targets, "default": default}
    return _catch(tn, "list_targets", _impl)


def probe_target(name: Optional[str] = None) -> dict:
    """
    Probe the target for basic capabilities (SSH, Python, platform tools).

    Returns {"ok": True, "target": str, "stage": "probe", "data": {...}}
    or {"ok": False, "target": str, "stage": "probe", "error": ..., "code": ...}.
    """
    def _do() -> dict:
        tc = TargetConfig()
        target_name = name or tc.get_default_target()
        cfg = tc.get_resolved_target(target_name)
        cfg["_target_name"] = target_name

        lifecycle, err = _dispatch_lifecycle(cfg)
        if err is not None:
            return err
        return lifecycle.probe(cfg)

    try:
        tc = TargetConfig()
        tn = name or tc.get_default_target()
    except Exception:
        tn = name or "?"
    return _catch(tn, "probe", _do)


def deploy_target(name: Optional[str] = None) -> dict:
    """
    Upload the local target/ directory to the remote target_root.

    Returns {"ok": True, "target": str, "stage": "deploy", "data": {...}}
    or {"ok": False, "target": str, "stage": "deploy", "error": ..., "code": ...}.
    """
    def _do() -> dict:
        tc = TargetConfig()
        target_name = name or tc.get_default_target()
        cfg = tc.get_resolved_target(target_name)
        cfg["_target_name"] = target_name

        lifecycle, err = _dispatch_lifecycle(cfg)
        if err is not None:
            return err
        return lifecycle.deploy(cfg)

    try:
        tc = TargetConfig()
        tn = name or tc.get_default_target()
    except Exception:
        tn = name or "?"
    return _catch(tn, "deploy", _do)


def get_target(name: Optional[str] = None) -> dict:
    """Get the resolved config for `name` (or default if None)."""
    tn = name or "?"
    def _impl() -> dict:
        tc = TargetConfig()
        target_name = name or tc.get_default_target()
        cfg = tc.get_resolved_target(target_name)
        return {"ok": True, "target": target_name, "config": cfg}
    return _catch(tn, "get_target", _impl)


def check_server_health(name: Optional[str] = None) -> dict:
    """Check whether the MCP server is reachable and GUI-ready.

    Returns {"ok": True, "target": str, "stage": "health_check", "data": {port_open, server_gui_ready, ...}}.
    Note: MCP-level ready (initialize handshake) is delegated to mcp_manager.py.
    """
    tn = name or "?"
    def _impl() -> dict:
        tc = TargetConfig()
        target_name = name or tc.get_default_target()

        # TCP reachability — does NOT need SSH auth.
        cfg_light = tc.get_target(target_name)
        mcp_cfg = cfg_light["mcp"]
        ssh_cfg = cfg_light["ssh"]

        connect_mode = mcp_cfg["connect_mode"]
        if connect_mode == "direct":
            check_host = ssh_cfg["host"]
            check_port = mcp_cfg["port"]
        elif connect_mode == "local":
            check_host = "127.0.0.1"
            check_port = mcp_cfg["port"]
        elif connect_mode == "tunnel":
            check_host = "127.0.0.1"
            check_port = mcp_cfg["tunnel"]["local_port"]
        else:
            raise ValueError(f"Unsupported mcp.connect_mode={connect_mode!r}")
        mcp_path = mcp_cfg["path"]
        mcp_url = f"http://{check_host}:{check_port}{mcp_path}"

        port_open = _is_port_listening(check_host, check_port)

        # GUI readiness via mcp_manager (correct SSE/MCP session handling)
        gui_data = {"server_gui_ready": False, "list_windows_count": 0, "backend": None}
        if port_open:
            gui_data = health_detail(target_name)
            # health_detail returns full details; extract what we need
            gui_data = {
                "server_gui_ready": gui_data.get("server_gui_ready", False),
                "list_windows_count": gui_data.get("list_windows_count", 0),
                "backend": gui_data.get("backend"),
                "ready_level": gui_data.get("ready_level", "tcp_only"),
            }

        return _ok(target_name, "health_check", {
            "port_open": port_open,
            "mcp_responding": None,          # delegated to mcp_manager
            "ready": port_open,
            "ready_level": gui_data.get("ready_level", "tcp_only"),
            "server_gui_ready": gui_data.get("server_gui_ready", False),
            "list_windows_count": gui_data.get("list_windows_count", 0),
            "backend": gui_data.get("backend"),
            "mcp_url": mcp_url,
            "check_host": check_host,
            "check_port": check_port,
        })
    return _catch(tn, "health_check", _impl)


def install_target_task(name: Optional[str] = None) -> dict:
    """
    Upload scripts and register the platform's service definition
    (Task Scheduler on Windows, LaunchAgent on macOS).

    Dispatches to the platform-specific lifecycle backend.
    Returns {"ok": True, "target": str, "stage": "install", "data": {...}}.
    """
    def _do() -> dict:
        tc = TargetConfig()
        target_name = name or tc.get_default_target()
        cfg = tc.get_resolved_target(target_name)
        cfg["_target_name"] = target_name  # for error reporting

        lifecycle, err = _dispatch_lifecycle(cfg)
        if err is not None:
            return err
        return lifecycle.install(cfg)

    try:
        tc = TargetConfig()
        tn = name or tc.get_default_target()
    except Exception:
        tn = name or "?"
    return _catch(tn, "install", _do)


def repair_target(name: Optional[str] = None, repair: bool = False) -> dict:
    """
    Repair a target by sync-deploying and restarting the MCP server.

    When repair=True, performs a full deploy (upload target/ directory) first,
    then ensures the server is running. When repair=False, just probes and reports
    status without making changes.

    Returns {"ok": True, "target": str, "stage": "repair", "data": {...}} or
            {"ok": False, "target": str, "stage": "repair", "error": ...,
             "code": ..., "details": {...}}.
    """
    tn = name or "?"
    def _do() -> dict:
        tc = TargetConfig()
        target_name = name or tc.get_default_target()

        repair_actions: list[str] = []
        repair_results: dict[str, dict] = {}
        if repair:
            deploy_result = deploy_target(target_name)
            repair_actions.append("deploy_target")
            repair_results["deploy_target"] = deploy_result
            if not deploy_result.get("ok"):
                return deploy_result

            install_result = install_target_task(target_name)
            repair_actions.append("install_target_task")
            repair_results["install_target_task"] = install_result
            if not install_result.get("ok"):
                return install_result

        # Force-recovery remains explicit and does not leak into normal connects.
        ensure_result = ensure_server_running(target_name, repair=repair)
        if repair:
            repair_actions.append("ensure_server_running")
            ensure_snapshot = dict(ensure_result)
            if isinstance(ensure_result.get("data"), dict):
                ensure_snapshot["data"] = dict(ensure_result["data"])
            repair_results["ensure_server_running"] = ensure_snapshot
            ensure_result.setdefault("data", {}).update({
                "repair_actions": repair_actions,
                "repair_results": repair_results,
            })
        return ensure_result

    return _catch(tn, "repair", _do)


def ensure_server_running(name: Optional[str] = None, *, repair: bool = False) -> dict:
    """
    Ensure the MCP server is running and GUI-ready on the target.

    Phase 1: TCP probe — if port is already open, do full GUI readiness check
      and return immediately (no restart).
    Phase 2: if port closed, dispatch to platform lifecycle to start the server,
      then wait for the port and check GUI readiness.

    repair: if True, force-clear the target MCP port before starting. This is
      an explicit recovery path, not normal connect behavior.

    Returns {"ok": True, "target": str, "stage": "ensure", "data": {...}} or
            {"ok": False, "target": str, "stage": "ensure", "error": ...,
             "code": ..., "details": {...}}.
    """
    tn = name or "?"
    def _do() -> dict:
        tc = TargetConfig()
        target_name = name or tc.get_default_target()

        cfg_light = tc.get_target(target_name)
        mcp_cfg = cfg_light["mcp"]
        ssh_cfg = cfg_light["ssh"]

        connect_mode = mcp_cfg["connect_mode"]
        if connect_mode == "direct":
            check_host = ssh_cfg["host"]
            check_port = mcp_cfg["port"]
        elif connect_mode == "local":
            check_host = "127.0.0.1"
            check_port = mcp_cfg["port"]
        elif connect_mode == "tunnel":
            check_host = "127.0.0.1"
            check_port = mcp_cfg["tunnel"]["local_port"]
        else:
            raise ValueError(f"Unsupported mcp.connect_mode={connect_mode!r}")
        mcp_path = mcp_cfg["path"]

        port_open = _is_port_listening(check_host, check_port)

        if port_open:
            # Phase 1: server already running — still need GUI readiness check
            gui_data = health_detail(target_name)
            if gui_data.get("ready_level") == "unreachable":
                port_open = False
            else:
                result = _ok(tn, "ensure", {
                    "status": "already_running",
                    "port": check_port,
                    "mcp_url": tc.build_mcp_url(target_name),
                    "ready_level": gui_data.get("ready_level", "tcp_only"),
                    "server_gui_ready": gui_data.get("server_gui_ready", False),
                    "list_windows_count": gui_data.get("list_windows_count", 0),
                    "backend": gui_data.get("backend"),
                })
                # If GUI is not ready, tell the user what to do
                if not gui_data.get("server_gui_ready"):
                    result["next_action"] = _gui_recovery_action(gui_data)
                return result

        # Phase 2: if repair is explicit, force-clear the target MCP port first.
        if repair:
            target_port = mcp_cfg["port"]
            _kill_ports_on_target(cfg_light, [target_port])

        # Phase 3: port closed — start via lifecycle
        cfg = tc.get_resolved_target(target_name)
        cfg["_target_name"] = target_name
        lifecycle, err = _dispatch_lifecycle(cfg)
        if err is not None:
            return err
        result = lifecycle.ensure_server_running(cfg)

        if result.get("ok"):
            # Lifecycle succeeded — do a GUI readiness check
            time.sleep(2)
            gui_data = health_detail(target_name)
            result.setdefault("data", {})["mcp_url"] = _build_mcp_url(cfg)
            for key in ("ready_level", "server_gui_ready", "list_windows_count", "backend"):
                if key in gui_data:
                    result["data"][key] = gui_data[key]
            if not gui_data.get("server_gui_ready"):
                result["next_action"] = _gui_recovery_action(gui_data)
        else:
            # Lifecycle error — add next_action hint for GUI-related codes
            code = result.get("code", "")
            if _is_gui_error_code(code):
                result["next_action"] = _gui_recovery_action(result.get("details", {}))
            elif code == "server_start_timeout":
                result["next_action"] = (
                    "Server failed to start within the timeout. "
                    "Check the server log on the target for errors. "
                    "Then rerun ensure_server_running."
                )

        return result

    return _catch(tn, "ensure", _do)


def _gui_recovery_action(details: dict) -> str:
    """Return a human-readable next_action string for GUI-related errors."""
    code = details.get("code", "")
    session_id = details.get("server_session_id") or details.get("session_id")
    session_state = details.get("session_state", "")
    window_count = details.get("list_windows_count", 0)

    if code == "desktop_session_disconnected":
        return (
            f"RDP session {session_id} is disconnected. "
            f"Reconnect via RDP or Parallels Console to unlock the desktop session, "
            f"then rerun ensure_server_running."
        )
    if code == "session0_or_desktop_unavailable":
        return (
            f"Server is in Session {session_id} (Session 0). "
            f"Configure auto-login or use Task Scheduler with LogonType=Interactive "
            f"to run in an active desktop session, then rerun ensure_server_running."
        )
    if window_count == 0 and session_state == "active":
        return (
            "Server session is active but no windows are visible. "
            "Ensure EDR/HISEC application is running and has visible windows, "
            "then rerun ensure_server_running."
        )
    if window_count == 0:
        return (
            "GUI automation is unavailable (no visible windows detected). "
            "Verify the desktop session is active and the EDR application is running, "
            "then rerun ensure_server_running."
        )
    return (
        "GUI automation prerequisites not met. "
        "Check server logs and verify the desktop session is accessible, "
        "then rerun ensure_server_running."
    )


def stop_server(name: Optional[str] = None, *, repair: bool = False) -> dict:
    """
    Stop the MCP server process on the target.

    Default (repair=False): no-write, lifecycle-only. The lifecycle backend
    inspects target payload integrity (Phase 1) and refuses with
    target_payload_incomplete when tracked files are missing. The caller
    must run deploy_target() + install_target_task() explicitly.

    repair=True: explicit opt-in to cascading repair. If the lifecycle
    backend refuses with target_payload_incomplete, this entry point
    delegates to repair_target(repair=True) — which may deploy/install —
    and reports the cascade under data.repair_actions. The lifecycle
    backend itself remains strict no-write; the cascade lives only in
    this top-level wrapper.

    Returns {"ok": True, "target": str, "stage": "stop", "data": {...}} or
    {"ok": False, "target": str, "stage": "stop", "error": ..., "code": ...}.
    """
    def _do() -> dict:
        tc = TargetConfig()
        target_name = name or tc.get_default_target()
        cfg = tc.get_resolved_target(target_name)
        cfg["_target_name"] = target_name

        lifecycle, err = _dispatch_lifecycle(cfg)
        if err is not None:
            return err
        result = lifecycle.stop_server(cfg)
        if result.get("ok") or not repair:
            return result

        # repair=True AND lifecycle refused.  Cascade through the
        # explicit repair entry point — never through the lifecycle
        # backend directly, so the backend remains strict no-write.
        if result.get("code") != "target_payload_incomplete":
            # Other lifecycle errors (e.g. SSH failure) are not repairable
            # from this entry point; surface them as-is.
            return result

        repair_result = repair_target(target_name, repair=True)
        repair_data = repair_result.get("data") or {}
        cascade_data = {
            "lifecycle_result": result,
            "repair_result": repair_result,
            "repair_actions": repair_data.get("repair_actions", []),
        }
        if not repair_result.get("ok"):
            return {
                "ok": False,
                "recoverable": False,
                "stage": "stop",
                "code": "stop_repair_failed",
                "error": "Stop payload was missing and the repair cascade failed.",
                "data": cascade_data,
                "next_action": repair_result.get(
                    "next_action",
                    "Inspect data.repair_result, fix the reported error, and retry.",
                ),
            }

        # Repair starts the server as part of readiness verification. Retry the
        # original stop operation so this API still fulfils its requested action.
        retry_result = lifecycle.stop_server(cfg)
        retry_snapshot = dict(retry_result)
        if isinstance(retry_result.get("data"), dict):
            retry_snapshot["data"] = dict(retry_result["data"])
        cascade_data["retry_result"] = retry_snapshot
        if not retry_result.get("ok"):
            return {
                "ok": False,
                "recoverable": False,
                "stage": "stop",
                "code": "stop_retry_failed",
                "error": "Repair succeeded, but the stop retry failed.",
                "data": cascade_data,
                "next_action": retry_result.get(
                    "next_action",
                    "Inspect data.retry_result and retry stop_server().",
                ),
            }

        retry_result["recoverable"] = True
        retry_result.setdefault("data", {}).update(cascade_data)
        return retry_result

    try:
        tc = TargetConfig()
        tn = name or tc.get_default_target()
    except Exception:
        tn = name or "?"
    return _catch(tn, "stop", _do)


def restart_server(name: Optional[str] = None, *, repair: bool = False) -> dict:
    """
    Stop then ensure running. Returns combined result.

    repair is forwarded as-is to both stop_server and ensure_server_running;
    this wrapper does not add its own repair logic (Phase 3 design rule:
    keep cascade logic in the leaf entry points, not duplicated in
    restart_server).
    """
    stop_result = stop_server(name, repair=repair)
    if not stop_result["ok"]:
        return stop_result
    return ensure_server_running(name, repair=repair)


def _build_mcp_url(cfg: dict) -> str:
    """Build the MCP URL from a resolved target config.

    For direct mode, the client connects to ssh.host (the reachable address),
    not mcp.host (which is 0.0.0.0 on the server side).
    """
    mcp_cfg = cfg["mcp"]
    ssh_cfg = cfg["ssh"]
    connect_mode = mcp_cfg["connect_mode"]
    if connect_mode == "direct":
        # Client connects to the SSH host address, not 0.0.0.0
        return f"http://{ssh_cfg['host']}:{mcp_cfg['port']}{mcp_cfg['path']}"
    if connect_mode == "local":
        return f"http://127.0.0.1:{mcp_cfg['port']}{mcp_cfg['path']}"
    if connect_mode == "tunnel":
        local_port = mcp_cfg.get("tunnel", {}).get("local_port", 18765)
        return f"http://127.0.0.1:{local_port}{mcp_cfg['path']}"
    raise ValueError(f"Unsupported mcp.connect_mode={connect_mode!r}")
