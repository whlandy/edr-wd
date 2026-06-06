"""
mcp_manager.py — EDR-WD MCP Server lifecycle management from the agent side.

Responsibilities (Phase 3):
  - Receive a target name or pre-resolved target config
  - Build the MCP URL via target_manager's _build_mcp_url helper
  - Perform MCP initialize (Streamable HTTP + SSE)
  - Return structured result with session_id

Not responsible for:
  - SSH / server start / Task Scheduler (delegated to target_manager)
  - Config loading (delegated to TargetConfig)

Architecture:
  target_manager.ensure_server_running()  →  TCP port open (tcp_only)
  mcp_manager.initialize(target_name)     →  MCP session ready

Usage:
  from agent.mcp_manager import initialize, get_mcp_tools, call_mcp_tool
  result = initialize("win-dev")
  if result["ok"]:
      tools = get_mcp_tools(result["session_id"])
"""

from __future__ import annotations

import json
import re
import socket
import urllib.request
import urllib.error
from typing import Optional

# Re-export for backward compatibility with existing callers
from .target_config import TargetConfig

# ── Constants ──────────────────────────────────────────────────────────────────

MCP_INIT_TIMEOUT = 10.0   # seconds to wait for MCP initialize response
MCP_PROTOCOL_VERSION = "2025-03-26"  # confirmed working with the server


# ── Low-level MCP HTTP ────────────────────────────────────────────────────────

def _mcp_initialize(url: str, timeout: float = MCP_INIT_TIMEOUT) -> tuple[bool, Optional[str], Optional[str]]:
    """
    Send MCP initialize via Streamable HTTP POST.

    Server (FastMCP 3.x) responds with:
      - HTTP 200 + text/event-stream body containing JSON-RPC result
      - Mcp-Session-Id header for subsequent requests

    Returns (ok, session_id, error_msg).
    """
    payload = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "edr-wd-agent", "version": "1.0"},
        },
    }).encode("utf-8")

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }

    req = urllib.request.Request(url, data=payload, headers=headers, method="POST")

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            session = resp.headers.get("Mcp-Session-Id")
            raw = _read_all_sse_data(resp)

            if session:
                return (True, session, None)
            elif resp.status == 200:
                # Server responded without session header — still OK if we got JSON
                # Try to extract session from SSE body
                session_in_body = _extract_session_from_sse(raw)
                if session_in_body:
                    return (True, session_in_body, None)
                return (True, None, None)
            else:
                return (False, None, f"status={resp.status}")

    except urllib.error.HTTPError as e:
        body = e.read(200).decode("utf-8", errors="replace")
        session = e.headers.get("Mcp-Session-Id")
        if session:
            return (True, session, f"HTTP {e.code}: {body[:200]}")
        return (False, None, f"HTTP {e.code}: {body[:200]}")

    except (urllib.error.URLError, socket.timeout) as e:
        return (False, None, f"{type(e).__name__}: {e}")


def _extract_session_from_sse(raw: str) -> Optional[str]:
    """Extract JSON-RPC result from SSE-encoded body, return session if present."""
    # Format: "event: message\\r\\ndata: {...}\\r\\n\\r\\n"
    for line in raw.split("\r\n"):
        if line.startswith("data:"):
            try:
                data = json.loads(line[5:].strip())
                result = data.get("result", {})
                if isinstance(result, dict) and "protocolVersion" in result:
                    return data.get("id")  # session is in the session header, not body
            except (json.JSONDecodeError, ValueError):
                pass
    return None


# ── Public API ────────────────────────────────────────────────────────────────

def initialize(name: Optional[str] = None) -> dict:
    """
    Initialize MCP session with the target server.

    Takes an optional target name (uses default if None), resolves its config,
    builds the MCP URL, and performs the MCP initialize handshake.

    Returns {
        "ok": True,
        "target": str,
        "stage": "mcp_initialize",
        "data": {
            "session_id": str,
            "mcp_url": str,
            "protocol_version": str,
            "server_info": {...},
            "ready_level": "mcp_ready",
        }
    }

    Raises SSHAuthError / UnsupportedAuthType on config errors (not caught here).
    """
    tc = TargetConfig()
    target_name = name or _get_default_target_name(tc)
    _validate_name_or_raise(target_name, tc)

    # Build MCP URL directly — no SSH auth resolution needed
    mcp_url = tc.build_mcp_url(target_name)

    ok, session, error = _mcp_initialize(mcp_url)

    if ok:
        return {
            "ok": True,
            "target": target_name,
            "stage": "mcp_initialize",
            "data": {
                "session_id": session,
                "mcp_url": mcp_url,
                "protocol_version": MCP_PROTOCOL_VERSION,
                "ready_level": "mcp_ready",
            },
        }
    else:
        return {
            "ok": False,
            "target": target_name,
            "stage": "mcp_initialize",
            "error": f"MCP initialize failed: {error}",
            "mcp_url": mcp_url,
        }


