"""
conftest.py — 共享 fixtures 和环境检查
FastMCP 3.x 使用 SSE 流式响应，必须用 httpx 流式读取。
"""

import json
import os
from typing import Optional

import httpx

# ---------------------------------------------------------------------------
# Config (from environment or defaults)
# ---------------------------------------------------------------------------
TUNNEL_HOST = os.environ.get("EDR_WD_TUNNEL_HOST", "127.0.0.1")
TUNNEL_PORT = int(os.environ.get("EDR_WD_TUNNEL_PORT", "18765"))
MCP_BASE_URL = f"http://{TUNNEL_HOST}:{TUNNEL_PORT}/mcp"


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
            # stream=True keeps the connection open and lets us read line-by-line
            with self._client.stream(
                "POST", self.base_url, json=payload, headers=headers
            ) as resp:
                # Extract session ID from response headers (available before body)
                if not self._session_id:
                    self._session_id = resp.headers.get("mcp-session-id")

                body = b""
                # Read SSE stream: each line is b"event: ...", b"data: ...",
                # or b"" (blank line = end of event)
                # The server sends one `event: message\\ndata: {json}\\n\\n` per
                # JSON-RPC response, then closes the chunked stream.
                body_parts = []
                for line in resp.iter_lines():
                    if isinstance(line, bytes):
                        line_str = line.decode("utf-8", errors="replace")
                    else:
                        line_str = line

                    # Blank line = end of this SSE event
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
        result = self._do_req("initialize", {
            "protocolVersion": "2024-11-05",
            "clientInfo": {"name": "edr-wd-test", "version": "1.0.0"},
            "capabilities": {},
        })
        return result

    def tools_list(self) -> dict:
        return self._do_req("tools/list")

    def call_tool(self, name: str, arguments: dict = None) -> dict:
        result = self._do_req("tools/call", {
            "name": name,
            "arguments": arguments or {},
        })
        # Unwrap FastMCP tool response envelope
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

def check_tunnel() -> tuple[bool, str]:
    """Check if SSH tunnel is listening on local port."""
    import socket
    try:
        with socket.create_connection((TUNNEL_HOST, TUNNEL_PORT), timeout=3):
            return True, f"Tunnel open on {TUNNEL_HOST}:{TUNNEL_PORT}"
    except OSError as e:
        return False, f"Tunnel not reachable on {TUNNEL_HOST}:{TUNNEL_PORT}: {e}"


def check_mcp_server() -> tuple[bool, str]:
    """Check if MCP server is responding on the tunnel."""
    try:
        client = McpClient()
        try:
            result = client.initialize()
            if "error" in result:
                return False, f"MCP initialize error: {result['error']}"
            return True, "MCP server responding"
        finally:
            client.close()
    except Exception as e:
        return False, f"MCP server unreachable: {e}"


def is_server_online() -> bool:
    """Quick boolean check."""
    ok, _ = check_mcp_server()
    return ok


# ---------------------------------------------------------------------------
# Server lifecycle via SSH
# ---------------------------------------------------------------------------

import subprocess
import time


def _ssh(host: str, port: int, user: str, password: str, cmd: str, timeout: int = 15) -> tuple[int, str]:
    """Run a command via SSH and return (exit_code, stdout+stderr)."""
    cp = subprocess.run(
        [
            "sshpass", "-p", password,
            "ssh", "-o", "StrictHostKeyChecking=no",
            "-o", f"Port={port}",
            f"{user}@{host}",
            cmd
        ],
        capture_output=True, timeout=timeout,
    )
    return cp.returncode, (cp.stdout + cp.stderr).decode("utf-8", errors="replace")


def ensure_server_running(
    host: str = "170.170.11.26",
    port: int = 22,
    user: str = "admin",
    password: str = "whl@123",
    local_tunnel_port: int = 18765,
    server_path: str = "C:/Users/admin/Desktop/edr-wd-main/edr-wd-main/target",
    server_port: int = 8765,
) -> tuple[bool, str]:
    """
    Ensure the MCP server is running on Windows:
      1. Kill any process holding the server port.
      2. Start a new server process via SSH (Start-Process, background).
      3. Wait for it to become reachable via the local tunnel.

    Returns (True, msg) on success, (False, msg) on failure.
    """
    kill_script = "C:/Users/admin/Desktop/kill_edr.ps1"

    # Step 1: kill old processes
    rc, out = _ssh(host, port, user, password,
                   f'powershell -ExecutionPolicy Bypass -Command "& {{{kill_script}}}"',
                   timeout=20)
    if rc != 0:
        return False, f"kill script failed: {out}"

    # Step 2: start new server via the start script (background)
    start_script = "C:/Users/admin/Desktop/start_edr.ps1"
    rc, out = _ssh(host, port, user, password,
                    f'powershell -ExecutionPolicy Bypass -File "{start_script}"',
                    timeout=15)
    # Start-Process returns immediately, so rc may be 0 even if it worked

    # Step 3: wait for server to be reachable via local tunnel (up to 20s)
    import socket
    deadline = time.time() + 20
    while time.time() < deadline:
        # First check tunnel port is open
        try:
            with socket.create_connection(("127.0.0.1", local_tunnel_port), timeout=2):
                pass
        except OSError:
            time.sleep(1)
            continue
        # Tunnel is open — give server a moment to finish initialization
        time.sleep(2)
        # Verify server is responding to MCP initialize
        try:
            client = McpClient()
            try:
                result = client.initialize()
                if "error" not in result:
                    return True, "Server started and responding"
            finally:
                client.close()
        except Exception as e:
            # Server not ready yet — log and retry
            time.sleep(1)
            continue
    return False, "Server did not respond within 20s"


def restart_server_via_ssh(
    host: str = "170.170.11.26",
    port: int = 22,
    user: str = "admin",
    password: str = "whl@123",
) -> tuple[bool, str]:
    """
    Convenience wrapper: kill server processes and restart them.
    Uses kill_edr.ps1 to stop existing processes, then launches a fresh
    edr_wd.server in the background on Windows.
    """
    return ensure_server_running(host, port, user, password)
