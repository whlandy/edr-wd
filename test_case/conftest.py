"""
conftest.py — 共享 fixtures 和环境检查

测试会话通过 target_manager + mcp_manager 初始化：
  1. target_manager.ensure_server_running(target)  →  TCP port open
  2. mcp_manager.initialize(target)              →  MCP session ready

配置来源: config/targets.local.json (EDR_WD_TARGET 或 default_target)
不支持 test_machines.json（遗留，仅作参考）
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

# ── Target resolution (Phase 4) ───────────────────────────────────────────────

from agent.target_config import TargetConfig
from agent.subagent import TargetSubAgentPool


# ---------------------------------------------------------------------------
# Target-aware test fixtures
# ---------------------------------------------------------------------------

_TC: Optional[TargetConfig] = None
_SUBAGENT_POOL: Optional[TargetSubAgentPool] = None


def _subagent_pool() -> TargetSubAgentPool:
    global _SUBAGENT_POOL
    if _SUBAGENT_POOL is None:
        _SUBAGENT_POOL = TargetSubAgentPool.from_config()
    return _SUBAGENT_POOL


def get_target_name() -> str:
    """Resolve target name: EDR_WD_TARGET env var > default_target in config."""
    env_target = os.environ.get("EDR_WD_TARGET")
    if env_target:
        return env_target
    tc = TargetConfig()
    default = tc.get_default_target()
    if not default:
        raise RuntimeError(
            "No EDR_WD_TARGET set and no default_target in config/targets.local.json"
        )
    return default


def ensure_server_running(target: Optional[str] = None) -> tuple[bool, str]:
    """
    Ensure MCP server TCP port is open on the target.

    Delegates to target_manager.ensure_server_running().
    Returns (ok, message).
    """
    name = target or get_target_name()
    result = _subagent_pool().get(name).ensure_running()
    if result["ok"]:
        return True, f"{name}: {result['data'].get('status', 'running')}"
    return False, f"{name}: {result.get('error', 'unknown error')}"


def mcp_initialize(target: Optional[str] = None) -> dict:
    """
    Perform MCP initialize for the target.

    Cached per target — repeated calls for the same target return the same session.
    Different targets get independent sessions.

    Returns mcp_manager.initialize() result dict.
    """
    name = target or get_target_name()
    return _subagent_pool().get(name).initialize_mcp()


# ---------------------------------------------------------------------------
# MCP Streamable HTTP Client (FastMCP 3.x)
# ---------------------------------------------------------------------------

class McpClient:
    """
    JSON-RPC-over-HTTP client using FastMCP 3.x Streamable HTTP transport.

    Accepts either a pre-initialized mcp_manager result dict, or a target name
    which will be resolved via target_manager + mcp_manager.
    """

    def __init__(
        self,
        target: Optional[str] = None,
        mcp_init_result: Optional[dict] = None,
        base_url: Optional[str] = None,
    ):
        """
        Args:
            target:        Target name. Resolved via target_manager + mcp_manager.
            mcp_init_result: Pre-obtained mcp_manager.initialize() result.
                              Priority: if provided, target is ignored and this is used directly.
            base_url:      Direct MCP URL override. Low-level debugging only.
                              Cannot be combined with target or mcp_init_result.

        Priority: mcp_init_result > target > base_url
        If mcp_init_result is set, target is ignored.
        If base_url is set, both mcp_init_result and target must be None.
        """
        provided = sum(bool(x) for x in [target, mcp_init_result, base_url])
        if provided >= 2 and base_url and provided > 1:
            raise ValueError(
                "base_url is a low-level override and cannot be combined with "
                "target or mcp_init_result. Use one or the other."
            )

        self._session_id: Optional[str] = None
        self._base_url: Optional[str] = None
        # Fixed HTTP/1.1 transport: httpx streaming request path caused unstable SSE
        # handling with FastMCP; switch to regular POST + HTTP/1.1 transport.
        self._client = httpx.Client(
            timeout=30.0,
            transport=httpx.HTTPTransport(http1=True),
        )

        if mcp_init_result:
            self._base_url = mcp_init_result["data"]["mcp_url"]
            self._session_id = mcp_init_result["data"]["session_id"]
        elif target:
            init_result = mcp_initialize(target)
            if not init_result["ok"]:
                raise RuntimeError(f"MCP initialize failed: {init_result.get('error')}")
            self._base_url = init_result["data"]["mcp_url"]
            self._session_id = init_result["data"]["session_id"]
        elif base_url:
            self._base_url = base_url
        else:
            # Legacy: use default target
            init_result = mcp_initialize()
            if not init_result["ok"]:
                raise RuntimeError(f"MCP initialize failed: {init_result.get('error')}")
            self._base_url = init_result["data"]["mcp_url"]
            self._session_id = init_result["data"]["session_id"]

    @property
    def session_id(self) -> Optional[str]:
        return self._session_id

    @property
    def mcp_url(self) -> str:
        return self._base_url or ""

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
            resp = self._client.post(
                self._base_url, json=payload, headers=headers
            )
            status = resp.status_code
            session = resp.headers.get("Mcp-Session-Id")
            if session:
                self._session_id = session

            content_type = resp.headers.get("Content-Type", "")

            if "application/json" in content_type:
                body = resp.text
                return json.loads(body)

            # SSE body — read and parse
            raw = resp.text
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
            "protocolVersion": "2025-03-26",
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
# Environment checks (legacy compatibility)
# ---------------------------------------------------------------------------

def check_mcp_server() -> tuple[bool, str]:
    """
    Check if MCP server is responding on the configured MCP URL.
    Lightweight check for pytest collection-time skip markers.

    Do not call TargetSubAgent.ensure_running() here: pytest evaluates
    skipif conditions during collection, and collection must not start or
    repair target servers.
    """
    try:
        target = get_target_name()
        tc = TargetConfig()
        cfg = tc.get_target(target)
        mcp = cfg.get("mcp", {})
        ssh = cfg.get("ssh", {})
        path = mcp.get("path", "/mcp")
        connect_mode = mcp.get("connect_mode", "direct")
        if connect_mode == "tunnel":
            host = "127.0.0.1"
            port = mcp.get("tunnel", {}).get("local_port", 18765)
        elif connect_mode == "local":
            host = "127.0.0.1"
            port = mcp.get("port", 8765)
        else:
            host = ssh.get("host", "")
            port = mcp.get("port", 8765)
        mcp_url = f"http://{host}:{port}{path}"
        port_open = False
        if host:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.settimeout(0.5)
                port_open = sock.connect_ex((host, int(port))) == 0
        if not port_open:
            return False, f"MCP port not open on {mcp_url}"
        return True, f"MCP server responding at {mcp_url}"
    except Exception as e:
        return False, f"MCP server check failed: {e}"


def is_server_online() -> bool:
    """Quick boolean check."""
    ok, _ = check_mcp_server()
    return ok
