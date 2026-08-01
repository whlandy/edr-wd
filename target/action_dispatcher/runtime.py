"""
runtime.py — Server instance identity and backend holder (P1.1).

The dispatcher does not own the backend instance; `server.py` does.
P1.1 exposes a tiny holder so that `dispatch()` can resolve the
backend's method by `tool_name` without importing the entire
server module.

`server_instance_id` is generated at module import time. The
identifier is regenerated on every fresh Python interpreter start
(typical "server restart"); the agent uses a change in this value
to classify previously-in-flight requests as having an unknown
outcome (FR-P1.1-09).

Stability (architecture §7.2):
  * `server_instance_id` format is `inst-<8 hex>`. Bumping the
    format is a major change.
"""

from __future__ import annotations

import secrets
import threading


_lock = threading.Lock()
_instance_id: str | None = None
_current_backend_factory = None  # callable returning the backend instance


def _new_instance_id() -> str:
    return "inst-" + secrets.token_hex(4)


def get_server_instance_id() -> str:
    """Return the current server instance id, generating one on
    first call within this process."""
    global _instance_id
    if _instance_id is None:
        with _lock:
            if _instance_id is None:
                _instance_id = _new_instance_id()
    return _instance_id


def reset_server_instance_id_for_tests() -> None:
    """Test-only: clear the cached id so the next call generates a
    fresh one. Used to simulate a server restart in unit tests."""
    global _instance_id
    with _lock:
        _instance_id = None


def set_backend_resolver(resolver) -> None:
    """Register a callable that returns the live backend instance.

    `server.py` calls this at import time so `dispatch()` can find
    the backend without importing the entire server module. Tests
    may override this with a stub backend.
    """
    global _current_backend_factory
    _current_backend_factory = resolver


def get_backend():
    """Resolve the backend instance via the registered resolver.

    Raises `BackendNotConfiguredError` if no resolver has been
    registered yet (e.g. P1.1 tests that forgot to set one up).
    """
    if _current_backend_factory is None:
        raise BackendNotConfiguredError(
            "no backend resolver registered; "
            "call set_backend_resolver() first"
        )
    return _current_backend_factory()


class BackendNotConfiguredError(RuntimeError):
    """Raised when `dispatch()` is invoked before a backend
    resolver has been registered. Surfaces with code
    `dispatch_target_missing` per FR-P1.1-10."""


__all__ = [
    "BackendNotConfiguredError",
    "get_server_instance_id",
    "reset_server_instance_id_for_tests",
    "set_backend_resolver",
    "get_backend",
]