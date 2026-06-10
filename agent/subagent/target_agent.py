"""Target-scoped lifecycle and MCP session owner."""

from __future__ import annotations

import json
import threading
from typing import Any, Optional

from agent import mcp_manager, target_manager
from agent.target_config import TargetConfig

from .state import TargetState


DEFAULT_PROFILE_BY_BACKEND: dict[str, str] = {
    "windows_pywinauto": "windows_hisec",
    "uia": "windows_hisec",
    "win32": "windows_hisec",
    "macos_accessibility": "macos_generic",
}

PROFILE_BACKENDS: dict[str, set[str]] = {
    "windows_hisec": {"windows_pywinauto", "uia", "win32"},
    "macos_generic": {"macos_accessibility"},
    "macos_hisec": {"macos_accessibility"},
}


class TargetSubAgent:
    """
    Own lifecycle and MCP session state for exactly one target.

    The implementation intentionally delegates to the existing manager modules.
    This gives us per-target state isolation without rewriting SSH, launchd,
    Task Scheduler, or the FastMCP transport code.
    """

    def __init__(self, target: str, config: Optional[TargetConfig] = None):
        self.config = config or TargetConfig()
        self.target = target
        raw = self.config.get_target(target)
        self.cfg = raw
        self.state = TargetState(
            target=target,
            platform=raw.get("platform", "windows"),
            app_profile=raw.get("app_profile"),
        )
        self._lock = threading.RLock()

    @classmethod
    def from_name(cls, target: Optional[str] = None,
                  config: Optional[TargetConfig] = None) -> "TargetSubAgent":
        tc = config or TargetConfig()
        name = target or tc.get_default_target()
        if not name:
            raise ValueError("No target name and no default_target configured")
        return cls(name, config=tc)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return self.state.as_dict()

    def ensure_running(self) -> dict:
        """Ensure the target MCP server is running."""
        with self._lock:
            result = target_manager.ensure_server_running(self.target)
            self.state.server_running = bool(result.get("ok"))
            if result.get("ok"):
                data = result.get("data", {})
                self.state.mcp_url = data.get("mcp_url") or self.config.build_mcp_url(self.target)
                self.state.ready_level = data.get("ready_level", self.state.ready_level)
                self.state.backend_kind = data.get("backend") or self.state.backend_kind
                self.state.health = data
                self.state.last_error = None
            else:
                self.state.last_error = result.get("error", "ensure_server_running failed")
            return result

    def initialize_mcp(self, force: bool = False) -> dict:
        """
        Initialize this target's MCP session.

        The latest session id is stored only on this TargetSubAgent instance.
        """
        with self._lock:
            if (
                not force
                and self.state.session_id
                and self.state.mcp_url
            ):
                return {
                    "ok": True,
                    "target": self.target,
                    "stage": "mcp_initialize",
                    "data": {
                        "session_id": self.state.session_id,
                        "mcp_url": self.state.mcp_url,
                        "ready_level": self.state.ready_level,
                    },
                }

            result = mcp_manager.initialize(self.target)
            if result.get("ok"):
                data = result.get("data", {})
                self.state.session_id = data.get("session_id")
                self.state.mcp_url = data.get("mcp_url")
                self.state.ready_level = data.get("ready_level", "mcp_ready")
                self.state.last_error = None
                self.refresh_status()
            else:
                self.state.last_error = result.get("error", "MCP initialize failed")
            return result

    def ensure_ready(self) -> dict:
        """Ensure server is running and MCP is initialized."""
        with self._lock:
            running = self.ensure_running()
            if not running.get("ok"):
                return running
            return self.initialize_mcp()

    def refresh_status(self) -> dict:
        """Call the status tool and update backend_kind."""
        status = self.call_tool("status", {}, retry_on_session_error=False)
        if isinstance(status, dict):
            backend = status.get("backend_kind") or status.get("backend")
            if backend:
                self.state.backend_kind = backend
        return status

    def tools_list(self) -> dict:
        with self._lock:
            init = self.initialize_mcp()
            if not init.get("ok"):
                return init
            return mcp_manager.get_mcp_tools(
                self.state.session_id or "",
                self.state.mcp_url,
            )

    def call_tool(
        self,
        name: str,
        arguments: Optional[dict] = None,
        timeout: Optional[float] = None,
        retry_on_session_error: bool = True,
    ) -> dict:
        """
        Call one MCP tool using this target's owned session.

        Returns the parsed tool JSON when possible. If the session appears
        stale, reinitialize once and retry.
        """
        with self._lock:
            init = self.initialize_mcp()
            if not init.get("ok"):
                return init

            raw = mcp_manager.call_mcp_tool(
                self.state.session_id or "",
                self.state.mcp_url or "",
                name,
                arguments or {},
                timeout=timeout,
            )
            parsed = self._unwrap_tool_result(raw)
            if retry_on_session_error and self._looks_like_session_error(parsed):
                self.initialize_mcp(force=True)
                raw = mcp_manager.call_mcp_tool(
                    self.state.session_id or "",
                    self.state.mcp_url or "",
                    name,
                    arguments or {},
                    timeout=timeout,
                )
                parsed = self._unwrap_tool_result(raw)
            return parsed

    def profile_backend_conflict(self, profile: str) -> bool:
        backend = self.state.backend_kind
        if not backend:
            return False
        expected = PROFILE_BACKENDS.get(profile)
        return expected is not None and backend not in expected

    def default_profile_for_backend(self) -> Optional[str]:
        backend = self.state.backend_kind
        return DEFAULT_PROFILE_BY_BACKEND.get(backend or "")

    def resolve_profile_for_live_backend(
        self,
        profile: str,
        *,
        explicit_profile: bool,
    ) -> tuple[bool, str, Optional[str]]:
        """
        Return (ok, profile_to_run, error).

        Non-explicit platform defaults may be rerouted to the live backend's
        default profile. Explicit profiles fail on mismatch.
        """
        if not self.profile_backend_conflict(profile):
            return True, profile, None

        live_default = self.default_profile_for_backend()
        if explicit_profile or live_default is None:
            return (
                False,
                profile,
                (
                    f"profile={profile!r} does not match live "
                    f"backend={self.state.backend_kind!r}"
                ),
            )
        return True, live_default, None

    @staticmethod
    def _looks_like_session_error(result: dict) -> bool:
        text = json.dumps(result, ensure_ascii=False).lower()
        return (
            "session" in text
            and (
                "invalid" in text
                or "expired" in text
                or "not found" in text
                or "missing" in text
            )
        )

    @staticmethod
    def _unwrap_tool_result(result: dict) -> dict:
        if not isinstance(result, dict):
            return {"ok": False, "raw": result}
        if not result.get("ok") or "data" not in result:
            return result

        data = result.get("data", {})
        result_obj = data.get("result", data) if isinstance(data, dict) else data
        if isinstance(result_obj, dict) and "content" in result_obj:
            for block in result_obj.get("content", []):
                if isinstance(block, dict) and block.get("type") == "text":
                    text = block.get("text", "")
                    try:
                        return json.loads(text)
                    except (TypeError, json.JSONDecodeError):
                        return {"ok": False, "raw": text}
        return result_obj if isinstance(result_obj, dict) else {"ok": True, "data": result_obj}