def get_mcp_tools(session_id: str, mcp_url: Optional[str] = None) -> dict:
    """
    Call MCP tools/list to retrieve available tools.
    Requires session_id from initialize() and the same mcp_url.
    """
    if not mcp_url:
        return {"ok": False, "error": "mcp_url required"}
    return _mcp_jsonrpc(session_id, mcp_url, "tools/list", {})


def call_mcp_tool(session_id: str, mcp_url: str, tool_name: str, arguments: Optional[dict] = None) -> dict:
    """
    Call an MCP tool by name with optional arguments.
    """
    return _mcp_jsonrpc(session_id, mcp_url, "tools/call", {
        "name": tool_name,
        "arguments": arguments or {},
    })


_jsonrpc_id_counter = 1


def _next_rpc_id() -> int:
    global _jsonrpc_id_counter
    cur = _jsonrpc_id_counter
    _jsonrpc_id_counter += 1
    return cur


def _mcp_jsonrpc(session_id: str, mcp_url: str, method: str, params: dict) -> dict:
    """Send a JSON-RPC request with an active MCP session."""
    rpc_id = _next_rpc_id()
    payload = json.dumps({
        "jsonrpc": "2.0",
        "id": rpc_id,
        "method": method,
        "params": params,
    }).encode("utf-8")

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "Mcp-Session-Id": session_id,
    }

    req = urllib.request.Request(mcp_url, data=payload, headers=headers, method="POST")

    try:
        with urllib.request.urlopen(req, timeout=MCP_INIT_TIMEOUT) as resp:
            raw = _read_all_sse_data(resp)
            return _parse_sse_response(raw, expected_id=rpc_id)
    except socket.timeout:
        return {"ok": False, "error": f"socket timeout after {MCP_INIT_TIMEOUT}s waiting for JSON-RPC response"}
    except (urllib.error.URLError, socket.timeout) as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def _parse_sse_response(raw: str, expected_id: Optional[int] = None) -> dict:
    """
    Parse SSE-encoded JSON-RPC response into a clean dict.

    SSE format (FastMCP 3.x):
      : comment / ping lines         → skip
      event: <event_type>           → skip (we only care about data:)
      data: <JSON>                  → parse as JSON-RPC
      (blank lines)                  → skip

    If raw contains no parseable data at all, returns error indicating raw content.
    If expected_id is provided, only returns a data: line whose JSON-RPC id matches.
    """
    data_lines = []
    for line in raw.split("\r\n"):
        stripped = line.strip()
        # Skip blank lines and SSE comment/ping lines (start with :)
        if not stripped or stripped.startswith(":"):
            continue
        # Skip SSE event metadata lines
        if stripped.startswith("event:"):
            continue
        # Collect data: lines (support multi-line JSON)
        if stripped.startswith("data:"):
            data_lines.append(stripped[5:].strip())

    if not data_lines:
        # No data: lines found — return the raw content for debugging
        sample = raw.split("\r\n")[0][:100] if raw else "(empty)"
        return {"ok": False, "error": f"no SSE data: found. raw: {sample}"}

    # Parse data lines in reverse order (last event wins in SSE)
    for data_str in reversed(data_lines):
        try:
            data = json.loads(data_str)
            # If we have an expected id, only return matching responses
            if expected_id is not None:
                rpc_id = data.get("id")
                if rpc_id != expected_id:
                    continue
            return {"ok": True, "data": data}
        except (json.JSONDecodeError, ValueError):
            continue

    return {"ok": False, "error": "SSE data: lines found but none were valid JSON"}


def _read_all_sse_data(resp) -> str:
    """Read the full SSE response body, handling chunked transfer if needed."""
    body = b""
    while True:
        chunk = resp.read(8192)
        if not chunk:
            break
        body += chunk
    return body.decode("utf-8", errors="replace")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_default_target_name(tc_or_targets: Optional[TargetConfig | dict] = None) -> str:
    """Get default target name. Pass a TargetConfig instance or a targets dict."""
    if tc_or_targets is None:
        tc = TargetConfig()
    elif isinstance(tc_or_targets, TargetConfig):
        tc = tc_or_targets
    else:
        default = tc_or_targets.get("default_target")
        if not default:
            raise ValueError("No default target set")
        return default
    default = tc.get_default_target()
    if not default:
        raise ValueError("No default target set in config/targets.local.json")
    return default


def _validate_name_or_raise(name: str, tc_or_targets: Optional[TargetConfig | dict] = None) -> None:
    """Validate target name exists. Pass a TargetConfig instance or a targets dict."""
    if tc_or_targets is None:
        tc = TargetConfig()
    elif isinstance(tc_or_targets, TargetConfig):
        tc = tc_or_targets
    else:
        if name not in tc_or_targets:
            raise ValueError(f"Unknown target: {name}. Available: {list(tc_or_targets.keys())}")
        return
    if not tc.has_target(name):
        raise ValueError(f"Unknown target: {name}. Available: {list(tc.list_targets().keys())}")
