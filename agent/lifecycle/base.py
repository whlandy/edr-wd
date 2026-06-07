"""
base.py — LifecycleBackend abstract interface.

A lifecycle backend knows how to start, stop, and check the MCP server on a
specific target platform. It is platform-aware but otherwise transport-agnostic
(SSH execution is delegated to agent.ssh_runner, which is already
cross-platform).

Contract:
    - All methods return a dict with shape:
        {"ok": bool, "stage": str, "data"?: dict, "error"?: str}
    - `stage` is one of: "ensure", "stop", "install", "health_check"
    - Implementations MUST NOT raise on transport errors — wrap them in
      {"ok": False, "error": str}. Programming errors (AttributeError, etc.)
      MAY be raised since they indicate a real bug.

Configuration contract:
    A `cfg` dict passed to methods is the FULLY RESOLVED target config
    (from TargetConfig.get_resolved_target()). It contains at minimum:
      - cfg["ssh"]:      SSH connection config (host/port/user/auth)
      - cfg["mcp"]:      MCP listener config (port, path, connect_mode, ...)
      - cfg["<platform>"]: platform-specific block
        (windows.target_root / macos.root, etc.)
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class LifecycleBackend(Protocol):
    """Protocol for platform-specific lifecycle backends."""

    @property
    def platform(self) -> str:
        """Return the platform name this backend handles."""
        ...

    def ensure_server_running(self, cfg: dict) -> dict:
        """
        Ensure the MCP server TCP port is listening on the target.

        Steps typically include:
          1. Check if already listening (TCP probe).
          2. If not, invoke the platform's service manager.
          3. Wait for the port to become available.

        MCP-level ready (initialize handshake) is delegated to mcp_manager.

        Returns {"ok": True, "stage": "ensure", "data": {...}} on success.
        """
        ...

    def stop_server(self, cfg: dict) -> dict:
        """
        Stop the MCP server process on the target.

        Returns {"ok": True, "stage": "stop", "data": {...}} on success.
        """
        ...

    def install(self, cfg: dict) -> dict:
        """
        Install / register the persistent service definition on the target
        (e.g. Windows Task Scheduler entry, macOS LaunchAgent plist).

        Optional: implementations may return {"ok": False, "error": "not supported"}.
        """
        ...

    def probe(self, cfg: dict) -> dict:
        """
        Probe the target for basic capabilities (SSH, Python, platform tools).

        Returns {"ok": True, "stage": "probe", "data": {"ssh": ..., "python": ..., ...}}.
        Returns {"ok": False, "stage": "probe", "error": ..., "code": ...} on failure.
        Never raises — always returns a structured result.
        """
        ...

    def deploy(self, cfg: dict) -> dict:
        """
        Upload the local target/ directory to the remote target_root.

        Returns {"ok": True, "stage": "deploy", "data": {...}}.
        Returns {"ok": False, "stage": "deploy", "error": ..., "code": ...} on failure.
        Never raises — always returns a structured result.
        """
        ...
