"""
mcp_manager.py — EDR-WD MCP Server lifecycle management from the agent side.

Manages the EDR-WD MCP server running on a remote Windows target by:
  1. Checking if the MCP server is reachable.
  2. Remotely triggering it via Windows Task Scheduler (StartEDRMCP task).
  3. Polling until the MCP server is ready.

Architecture:
  agent (Mac/Linux)  →  SSH tunnel  →  Windows target MCP server
                         (tunnel.sh)     (target/scripts/start_server.ps1)

The agent NEVER starts Python directly over SSH.  Instead it calls
`schtasks /Run /TN StartEDRMCP` which fires the scheduled task in the
interactive user session.

Usage (within an agent tool or skill):
    from mcp_manager import ensure_server_running, install_target_task

    # One-shot: ensure server is up, install task if needed.
    await ensure_server_running(tunnel_port=18765, target_host="192.168.3.23",
                                ssh_user="whl", ssh_pass="...")

    # Install the scheduled task on the target (one-time setup).
    await install_target_task(target_host="192.168.3.23", ssh_user="whl", ssh_pass="...")

Environment variables (also respected):
    EDR_WD_HOST       Remote Windows IP  (default: 192.168.3.23)
    EDR_WD_USER       SSH username       (default: whl)
    EDR_WD_PASS       SSH password       (default: from ~/.ssh/.tunnelpass)
    EDR_WD_LOCAL_PORT Local tunnel port  (default: 18765)
    EDR_WD_REMOTE_PORT Remote MCP port   (default: 8765)
"""

from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import time
import urllib.request
import urllib.error
from typing import Optional

logger = logging.getLogger("edr_wd.mcp_manager")

# ── Defaults ─────────────────────────────────────────────────────────────────
DEFAULT_HOST = os.environ.get("EDR_WD_HOST", "192.168.3.23")
DEFAULT_USER = os.environ.get("EDR_WD_USER", "whl")
DEFAULT_LOCAL_PORT = int(os.environ.get("EDR_WD_LOCAL_PORT", "18765"))
DEFAULT_REMOTE_PORT = int(os.environ.get("EDR_WD_REMOTE_PORT", "8765"))
DEFAULT_SSH_PASS_FILE = os.path.expanduser("~/.ssh/.tunnelpass")

POLL_INTERVAL = 3          # seconds between health polls
POLL_DEADLINE = 60         # stop waiting after this many seconds


# ── Low-level helpers ────────────────────────────────────────────────────────

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


