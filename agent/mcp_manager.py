"""
mcp_manager.py — EDR-WD MCP Server lifecycle management from the agent side.

Manages the EDR-WD MCP server running on a remote Windows target by:
  1. Loading SSH / server config from target/config.json
  2. Checking if the MCP server is reachable via Streamable HTTP POST /mcp
  3. Remotely triggering it via Windows Task Scheduler (StartEDRMCP task)
  4. Polling until the MCP server is ready

Architecture:
  agent (Mac/Linux)  →  SSH tunnel (optional)  →  Windows target MCP server
                                                     (target/scripts/start_server.ps1)

Usage:
    from mcp_manager import ensure_server_running, install_target_task
    result = ensure_server_running()
"""

from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import time
from typing import Optional

import urllib.request
import urllib.error

logger = logging.getLogger("edr_wd.mcp_manager")

# ── Config loading ────────────────────────────────────────────────────────────

def _load_config() -> dict:
    """
    Load config from target/config.json relative to this file's location.
    Falls back to environment variables, then to known defaults.
    """
    # Resolve target/config.json relative to this file (agent/mcp_manager.py → edr-wd/)
    script_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(script_dir)  # agent/ → project root
    config_path = os.path.join(project_root, "target", "config.json")

    defaults = {
        "ssh": {
            "host": "170.170.11.26",
            "port": 22,
            "user": "admin",
            "password": "whl@123",
        },
        "server": {
            "python_path": "C:\\Program Files\\Python313\\python.exe",
            "host": "0.0.0.0",
            "port": 8765,
            "command": "server.py --http --host 0.0.0.0 --port 8765",
        },
        "task": {"name": "StartEDRMCP"},
        "connection": {
            "preferred": "direct",
            "direct_url": "http://170.170.11.26:8765/mcp",
            "tunnel_url": "http://localhost:18765/mcp",
        },
    }

    if os.path.exists(config_path):
        try:
            with open(config_path) as f:
                user_cfg = json.load(f)
            # Deep-merge user config over defaults
            for section, values in user_cfg.items():
                if section in defaults and isinstance(defaults[section], dict):
                    defaults[section].update(values)
                else:
                    defaults[section] = values
        except Exception as e:
            logger.warning("Failed to load %s: %s — using defaults", config_path, e)

    # Environment variable overrides
    defaults["ssh"]["host"] = os.environ.get("EDR_WD_HOST", defaults["ssh"]["host"])
    defaults["ssh"]["user"] = os.environ.get("EDR_WD_USER", defaults["ssh"]["user"])
    defaults["ssh"].setdefault("password", os.environ.get("EDR_WD_PASS", defaults["ssh"]["password"]))
    defaults["connection"]["preferred"] = os.environ.get("EDR_WD_CONN_PREF", defaults["connection"]["preferred"])

    return defaults

CONFIG = _load_config()

# ── Derived constants ─────────────────────────────────────────────────────────

SSH_HOST    = CONFIG["ssh"]["host"]
SSH_PORT    = CONFIG["ssh"]["port"]
SSH_USER    = CONFIG["ssh"]["user"]
SSH_PASS    = CONFIG["ssh"]["password"]
TASK_NAME   = CONFIG["task"]["name"]
SERVER_PORT = CONFIG["server"]["port"]
DIRECT_URL  = CONFIG["connection"]["direct_url"]
TUNNEL_URL  = CONFIG["connection"]["tunnel_url"]
PREFERRED_CONN = CONFIG["connection"]["preferred"]

POLL_INTERVAL = 3    # seconds between health polls
POLL_DEADLINE  = 60   # stop waiting after this many seconds

# ── Low-level helpers ─────────────────────────────────────────────────────────

