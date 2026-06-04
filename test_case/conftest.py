"""
conftest.py — 共享 fixtures 和环境检查

从 target/config.json 读取 SSH / server / connection 配置。
MCP 通信使用 FastMCP 3.x Streamable HTTP transport (POST /mcp)。
服务器生命周期通过 SSH + schtasks + mcp_manager 管理。
"""

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

import httpx

# ---------------------------------------------------------------------------
# Config loading — reads target/config.json (same source as mcp_manager)
# ---------------------------------------------------------------------------

def _load_config() -> dict:
    """Load target/config.json relative to this file's parent directory."""
    config_path = Path(__file__).parent.parent / "target" / "config.json"
    with open(config_path, encoding="utf-8") as f:
        return json.load(f)

CONFIG = _load_config()

SSH_HOST    = CONFIG["ssh"]["host"]
SSH_PORT    = CONFIG["ssh"]["port"]
SSH_USER    = CONFIG["ssh"]["user"]
SSH_PASS    = CONFIG["ssh"]["password"]
SERVER_PORT = CONFIG["server"]["port"]

# Connection: try direct first, fall back to tunnel
CONN_PREFS = CONFIG["connection"]["preferred"]
DIRECT_URL  = CONFIG["connection"]["direct_url"]
TUNNEL_URL  = CONFIG["connection"]["tunnel_url"]


def _resolve_mcp_url() -> str:
    """Return the reachable MCP URL (direct or tunnel)."""
    for url in [DIRECT_URL, TUNNEL_URL]:
        import re
        m = re.match(r"http://([^:]+):(\d+)", url)
        if m:
            host, port = m.group(1), int(m.group(2))
            try:
                with socket.create_connection((host, port), timeout=2):
                    return url
            except OSError:
                pass
    return DIRECT_URL  # fallback anyway


MCP_URL = _resolve_mcp_url()

# Legacy alias for run_tests.py compatibility
MACHINE_CONFIG = {
    "host": SSH_HOST,
    "server_port": SERVER_PORT,
    "mcp_url": MCP_URL,
    "ssh_port": SSH_PORT,
    "ssh_user": SSH_USER,
    "ssh_password": SSH_PASS,
}


# ---------------------------------------------------------------------------
# MCP Streamable HTTP Client (FastMCP 3.x)
# ---------------------------------------------------------------------------