def _mcp_initialize(base_url: str, timeout: float = 5.0) -> tuple[bool, Optional[str]]:
    """
    Send an MCP initialize request to the server.

    Returns (ok, session_id).  ok=True means the server responded with a
    valid MCP session header.  session_id is None on failure.
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
    }).encode()

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }

    req = urllib.request.Request(
        f"http://{base_url}/mcp",
        data=payload,
        headers=headers,
        method="GET",
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            session = resp.headers.get("Mcp-Session-Id")
            return (resp.status in (200, 400) and session is not None, session)
    except Exception as e:
        logger.debug("_mcp_initialize failed: %s", e)
        return False, None


def _ssh(
    host: str,
    user: str,
    pass_file: str,
    command: str,
    timeout: int = 30,
) -> subprocess.CompletedProcess:
    """
    Run a command on the remote Windows host via SSH (using sshpass).
    Returns the CompletedProcess result.
    """
    return subprocess.run(
        [
            "sshpass", "-f", pass_file,
            "ssh",
            "-o", "StrictHostKeyChecking=no",
            "-o", "ConnectTimeout=10",
            "-p", "22",                  # direct SSH to Windows, not tunnel
            f"{user}@{host}",
            command,
        ],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


# ── Public API ───────────────────────────────────────────────────────────────

def check_server_health(local_port: int = DEFAULT_LOCAL_PORT) -> dict:
    """
    Lightweight health check: port open + MCP initialize succeeds.

    Returns a dict with ok, port_open, mcp_ok, session fields.
    """
    base_url = f"127.0.0.1:{local_port}"
    port_open = _check_port("127.0.0.1", local_port, timeout=3)
    if not port_open:
        return {"ok": False, "port_open": False, "mcp_ok": False, "session": None}

    mcp_ok, session = _mcp_initialize(base_url)
    return {
        "ok": mcp_ok,
        "port_open": True,
        "mcp_ok": mcp_ok,
        "session": session,
    }


def trigger_target_server(
    host: str = DEFAULT_HOST,
    user: str = DEFAULT_USER,
    pass_file: str = DEFAULT_SSH_PASS_FILE,
) -> subprocess.CompletedProcess:
    """
    Trigger the StartEDRMCP scheduled task on the target via schtasks.

    This does NOT wait for the server to be ready — the caller must poll.
    """
    cmd = "schtasks /Run /TN StartEDRMCP"
    logger.info("Triggering StartEDRMCP on %s", host)
    return _ssh(host, user, pass_file, cmd)


def install_target_task(
    host: str = DEFAULT_HOST,
    user: str = DEFAULT_USER,
    pass_file: str = DEFAULT_SSH_PASS_FILE,
    script_path: str = "target/scripts/install_task.ps1",
) -> dict:
    """
    Run the install_task.ps1 script on the target via SSH.

    This installs (or updates) the StartEDRMCP scheduled task so that it
    exists before the first trigger.

    Returns {"ok": True} on success, {"ok": False, "error": "..."} on failure.
    """
    logger.info("Installing StartEDRMCP task on %s", host)
    # Copy the install script content and execute it remotely via PowerShell
    # Read the local script to send
    # We execute it as a one-liner to avoid file-transfer complexity
    cmd = (
        f"cd ~ && powershell -NoProfile -ExecutionPolicy Bypass "
        f"-Command \"& {{{script_path}}}\""
    )
    result = _ssh(host, user, pass_file, cmd)
    if result.returncode != 0:
        logger.error("install_task failed: %s", result.stderr)
        return {"ok": False, "error": result.stderr or result.stdout}
    return {"ok": True}


def ensure_server_running(
    local_port: int = DEFAULT_LOCAL_PORT,
    host: str = DEFAULT_HOST,
    user: str = DEFAULT_USER,
    pass_file: str = DEFAULT_SSH_PASS_FILE,
    poll_interval: float = POLL_INTERVAL,
    poll_deadline: float = POLL_DEADLINE,
) -> dict:
    """
    Ensure the target MCP server is reachable.

    Strategy:
      1. If `local_port` is reachable and MCP initialize succeeds → done.
      2. Otherwise trigger StartEDRMCP via schtasks.
      3. Poll until MCP initialize succeeds or deadline expires.
      4. Return {"ok": True, "session": session_id} on success,
         {"ok": False, "stage": "...", "error": "..."} on failure.

    Note: the caller is responsible for setting up the SSH tunnel
    (via tunnel.sh) before calling this.
    """
    base_url = f"127.0.0.1:{local_port}"

    # ── Step 1: fast-path — already running ─────────────────────────────────
    mcp_ok, session = _mcp_initialize(base_url)
    if mcp_ok:
        logger.info("MCP server already healthy (session=%s)", session)
        return {"ok": True, "session": session, "already_running": True}

    # ── Step 2: trigger via Task Scheduler ───────────────────────────────────
    logger.info("MCP server not reachable, triggering StartEDRMCP...")
    trigger_result = trigger_target_server(host, user, pass_file)
    if trigger_result.returncode != 0:
        err = trigger_result.stderr or trigger_result.stdout
        return {
            "ok": False,
            "stage": "trigger",
            "error": f"schtasks /Run failed: {err}",
        }

    # ── Step 3: poll until ready ─────────────────────────────────────────────
    deadline = time.time() + poll_deadline
    last_error = None

    while time.time() < deadline:
        time.sleep(poll_interval)
        mcp_ok, session = _mcp_initialize(base_url)
        if mcp_ok:
            logger.info("MCP server ready (session=%s)", session)
            return {"ok": True, "session": session, "already_running": False}

        remaining = deadline - time.time()
        last_error = f"still not ready after {(POLL_DEADLINE - remaining):.0f}s"
        logger.debug("Waiting for MCP server... (%.0fs left)", remaining)

    return {
        "ok": False,
        "stage": "wait_mcp_ready",
        "error": (
            "MCP server did not become ready after triggering StartEDRMCP. "
            f"Last poll: {last_error}. "
            "Confirm the Windows user is logged in and check target/logs/."
        ),
        "suggestion": (
            "Ensure Windows is logged in to an interactive desktop. "
            "Check target/logs/ on the Windows host for server startup errors."
        ),
    }
