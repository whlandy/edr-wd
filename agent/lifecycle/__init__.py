"""
lifecycle — Platform-specific server lifecycle backends.

A lifecycle backend owns the question: "is the MCP server running on the
target, and if not, how do we start it?"

Each platform has its own service manager:
  - windows: Task Scheduler (schtasks), PowerShell kill by port
  - macos:   launchd LaunchAgent (launchctl bootstrap / kickstart),
             lsof kill by port

Public surface (used by agent/target_manager.py):
    get_lifecycle(platform: str) -> LifecycleBackend
    LifecycleBackend.ensure_server_running(cfg) -> dict
    LifecycleBackend.stop_server(cfg) -> dict
    LifecycleBackend.install(cfg) -> dict  (optional; default: not supported)
    LifecycleBackend.platform -> str       ('windows' | 'macos')

Adding a new platform:
  1. Create agent/lifecycle/<platform>.py with a class implementing the
     LifecycleBackend interface.
  2. Register it in _REGISTRY below.
  3. Validation: target_config.SSUPPORTED_PLATFORMS must include the new value.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .base import LifecycleBackend


_REGISTRY: dict[str, str] = {
    # platform -> module path
    "windows": "agent.lifecycle.windows",
    "macos":   "agent.lifecycle.macos",
}


class UnsupportedPlatformError(ValueError):
    """Raised when a target's platform is not in the registry."""
    pass


def get_lifecycle(platform: str) -> "LifecycleBackend":
    """
    Factory: return a LifecycleBackend for `platform`.

    Raises UnsupportedPlatformError if the platform is not registered.
    """
    if platform not in _REGISTRY:
        raise UnsupportedPlatformError(
            f"platform='{platform}' has no lifecycle backend. "
            f"Registered: {sorted(_REGISTRY)}"
        )
    import importlib
    module = importlib.import_module(_REGISTRY[platform])
    return module.backend()  # each module exports a `backend()` factory


__all__ = ["get_lifecycle", "UnsupportedPlatformError"]