class McpClient:
    """
    JSON-RPC-over-HTTP client using FastMCP 3.x Streamable HTTP transport.

    Key differences from legacy SSE client:
      - Uses POST (not GET) for all requests
      - Accepts both application/json and text/event-stream
      - Handles Mcp-Session-Id header for session continuity
      - FastMCP may respond with plain JSON or SSE depending on the method
    """

    def __init__(self, base_url: str = MCP_URL):
        self.base_url = base_url
        self._session_id: Optional[str] = None
        self._client = httpx.Client(timeout=30.0)

    def close(self):
        self._client.close()

    def _do_req(self, method: str, params: dict = None) -> dict:
        """Send JSON-RPC POST request and parse the response."""
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": method,
            "params": params or {},
        }
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id

        try:
            with self._client.stream(
                "POST", self.base_url, json=payload, headers=headers
            ) as resp:
                status = resp.status_code
                session = resp.headers.get("Mcp-Session-Id")
                if session:
                    self._session_id = session

                # FastMCP 3.x may respond with JSON or SSE
                content_type = resp.headers.get("Content-Type", "")

                if "application/json" in content_type:
                    # Plain JSON response
                    body = resp.read().decode("utf-8", errors="replace")
                    return json.loads(body)

                # Otherwise treat as SSE stream
                body_parts = []
                for line in resp.iter_lines():
                    if isinstance(line, bytes):
                        line = line.decode("utf-8", errors="replace")
                    if line.strip() == "":
                        break
                    body_parts.append(line)

                raw = "\n".join(body_parts)
                return self._parse_sse(raw)

        except httpx.HTTPError as e:
            return {"ok": False, "error": f"HTTP error: {e}"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def _parse_sse(self, raw: str) -> dict:
        """Extract JSON from raw SSE text (strip 'data: ' prefix)."""
        lines = raw.splitlines()
        for line in lines:
            line = line.strip()
            if line.startswith("data:"):
                json_str = line[5:].strip()
                try:
                    return json.loads(json_str)
                except json.JSONDecodeError:
                    return {"ok": False, "raw": json_str}
        return {"ok": False, "raw": raw}

    def initialize(self) -> dict:
        return self._do_req("initialize", {
            "protocolVersion": "2025-11-25",
            "clientInfo": {"name": "edr-wd-test", "version": "1.0.0"},
            "capabilities": {},
        })

    def tools_list(self) -> dict:
        return self._do_req("tools/list")

    def call_tool(self, name: str, arguments: dict = None) -> dict:
        result = self._do_req("tools/call", {
            "name": name,
            "arguments": arguments or {},
        })
        if "result" in result:
            data = result["result"]
            if isinstance(data, dict) and "content" in data:
                for block in data["content"]:
                    if block.get("type") == "text":
                        try:
                            return json.loads(block["text"])
                        except Exception:
                            return {"ok": False, "raw": block["text"]}
            return data
        return result


# ---------------------------------------------------------------------------
# Environment checks
# ---------------------------------------------------------------------------

def check_mcp_server() -> tuple[bool, str]:
    """Check if MCP server is responding on the configured MCP URL."""
    try:
        client = McpClient()
        try:
            result = client.initialize()
            if "error" in result:
                return False, f"MCP initialize error: {result['error']}"
            return True, f"MCP server responding at {MCP_URL}"
        finally:
            client.close()
    except Exception as e:
        return False, f"MCP server unreachable: {e}"


def is_server_online() -> bool:
    """Quick boolean check."""
    ok, _ = check_mcp_server()
    return ok


# ---------------------------------------------------------------------------
# Server lifecycle via SSH + schtasks
# ---------------------------------------------------------------------------

def _ssh(cmd: str, timeout: int = 30) -> tuple[int, str]:
    """
    Run a command on the remote Windows host via SSH (sshpass on macOS/Linux).
    Returns (exit_code, stdout+stderr_combined).
    """
    ssh_cmd = [
        "sshpass", "-p", SSH_PASS,
        "ssh",
        "-o", "StrictHostKeyChecking=no",
        "-o", "ConnectTimeout=10",
        "-p", str(SSH_PORT),
        f"{SSH_USER}@{SSH_HOST}",
        cmd,
    ]
    try:
        cp = subprocess.run(ssh_cmd, capture_output=True, timeout=timeout)
        return cp.returncode, (cp.stdout + cp.stderr).decode("utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        return -1, f"SSH command timed out after {timeout}s"
    except FileNotFoundError as e:
        return -1, f"Command not found: {e}"


def _find_target_root() -> str:
    """
    Discover the target root directory on Windows.
    Tries known paths in priority order.
    """
    candidates = [
        "C:\\Users\\admin\\Desktop\\edr-wd-main\\edr-wd-hermes",
        "D:\\skill\\edr-wd",
    ]
    for path in candidates:
        check = f"Test-Path '{path}\\target\\scripts\\start_server.ps1'"
        code, out = _ssh(f"powershell -NoProfile -Command {check}")
        if code == 0 and out.strip() == "True":
            return path
    return candidates[0]  # fallback to first candidate


def ensure_server_running() -> tuple[bool, str]:
    """
    Ensure the MCP server is running on the Windows target.

    Strategy:
      1. If MCP initialize succeeds → already running, done.
      2. Trigger StartEDRMCP via schtasks.
      3. Poll until MCP initialize succeeds or deadline expires.
      4. Return (True, msg) on success, (False, msg) on failure.

    Returns (True, msg) on success, (False, msg) on failure.
    """
    # Fast-path: already running
    ok, _ = check_mcp_server()
    if ok:
        return True, f"MCP server already running at {MCP_URL}"

    # Trigger via Task Scheduler
    TASK_NAME = CONFIG["task"]["name"]
    code, out = _ssh(f"schtasks /Run /TN {TASK_NAME} /I")
    if code != 0:
        return False, f"schtasks /Run failed (exit={code}): {out}"

    # Poll until ready
    deadline = time.time() + 60
    while time.time() < deadline:
        time.sleep(3)
        ok, _ = check_mcp_server()
        if ok:
            return True, f"MCP server started and ready at {MCP_URL}"

    return (
        False,
        f"MCP server did not become ready after triggering {TASK_NAME}.\n"
        f"Last check: {MCP_URL}\n"
        f"Confirm the Windows user is logged in interactively and check:\n"
        f"  - target/logs/start.log  (startup log)\n"
        f"  - target/logs/server.*.log  (server stdout/stderr)\n"
    )


def restart_server() -> tuple[bool, str]:
    """
    Kill existing server process and restart via schtasks.
    """
    TASK_NAME = CONFIG["task"]["name"]
    # Stop existing
    _ssh(f"powershell -NoProfile -Command \"Get-NetTCPConnection -LocalPort {SERVER_PORT} -State Listen -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue\"")
    time.sleep(2)
    # Trigger
    return ensure_server_running()
