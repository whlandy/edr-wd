"""State models for target-scoped subagents."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class TargetState:
    """Mutable runtime state owned by one TargetSubAgent."""

    target: str
    platform: str
    app_profile: Optional[str]
    mcp_url: Optional[str] = None
    session_id: Optional[str] = None
    backend_kind: Optional[str] = None
    ready_level: str = "unknown"
    server_running: bool = False
    last_error: Optional[str] = None
    health: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "target": self.target,
            "platform": self.platform,
            "app_profile": self.app_profile,
            "mcp_url": self.mcp_url,
            "session_id": self.session_id,
            "backend_kind": self.backend_kind,
            "ready_level": self.ready_level,
            "server_running": self.server_running,
            "last_error": self.last_error,
            "health": dict(self.health),
        }