def _check_port(host: str, port: int, timeout: float = 3.0) -> bool:
    """Return True if a TCP connection can be established."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((host, port))
        return True
    except Exception:
        return False
    finally:
        s.close()


def _mcp_initialize(mcp_url: str, timeout: float = 5.0) -> tuple[bool, Optional[str], Optional[str]]:
    """
    Send an MCP initialize request via Streamable HTTP POST.

    FastMCP 3.x uses Streamable HTTP transport:
      - POST to /mcp endpoint
      - Accept: application/json, text/event-stream
      - Response: JSON-RPC result OR SSE stream with Mcp-Session-Id

    Returns (ok, session_id, error_msg).
    ok=True means the server accepted the request with a valid MCP session header.
    """
    payload = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "edr-wd-agent", "version": "1.0"},
        },
    }).encode("utf-8")

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }

    req = urllib.request.Request(
        mcp_url,
        data=payload,
        headers=headers,
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.status
            session = resp.headers.get("Mcp-Session-Id")
            content_type = resp.headers.get("Content-Type", "")

            if status in (200, 202) and session:
                return (True, session, None)
            elif status == 400 and session:
                # 400 with session = server understood us but returned error
                body = resp.read().decode("utf-8", errors="replace")
                return (True, session, f"server returned 400: {body[:200]}")
            else:
                return (False, None, f"status={status} session={session}")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:200]
        return (False, None, f"HTTP {e.code}: {body}")
    except Exception as e:
        return (False, None, str(e))


def _ssh(command: str, timeout: int = 30) -> subprocess.CompletedProcess:
    """
    Run a command on the remote Windows host via SSH (sshpass).
    Returns the CompletedProcess result.
    """
    return subprocess.run(
        [
            "sshpass", "-p", SSH_PASS,
            "ssh",
            "-o", "StrictHostKeyChecking=no",
            "-o", "ConnectTimeout=10",
            "-p", str(SSH_PORT),
            f"{SSH_USER}@{SSH_HOST}",
            command,
        ],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


# ── Connection URL resolution ──────────────────────────────────────────────────

def _resolve_mcp_url() -> str:
    """
    Return the MCP URL to use based on preferred connection mode.
    Tries preferred first; falls back to the other if unreachable.
    """
    if PREFERRED_CONN == "direct":
        primary, fallback = DIRECT_URL, TUNNEL_URL
    else:
        primary, fallback = TUNNEL_URL, DIRECT_URL

    # Extract host:port from URL to check reachability
    import re
    m = re.match(r"http://([^:]+):(\d+)", primary)
    if m:
        host, port = m.group(1), int(m.group(2))
        if _check_port(host, port, timeout=2):
            return primary

    # Try fallback
    m = re.match(r"http://([^:]+):(\d+)", fallback)
    if m:
        host, port = m.group(1), int(m.group(2))
        if _check_port(host, port, timeout=2):
            return fallback

    return primary  # return primary anyway and let _mcp_initialize fail clearly


# ── Public API ───────────────────────────────────────────────────────────────

def check_server_health() -> dict:
    """
    Lightweight health check: port open + MCP initialize succeeds.

    Returns a dict with ok, port_open, mcp_ok, session, url fields.
    """
    mcp_url = _resolve_mcp_url()

    import re
    m = re.match(r"http://([^:]+):(\d+)", mcp_url)
    if m:
        host, port = m.group(1), int(m.group(2))
        port_open = _check_port(host, port, timeout=3)
    else:
        port_open = False

    if not port_open:
        return {"ok": False, "port_open": False, "mcp_ok": False, "session": None, "url": mcp_url}

    mcp_ok, session, error = _mcp_initialize(mcp_url)
    return {
        "ok": mcp_ok,
        "port_open": True,
        "mcp_ok": mcp_ok,
        "session": session,
        "url": mcp_url,
        "error": error,
    }


def trigger_target_server() -> subprocess.CompletedProcess:
    """
    Trigger the StartEDRMCP scheduled task on the target via schtasks.
    This does NOT wait for the server to be ready.
    """
    logger.info("Triggering %s on %s", TASK_NAME, SSH_HOST)
    return _ssh(f"schtasks /Run /TN {TASK_NAME} /I")


def install_target_task() -> dict:
    """
    Run install_task.ps1 on the target via SSH.
    Installs (or updates) the StartEDRMCP scheduled task.

    Returns {"ok": True} on success, {"ok": False, "error": "..."} on failure.
    """
    logger.info("Installing %s task on %s", TASK_NAME, SSH_HOST)

    # Detect target root via the same $PSScriptRoot logic used by the scripts
    # by reading back the target root from the target's own scripts directory
    detect_cmd = (
        "powershell -NoProfile -ExecutionPolicy Bypass -Command "
        "\"$d='C:\\Users\\admin\\Desktop\\edr-wd-main\\edr-wd-hermes'; "
        "if (Test-Path (Join-Path $d 'target\\scripts\\install_task.ps1')) { "
        "echo $d } else { "
        "$d2='D:\\skill\\edr-wd'; if (Test-Path (Join-Path $d2 'target\\scripts\\install_task.ps1')) { echo $d2 } "
        "else { echo 'NOTFOUND' } }\""
    )
    detect_result = _ssh(detect_cmd)
    if detect_result.returncode != 0 or "NOTFOUND" in detect_result.stdout:
        # Fallback: try known Desktop path directly
        install_cmd = (
            "powershell -NoProfile -ExecutionPolicy Bypass -Command "
            "\"& 'C:\\Users\\admin\\Desktop\\edr-wd-main\\edr-wd-hermes\\target\\scripts\\install_task.ps1'\""
        )
    else:
        detected_path = detect_result.stdout.strip()
        install_cmd = (
            f"powershell -NoProfile -ExecutionPolicy Bypass -Command "
            f"\"& '{detected_path}\\target\\scripts\\install_task.ps1'\""
        )

    result = _ssh(install_cmd)
    if result.returncode != 0:
        logger.error("install_task failed: %s", result.stderr or result.stdout)
        return {"ok": False, "error": result.stderr or result.stdout}
    return {"ok": True}


def read_target_logs(lines: int = 20) -> dict:
    """
    Read the tail of target/logs/start.log and the most recent server.log.
    Returns {"start_log": "...", "server_log": "..."}.
    """
    start_content = ""
    server_content = ""

    # Try to find target root
    detect_cmd = (
        "powershell -NoProfile -Command "
        "\"$d='C:\\Users\\admin\\Desktop\\edr-wd-main\\edr-wd-hermes'; "
        "if (Test-Path (Join-Path $d 'target\\logs')) { echo $d } else { "
        "$d2='D:\\skill\\edr-wd'; if (Test-Path (Join-Path $d2 'target\\logs')) { echo $d2 } "
        "else { echo 'NOTFOUND' } }\""
    )
    detect_result = _ssh(detect_cmd)
    if detect_result.returncode == 0 and "NOTFOUND" not in detect_result.stdout:
        target_root = detect_result.stdout.strip()
        start_log_path = f"{target_root}\\target\\logs\\start.log"
        # Read start.log
        start_result = _ssh(
            f"powershell -NoProfile -Command \"Get-Content '{start_log_path}' -Tail {lines} -ErrorAction SilentlyContinue\""
        )
        if start_result.returncode == 0:
            start_content = start_result.stdout

        # Find most recent server.log
        find_cmd = (
            f"powershell -NoProfile -Command "
            f"\"Get-ChildItem '{target_root}\\target\\logs\\server.*.log' | "
            f"Sort-Object LastWriteTime -Descending | Select-Object -First 1 | "
            f"ForEach-Object {{ Get-Content $_.FullName -Tail {lines} }}\""
        )
        server_result = _ssh(find_cmd)
        if server_result.returncode == 0:
            server_content = server_result.stdout

    return {"start_log": start_content, "server_log": server_content}


def ensure_server_running() -> dict:
    """
    Ensure the target MCP server is reachable.

    Strategy:
      1. If MCP initialize succeeds → done (already running).
      2. Otherwise trigger StartEDRMCP via schtasks.
      3. Poll until MCP initialize succeeds or deadline expires.
      4. Return {"ok": True, "session": session_id} on success,
         {"ok": False, "stage": "...", "error": "..."} on failure.

    The SSH tunnel (localhost:18765 → Windows:8765) must already be running
    before calling this.  Use the tunnel.sh script or set up the tunnel
    separately.
    """
    mcp_url = _resolve_mcp_url()

    # ── Step 1: fast-path — already running ─────────────────────────────────
    mcp_ok, session, err = _mcp_initialize(mcp_url)
    if mcp_ok:
        logger.info("MCP server already healthy (session=%s, url=%s)", session, mcp_url)
        return {"ok": True, "session": session, "already_running": True, "url": mcp_url}

    # ── Step 2: trigger via Task Scheduler ───────────────────────────────────
    logger.info("MCP server not reachable at %s, triggering %s...", mcp_url, TASK_NAME)
    trigger_result = trigger_target_server()
    if trigger_result.returncode != 0:
        err_msg = trigger_result.stderr or trigger_result.stdout
        return {
            "ok": False,
            "stage": "trigger",
            "error": f"schtasks /Run failed: {err_msg}",
            "url": mcp_url,
        }

    # ── Step 3: poll until ready ─────────────────────────────────────────────
    deadline = time.time() + POLL_DEADLINE
    last_error = None

    while time.time() < deadline:
        time.sleep(POLL_INTERVAL)
        mcp_ok, session, err = _mcp_initialize(mcp_url)
        if mcp_ok:
            logger.info("MCP server ready (session=%s, url=%s)", session, mcp_url)
            return {"ok": True, "session": session, "already_running": False, "url": mcp_url}

        remaining = deadline - time.time()
        last_error = err or f"still not ready after {POLL_DEADLINE - remaining:.0f}s"
        logger.debug("Waiting for MCP server at %s... (%.0fs left)", mcp_url, remaining)

    # ── Step 4: deadline expired — collect diagnostic logs ──────────────────
    logs = read_target_logs(lines=20)
    return {
        "ok": False,
        "stage": "wait_mcp_ready",
        "error": (
            f"MCP server did not become ready after triggering {TASK_NAME}. "
            f"Last error: {last_error}. "
            "Confirm the Windows user is logged in interactively and check target/logs/."
        ),
        "url": mcp_url,
        "start_log": logs["start_log"],
        "server_log": logs["server_log"],
        "suggestion": (
            "Ensure Windows is logged in to an interactive desktop. "
            "Run health.ps1 or check target/logs/ on the Windows host for server startup errors."
        ),
    }
