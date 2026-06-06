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


# ── Public API ─────────────────────────────────────────────────────────────────

def list_targets() -> dict:
    """List all configured targets. Returns {"ok": True, "targets": {...}, "default": str}."""
    tn = "?"
    def _impl() -> dict:
        tc = TargetConfig()
        targets = tc.list_targets()
        default = tc.get_default_target()
        return {"ok": True, "targets": targets, "default": default}
    return _catch(tn, "list_targets", _impl)


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
    """Check whether the MCP server TCP port is reachable.

    Returns {"ok": True, "target": str, "stage": "health_check", "data": {port_open, ready, ready_level, ...}}.

    Note: MCP-level ready (initialize handshake) is delegated to mcp_manager.py.
    This function only verifies TCP reachability.
    """
    tn = name or "?"
    def _impl() -> dict:
        tc = TargetConfig()
        target_name = name or tc.get_default_target()

        # TCP reachability — does NOT need SSH auth.
        # cfg_light is already normalized by get_target(), so mcp/ssh/windows exist.
        cfg_light = tc.get_target(target_name)
        mcp_cfg = cfg_light["mcp"]
        ssh_cfg = cfg_light["ssh"]

        connect_mode = mcp_cfg["connect_mode"]
        if connect_mode == "direct":
            check_host = ssh_cfg["host"]
            check_port = mcp_cfg["port"]
            mcp_path = mcp_cfg["path"]
            mcp_url = f"http://{check_host}:{check_port}{mcp_path}"
        else:  # tunnel
            check_host = "127.0.0.1"
            check_port = mcp_cfg["tunnel"]["local_port"]
            mcp_path = mcp_cfg["path"]
            mcp_url = f"http://127.0.0.1:{check_port}{mcp_path}"

        # TCP port check only — MCP initialize is handled by mcp_manager.py
        port_open = _is_port_listening(check_host, check_port)

        return _ok(target_name, "health_check", {
            "port_open": port_open,
            "mcp_responding": None,          # delegated to mcp_manager
            "ready": port_open,
            "ready_level": "tcp_only",       # MCP initialize not in scope for target_manager
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


def ensure_server_running(name: Optional[str] = None) -> dict:
    """
    Ensure the MCP server TCP port is listening on the target.

    Phase 1 (TCP probe only — no SSH): if port is already open, return early.
    Phase 2: dispatch to the platform-specific lifecycle backend to start the
    service if needed. Then wait for the port.

    MCP-level ready (initialize handshake) is delegated to mcp_manager.py.

    Returns {"ok": True, "target": str, "stage": "ensure", "data": {...}}.
    """
    tn = name or "?"
    def _do() -> dict:
        tc = TargetConfig()
        target_name = name or tc.get_default_target()

        # Phase 1: TCP reachability check — does NOT require SSH/auth.
        cfg_light = tc.get_target(target_name)
        mcp_cfg = cfg_light["mcp"]
        ssh_cfg = cfg_light["ssh"]

        connect_mode = mcp_cfg["connect_mode"]
        if connect_mode == "direct":
            check_host = ssh_cfg["host"]
        else:
            check_host = "127.0.0.1"
        check_port = mcp_cfg["port"]

        port_open = _is_port_listening(check_host, check_port)
        if port_open:
            return _ok(tn, "ensure", {
                "status": "already_running",
                "port": check_port,
                "ready_level": "tcp_only",
                "note": "MCP initialize handled by mcp_manager",
                "mcp_url": tc.build_mcp_url(target_name),
            })

        # Phase 2: dispatch to platform lifecycle
        cfg = tc.get_resolved_target(target_name)
        cfg["_target_name"] = target_name
        lifecycle, err = _dispatch_lifecycle(cfg)
        if err is not None:
            return err
        result = lifecycle.ensure_server_running(cfg)
        # Decorate with mcp_url for callers
        if result.get("ok") and "mcp_url" not in (result.get("data") or {}):
            result.setdefault("data", {})["mcp_url"] = _build_mcp_url(cfg)
        return result

    return _catch(tn, "ensure", _do)


def stop_server(name: Optional[str] = None) -> dict:
    """
    Stop the MCP server process on the target.
    Returns {"ok": True, "target": str, "stage": "stop", "data": {...}}.
    """
    def _do() -> dict:
        tc = TargetConfig()
        target_name = name or tc.get_default_target()
        cfg = tc.get_resolved_target(target_name)
        cfg["_target_name"] = target_name

        lifecycle, err = _dispatch_lifecycle(cfg)
        if err is not None:
            return err
        return lifecycle.stop_server(cfg)

    try:
        tc = TargetConfig()
        tn = name or tc.get_default_target()
    except Exception:
        tn = name or "?"
    return _catch(tn, "stop", _do)


def restart_server(name: Optional[str] = None) -> dict:
    """Stop then ensure running. Returns combined result."""
    stop_result = stop_server(name)
    if not stop_result["ok"]:
        return stop_result
    return ensure_server_running(name)


def _build_mcp_url(cfg: dict) -> str:
    """Build the MCP URL from a resolved target config.

    For direct mode, the client connects to ssh.host (the reachable address),
    not mcp.host (which is 0.0.0.0 on the server side).
    """
    mcp_cfg = cfg["mcp"]
    ssh_cfg = cfg["ssh"]
    if mcp_cfg["connect_mode"] == "direct":
        # Client connects to the SSH host address, not 0.0.0.0
        return f"http://{ssh_cfg['host']}:{mcp_cfg['port']}{mcp_cfg['path']}"
    else:
        local_port = mcp_cfg.get("tunnel", {}).get("local_port", 18765)
        return f"http://127.0.0.1:{local_port}{mcp_cfg['path']}"
