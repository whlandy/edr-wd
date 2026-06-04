"""
target_manager.py — Multi-target lifecycle management for EDR-WD.

Orchestrates target selection, server lifecycle (install/ensure/trigger/stop),
health checking. Delegates SSH to ssh_runner, MCP to mcp_manager.

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

SSHAuthError / UnsupportedAuthType are caught and returned as structured errors
(not raised), so callers never see raw exceptions.

Requires EDR_WD_ENABLE_POWERSHELL=1 on the Windows target side for full
lifecycle support.
"""

from __future__ import annotations

import json
import os
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
    UnsupportedAuthType,
    run_ssh,
    scp_to,
)

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
    except ConfigNotFound as e:
        return _err(target_name or "?", stage, f"Config error: {e}")
    except ConfigError as e:
        return _err(target_name or "?", stage, f"Config error: {e}")
    except Exception as e:
        # Re-raise unknown errors — programming mistakes should not be swallowed
        raise


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
    Upload scripts to the target and register the Task Scheduler task.
    Returns {"ok": True, "target": str, "stage": "install", "data": {...}}.
    """
    def _do() -> dict:
        tc = TargetConfig()
        target_name = name or tc.get_default_target()
        cfg = tc.get_resolved_target(target_name)
        ssh_cfg = cfg["ssh"]
        win_cfg = cfg["windows"]
        target_root = win_cfg["target_root"]

        # Upload scripts directory
        local_scripts = AGENT_ROOT / "target" / "scripts"
        remote_scripts = _remote_scripts_path(target_root)

        uploaded = []
        for script in ["install_task.ps1", "start_server.ps1", "stop_server.ps1"]:
            local = local_scripts / script
            if not local.exists():
                return _err(target_name, "install", f"Local script not found: {local}")
            rc, err = scp_to(ssh_cfg, str(local), remote_scripts)
            if rc != 0:
                return _err(target_name, "install", f"SCP upload failed for {script}: {err}")
            uploaded.append(script)

        # Run install_task.ps1 on remote
        install_cmd = (
            f"powershell -NoProfile -ExecutionPolicy Bypass -File "
            f"'{remote_scripts}\\install_task.ps1' "
            f"-TaskName '{win_cfg.get('task_name', 'StartEDRMCP')}' "
            f"-TargetRoot '{target_root}'"
        )
        rc, out = run_ssh(ssh_cfg, install_cmd)
        if rc != 0:
            return _err(target_name, "install", f"install_task.ps1 failed (rc={rc}): {out}")

        return _ok(target_name, "install", {"uploaded": uploaded, "install_output": out.strip()})

    try:
        tc = TargetConfig()
        tn = name or tc.get_default_target()
    except Exception:
        tn = name or "?"
    return _catch(tn, "install", _do)


def ensure_server_running(name: Optional[str] = None) -> dict:
    """
    Ensure the MCP server TCP port is listening on the target.

    Steps:
      1. Check if already listening.
      2. If not, stop any process on the MCP port.
      3. Upload updated start_server.ps1.
      4. Trigger the scheduled task.
      5. Wait for the port to become available.

    MCP-level ready (initialize handshake) is delegated to mcp_manager.py.

    Returns {"ok": True, "target": str, "stage": "ensure", "data": {status, ready_level, ...}}.
    """
    tn = name or "?"
    def _do() -> dict:
        tc = TargetConfig()
        target_name = name or tc.get_default_target()

        # Phase 1: TCP reachability check — does NOT require SSH auth.
        # cfg_light is already normalized; mcp/ssh/windows are guaranteed to exist.
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

        # Phase 2: Server not running — need full resolved config with auth
        cfg = tc.get_resolved_target(target_name)
        ssh_cfg = cfg["ssh"]
        mcp_cfg = cfg["mcp"]
        win_cfg = cfg["windows"]
        target_root = win_cfg["target_root"]

        # Step 2: stop any existing process on the port
        stop_cmd = (
            f"powershell -NoProfile -Command \""
            f"Get-NetTCPConnection -LocalPort {check_port} -State Listen "
            f"-ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue; "
            f"exit 0\""
        )
        run_ssh(ssh_cfg, stop_cmd)

        # Step 3: upload updated start_server.ps1
        start_script = _local_script("start_server.ps1")
        remote_start = _remote_script(target_root, "start_server.ps1")
        if start_script:
            scp_to(ssh_cfg, str(start_script), _remote_scripts_path(target_root))

        task_name = win_cfg.get("task_name", "StartEDRMCP")

        # Step 4: trigger scheduled task
        trigger_cmd = f"schtasks /Run /TN \"{task_name}\" /I"
        rc, out = run_ssh(ssh_cfg, trigger_cmd)
        if rc != 0:
            return _err(target_name, "ensure", f"schtasks /Run failed (rc={rc}): {out}")

        # Step 5: wait for port
        max_wait = 15
        waited = 0
        while waited < max_wait:
            if _is_port_listening(check_host, check_port):
                break
            time.sleep(1)
            waited += 1

        if waited >= max_wait:
            return _err(target_name, "ensure", f"Port {check_port} did not open within {max_wait}s after schtasks /Run")

        return _ok(target_name, "ensure", {
            "status": "started",
            "port": check_port,
            "waited_seconds": waited,
            "ready_level": "tcp_only",
            "mcp_url": _build_mcp_url(cfg),
            "note": "MCP initialize handled by mcp_manager",
        })
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
        ssh_cfg = cfg["ssh"]
        mcp_cfg = cfg["mcp"]
        win_cfg = cfg["windows"]
        target_root = win_cfg["target_root"]

        port = mcp_cfg["port"]

        # Try stop_server.ps1 first
        stop_script = _local_script("stop_server.ps1")
        if stop_script:
            remote_stop = _remote_script(target_root, "stop_server.ps1")
            scp_to(ssh_cfg, str(stop_script), _remote_scripts_path(target_root))
            rc, out = run_ssh(ssh_cfg, f"powershell -NoProfile -ExecutionPolicy Bypass -File '{remote_stop}' -Port {port}")
        else:
            # Fallback: kill by port
            rc, out = run_ssh(ssh_cfg,
                f"powershell -NoProfile -Command \"Get-NetTCPConnection -LocalPort {port} -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue; exit 0\"")

        port_still_open = _is_port_listening("127.0.0.1", port)
        return _ok(target_name, "stop", {
            "port_killed": not port_still_open,
            "output": out.strip(),
        })

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
