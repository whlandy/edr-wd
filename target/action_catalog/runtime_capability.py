"""
runtime_capability.py — Live backend probe (P1.1 territory, NOT P0.1).

This module exists so that the P0.1 catalog → view boundary is honest:

    * `views.py` answers "does the catalog say yes?" — static.
    * This module answers "can this backend object execute this method
      right now?" — runtime.

P0.1 does NOT call anything in this module. The P1.1 dispatcher will
be its primary consumer. Adding a runtime probe to P0.1 would couple
the catalog to a specific backend instance and obscure the static /
runtime distinction that the architecture review explicitly required.

Public API (P1.1 will exercise; P0.1 leaves it documented):

    runtime_capability_for(spec, backend_obj) -> (enabled, disabled_reason)
    runtime_capability_view(specs, backend_obj) -> dict[action_id, bool]

Behavior:

    * If `spec.execution_provider == "server_inline"`, return
      (True, "") — the inline handler in server.py is guaranteed
      by the runtime, no probe needed.
    * Otherwise, return `hasattr(backend_obj, spec.tool_name)` plus a
      diagnostic reason on False.

Stability:

    * P0.1: this file is documented but unused. The functions are
      importable so future tests / docs can reference them, but
      nothing inside the P0.1 codebase calls them.
    * P1.1: the dispatcher imports `runtime_capability_for` and uses
      it to decide whether a `request` may be dispatched.
"""

from __future__ import annotations

from .models import ActionSpec


def runtime_capability_for(
    spec: ActionSpec, backend_obj: object | None
) -> tuple[bool, str]:
    """Return (enabled, disabled_reason) for live dispatch.

    P1.1 dispatcher calls this. P0.1 does NOT — that is the whole
    point of separating this file from `views.py`.
    """
    if spec.execution_provider == "server_inline":
        return True, ""
    if backend_obj is None:
        return False, "no backend instance supplied"
    if hasattr(backend_obj, spec.tool_name):
        return True, ""
    return False, f"{spec.tool_name!r} is not implemented by this backend"


def runtime_capability_view(
    specs: tuple[ActionSpec, ...],
    backend_obj: object | None,
) -> dict[str, tuple[bool, str]]:
    """Return `action_id → (enabled, disabled_reason)` for live dispatch.

    P1.1 will use this when constructing per-case preflight checks.
    P0.1 does NOT call it.
    """
    out: dict[str, tuple[bool, str]] = {}
    for spec in specs:
        out[spec.action_id] = runtime_capability_for(spec, backend_obj)
    return out


__all__ = ["runtime_capability_for", "runtime_capability_view"]