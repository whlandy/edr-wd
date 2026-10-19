"""Target-owned, single-writer GUI product run lease.

The allowlist is constructed locally, never from an action's profile parameter.
Deployments may narrow it via ``EDR_WD_ALLOWED_PROFILES`` (comma-separated IDs).
This token is a concurrency fence, not transport authentication: MCP/SSH must
remain access-controlled by the deployment.
"""

from __future__ import annotations

import os
import secrets
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass

try:
    from .product_profiles import ProductCapability, BUILTIN_CAPABILITIES
except ImportError:  # target-local deployment
    from product_profiles import ProductCapability, BUILTIN_CAPABILITIES


@dataclass(frozen=True)
class ProductRunContext:
    run_id: str
    run_token: str
    capability: ProductCapability
    expires_at: float


_mutex = threading.RLock()
_active: ProductRunContext | None = None
_cleanup_hooks: list = []
_dispatch_local = threading.local()

GUI_MUTATION_METHODS = frozenset({
    "connect", "lock_window", "unlock_window", "verify_window_lock",
    "activate_app", "activate_registered_app", "activate_edr", "restore_edr",
    "click", "click_target",
    "click_at", "click_window_at", "double_click_at", "right_click_at",
    "middle_click_at", "hover_at", "drag", "scroll", "scroll_window",
    "type_text", "select",
})


def load_allowed_profiles(manifest: dict | None = None) -> dict[str, ProductCapability]:
    """Validate an optional local manifest against compiled-in capabilities."""
    configured = os.environ.get("EDR_WD_ALLOWED_PROFILES")
    ids = set(configured.split(",")) if configured is not None else {
        key[0] for key in BUILTIN_CAPABILITIES
    }
    if manifest is not None:
        ids &= set(manifest)
    out = {}
    for (profile_id, _), capability in BUILTIN_CAPABILITIES.items():
        if profile_id not in ids:
            continue
        if manifest is not None and manifest[profile_id] != capability.digest:
            raise ValueError("product profile digest mismatch")
        out[profile_id] = capability
    if manifest is not None and set(manifest) - set(out):
        raise ValueError("product_profile_unknown")
    return out


def register_cleanup_hook(callback) -> None:
    _cleanup_hooks.append(callback)


def _expire_if_needed(now: float) -> None:
    global _active
    if _active is not None and _active.expires_at <= now:
        _active = None
        for callback in tuple(_cleanup_hooks):
            callback()


def begin_product_run(profile_id: str, digest: str, *, lease_seconds: float = 300,
                      backend_kind: str | None = None) -> ProductRunContext:
    global _active
    if not 1 <= lease_seconds <= 3600:
        raise ValueError("lease_seconds must be in [1, 3600]")
    with _mutex:
        now = time.monotonic()
        _expire_if_needed(now)
        if _active is not None:
            raise ValueError("product_run_conflict")
        capability = load_allowed_profiles().get(profile_id)
        if capability is None:
            raise ValueError("product_profile_unknown")
        if backend_kind is not None and capability.backend != backend_kind:
            raise ValueError("product_profile_unknown")
        if not secrets.compare_digest(capability.digest, digest):
            raise ValueError("product profile digest mismatch")
        _active = ProductRunContext(secrets.token_hex(16), secrets.token_urlsafe(32),
                                    capability, now + lease_seconds)
        return _active


def require_product_run(run_token: str | None) -> ProductRunContext | None:
    with _mutex:
        _expire_if_needed(time.monotonic())
        if _active is None:
            if run_token:
                raise ValueError("product_run_expired")
            return None  # explicit legacy compatibility when no run is active
        if not run_token or not secrets.compare_digest(_active.run_token, run_token):
            raise ValueError("product_run_conflict")
        return _active


def end_product_run(run_token: str) -> None:
    global _active
    with _mutex:
        require_product_run(run_token)
        if _active is None:
            raise ValueError("product_run_expired")
        _active = None
        for callback in tuple(_cleanup_hooks):
            callback()


def authorize_legacy_gui_call(tool_name: str) -> None:
    with _mutex:
        _expire_if_needed(time.monotonic())
        if _active is not None:
            raise ValueError(f"product_run_conflict: legacy GUI tool {tool_name} is disabled")


@contextmanager
def authorized_dispatch(run_token: str | None):
    """Allow backend mutations only while a validated dispatch owns the lease."""
    with _mutex:
        context = require_product_run(run_token)
        previous = getattr(_dispatch_local, "run_id", None)
        _dispatch_local.run_id = context.run_id if context else None
        try:
            yield
        finally:
            _dispatch_local.run_id = previous


class GuardedBackend:
    """Shared backend fence covering direct MCP and dispatcher call paths."""

    def __init__(self, backend, backend_kind: str | None = None):
        self._delegate = backend
        self.backend_kind = backend_kind or getattr(backend, "backend_kind", None)

    def __getattr__(self, name):
        value = getattr(self._delegate, name)
        if name not in GUI_MUTATION_METHODS or not callable(value):
            return value

        def guarded(*args, **kwargs):
            with _mutex:
                _expire_if_needed(time.monotonic())
                if _active is not None and getattr(_dispatch_local, "run_id", None) != _active.run_id:
                    return {"ok": False, "code": "product_run_conflict",
                            "error": "legacy GUI mutation is disabled during an active product run"}
                return value(*args, **kwargs)

        return guarded


def reset_for_tests() -> None:
    global _active
    with _mutex:
        _active = None
