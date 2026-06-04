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
from agent import target_manager
from agent import mcp_manager


# ---------------------------------------------------------------------------
# Target-aware test fixtures
# ---------------------------------------------------------------------------

_TC: Optional[TargetConfig] = None
_MCP_INIT_BY_TARGET: dict[str, dict] = {}  # target_name → initialize result


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
    result = target_manager.ensure_server_running(name)
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
    global _MCP_INIT_BY_TARGET
    name = target or get_target_name()
    if name in _MCP_INIT_BY_TARGET:
        return _MCP_INIT_BY_TARGET[name]
    _MCP_INIT_BY_TARGET[name] = mcp_manager.initialize(name)
    return _MCP_INIT_BY_TARGET[name]


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
        self._client = httpx.Client(timeout=30.0)

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
            with self._client.stream(
                "POST", self._base_url, json=payload, headers=headers
            ) as resp:
                status = resp.status_code
                session = resp.headers.get("Mcp-Session-Id")
                if session:
                    self._session_id = session

                content_type = resp.headers.get("Content-Type", "")

                if "application/json" in content_type:
                    body = resp.read().decode("utf-8", errors="replace")
                    return json.loads(body)

                # SSE stream
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
    DEPRECATED: Use target_manager.check_server_health() for target-aware checks.
    """
    try:
        target = get_target_name()
        result = target_manager.check_server_health(target)
        port_open = result["data"]["port_open"]
        mcp_url = result["data"]["mcp_url"]
        if not port_open:
            return False, f"MCP port not open on {mcp_url}"
        return True, f"MCP server responding at {mcp_url}"
    except Exception as e:
        return False, f"MCP server check failed: {e}"


def is_server_online() -> bool:
    """Quick boolean check."""
    ok, _ = check_mcp_server()
    return ok
