"""
conftest.py — 共享 fixtures 和环境检查

从 target/config/test_machines.json 读取测试机配置。
默认使用 default 机器，可通过 EDR_WD_MACHINE 环境变量切换。

MCP 通信走 direct HTTP（SSE），不再依赖 SSH tunnel。
服务器生命周期管理通过 SSH + schtasks 操作。
"""

import json
import os
import socket
import time
from pathlib import Path
from typing import Optional

import httpx

# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------
def _load_config() -> dict:
    """Load test_machines.json relative to this file's parent directory."""
    config_path = Path(__file__).parent.parent / "config" / "test_machines.json"
    with open(config_path, encoding="utf-8") as f:
        return json.load(f)

def _get_machine_config() -> dict:
    """Return the active machine config dict based on EDR_WD_MACHINE env var."""
    config = _load_config()
    name = os.environ.get("EDR_WD_MACHINE", config.get("default", ""))
    machines = config.get("machines", {})
    if name not in machines:
        raise ValueError(f"Machine '{name}' not found in test_machines.json. Available: {list(machines.keys())}")
    return machines[name]

# Expose MCP URL for McpClient
MACHINE_CONFIG = _get_machine_config()
MCP_BASE_URL = MACHINE_CONFIG.get("mcp_url", f"http://{MACHINE_CONFIG['host']}:8765/mcp")


# ---------------------------------------------------------------------------
# MCP HTTP Client (httpx + SSE streaming for FastMCP 3.x)
# ---------------------------------------------------------------------------

class McpClient:
    """
    Lightweight JSON-RPC-over-HTTP client for FastMCP 3.x SSE transport.

    FastMCP 3.x returns Server-Sent Events (SSE) where each JSON-RPC response
    arrives as an `event: message\\ndata: {...}\\n\\n` block.  The HTTP response
    stream stays open until the server closes it, so we must stream-read
    the SSE body rather than calling resp.read() which would block forever.
    """

    def __init__(self, base_url: str = MCP_BASE_URL):
        self.base_url = base_url
        self._session_id: Optional[str] = None
        self._client = httpx.Client(timeout=30.0)

    def close(self):
        self._client.close()

    def _do_req(self, method: str, params: dict = None) -> dict:
        """Send JSON-RPC request and stream-parse SSE response."""
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
                if not self._session_id:
                    self._session_id = resp.headers.get("mcp-session-id")

                body_parts = []
                for line in resp.iter_lines():
                    if isinstance(line, bytes):
                        line_str = line.decode("utf-8", errors="replace")
                    else:
                        line_str = line
                    if line_str.strip() == "":
                        break
                    body_parts.append(line_str)

                raw = "\n".join(body_parts)
                return self._parse_sse(raw)
        except httpx.HTTPError as e:
            return {"ok": False, "error": f"HTTP error: {e}"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def _parse_sse(self, raw: str) -> dict:
        """Extract JSON from raw SSE text (strip 'event: ...\\ndata: ' prefix)."""
        lines = raw.splitlines()
        for line in lines:
            if line.startswith("data:"):
                json_str = line[5:].strip()
                try:
                    return json.loads(json_str)
                except json.JSONDecodeError:
                    return {"ok": False, "raw": json_str}
        return {"ok": False, "raw": raw}

    def initialize(self) -> dict:
        return self._do_req("initialize", {
            "protocolVersion": "2024-11-05",
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
            return True, f"MCP server responding at {MCP_BASE_URL}"
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

import subprocess
import sys


def _get_ssh_cmd(host: str, port: int, user: str, password: str, cmd: str) -> list:
    """
    Build an SSH command list. Uses sshpass on Linux/macOS; on Windows falls back
    to the system's own SSH client.
    """
    is_windows = sys.platform.startswith("win")
    if is_windows:
        # Windows: use system OpenSSH (no sshpass needed)
        return [
            "ssh", "-o", "StrictHostKeyChecking=no",
            "-o", f"Port={port}",
            f"{user}@{host}", cmd
        ]
    else:
        # Linux/macOS: use sshpass
        return [
            "sshpass", "-p", password,
            "ssh", "-o", "StrictHostKeyChecking=no",
            "-o", f"Port={port}",
            f"{user}@{host}", cmd
        ]


def _ssh(host: str, port: int, user: str, password: str, cmd: str, timeout: int = 20) -> tuple[int, str]:
    """
    Run a command via SSH and return (exit_code, stdout+stderr).
    Works on both Linux/macOS (sshpass) and Windows (system OpenSSH).
    """
    ssh_cmd = _get_ssh_cmd(host, port, user, password, cmd)
    try:
        cp = subprocess.run(
            ssh_cmd,
            capture_output=True, timeout=timeout,
        )
        return cp.returncode, (cp.stdout + cp.stderr).decode("utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        return -1, f"SSH command timed out after {timeout}s"
    except FileNotFoundError as e:
        return -1, f"Command not found: {e}"


def ensure_server_running(
    host: str = None,
    port: int = None,
    user: str = None,
    password: str = None,
    server_path: str = None,
    server_port: int = None,
) -> tuple[bool, str]:
    """
    Ensure the MCP server is running on the target Windows machine:

      1. Kill any process holding the server port via schtasks.
      2. Start a new server process via schtasks /Run.
      3. Wait for it to become reachable via direct HTTP.

    Values come from test_machines.json if not passed as arguments.

    Returns (True, msg) on success, (False, msg) on failure.
    """
    cfg = MACHINE_CONFIG
    host       = host       or cfg["host"]
    port       = port       or cfg["port"]
    user       = user       or cfg["user"]
    password   = password   or cfg["password"]
    server_path = server_path or cfg["server_path"]
    server_port = server_port or cfg.get("server_port", 8765)

    # Step 1: kill old server via schtasks on target
    kill_cmd = (
        f'powershell -ExecutionPolicy Bypass -Command '
        f'"Get-NetTCPConnection -LocalPort {server_port} -ErrorAction SilentlyContinue | '
        f'ForEach-Object {{ Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }}; '
        f'Start-Sleep -Seconds 1"'
    )
    rc, out = _ssh(host, port, user, password, kill_cmd, timeout=20)
    # rc may be 0 even if nothing was running — that's fine

    # Step 2: start server via schtasks
    start_cmd = (
        f'powershell -ExecutionPolicy Bypass -Command '
        f'"schtasks /Run /TN StartEDRMCP; Start-Sleep -Seconds 3"'
    )
    rc, out = _ssh(host, port, user, password, start_cmd, timeout=15)

    # Step 3: wait for server to be reachable (up to 20s)
    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            with socket.create_connection((host, server_port), timeout=2):
                pass
        except OSError:
            time.sleep(1)
            continue
        # Port is open — give server a moment to finish initialization
        time.sleep(2)
        # Verify server is responding to MCP initialize
        try:
            client = McpClient()
            try:
                result = client.initialize()
                if "error" not in result:
                    return True, f"Server started and responding at {host}:{server_port}"
            finally:
                client.close()
        except Exception:
            time.sleep(1)
            continue
    return False, f"Server did not respond within 20s at {host}:{server_port}"


def restart_server_via_ssh(
    host: str = None,
    port: int = None,
    user: str = None,
    password: str = None,
) -> tuple[bool, str]:
    """
    Convenience wrapper: kill server processes and restart them via schtasks.
    """
    return ensure_server_running(host, port, user, password)
