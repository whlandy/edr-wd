"""
views.py — Catalog → legacy/wire views (P0.1).

Two views consume the catalog:

* `status_action_space(backend)` — the legacy Boolean map keyed by
  `tool_name`. MUST be byte-identical to the pre-P0.1 hard-coded output
  for both backends (the P0.1 acceptance gate).

* `get_action_catalog(backend, include_disabled)` — the JSON-friendly
  catalog payload used by the `get_action_catalog` MCP tool and by
  the P3.1 planner.

Semantic boundary (PR review):

    P0.1 owns "is this action X supported on backend B according to
    the catalog?" — i.e. a **static**, catalog-resident answer.

    P0.1 explicitly does NOT answer "is the live backend object
    actually able to execute this action right now?" — that is
    runtime dispatch readiness and belongs to the P1.1 dispatcher.

The `enabled` flag in `get_action_catalog` therefore reflects ONLY:

    1. Is `action_id` in the catalog?
    2. Is `backend` in the spec's `backends` tuple?
    3. Is the action NOT in `BACKEND_NOT_IMPLEMENTED[backend]`?

It does NOT probe a live backend object. Even if the caller wants
to know "can this Python object execute this method right now?",
P0.1 returns the static answer and lets the caller infer that the
runtime check is the dispatcher's responsibility. The catalog stays
decoupled from any specific backend instance.

`execution_provider` carries the dispatch contract:

    * "backend"        → P1.1 dispatcher resolves tool_name → backend
                         method via hasattr at execution time.
    * "server_inline"  → server.py owns the inline handler; the
                         catalog entry is preserved for SSOT but
                         the dispatcher will route to server.py.

For convenience, `get_action_catalog` also surfaces
`status_provider` for `execution_provider == "server_inline"`
entries. This explicitly tells consumers "this entry reports
enabled because the dispatch lives in server.py, not because a
backend method exists". This is the review-feedback Issue 4 fix.

See `runtime_capability.py` for the (P1.1 territory) live probe
API. P0.1 does not import or call it.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Mapping

from .enums import BACKEND_NOT_IMPLEMENTED, VALID_BACKENDS
from .models import ActionSpec
from .registry import get_spec_by_tool, specs_for_backend


def _enabled_in_catalog(spec: ActionSpec, backend: str) -> tuple[bool, str]:
    """Pure-catalog enablement decision for `spec` on `backend`.

    Returns (enabled, disabled_reason). The decision is static and
    does NOT touch any backend instance. Runtime readiness (P1.1)
    is the dispatcher's responsibility.
    """
    gap = BACKEND_NOT_IMPLEMENTED.get(backend, {}).get(spec.tool_name)
    if gap:
        return False, gap
    if backend not in spec.backends:
        return False, f"not supported by {backend}"
    return True, ""


def status_action_space(backend: str) -> dict[str, bool]:
    """Generate the legacy `status.action_space` Boolean map.

    MUST be byte-identical to the pre-P0.1 hard-coded output:

      * Windows (`windows_pywinauto`): all 27 entries True.
      * macOS   (`macos_accessibility`):
          `type_text`, `select`, `get_text` → False;
          all other entries → True.

    Driven entirely by `spec.backends` + `BACKEND_NOT_IMPLEMENTED`.
    No runtime probe; no backend instance accepted as an argument
    (so callers cannot accidentally couple the view to one).
    """
    if backend not in VALID_BACKENDS:
        raise ValueError(
            f"unknown backend {backend!r}; expected one of {sorted(VALID_BACKENDS)}"
        )
    out: dict[str, bool] = {}
    for spec in specs_for_backend(_catalog_specs(), backend):
        enabled, _ = _enabled_in_catalog(spec, backend)
        out[spec.tool_name] = enabled
    return out


def get_action_catalog(
    backend: str | None = None,
    *,
    include_disabled: bool = False,
) -> dict:
    """Return the catalog as a JSON-friendly dict.

    `backend` (optional):
      When given, only actions statically supported on that backend
      appear (the catalog filters by `spec.backends`).

    `include_disabled`:
      When True, entries that are statically supported on the backend
      but flagged in `BACKEND_NOT_IMPLEMENTED` are also returned
      with `enabled: False` and a `disabled_reason`.

    The returned entries always include:

      * `execution_provider` — `"backend"` or `"server_inline"`.
      * `status_provider`    — `"catalog"` for `"backend"` entries;
        `"server_inline"` for `"server_inline"` entries. Tells the
        consumer where the `enabled` truth comes from. (Review
        feedback Issue 4: explicit semantic for `enabled=True`.)

    `enabled` semantic (Issue 4):

      * `True`  → catalog says yes; for `execution_provider == "backend"`
                  this is a *capability* claim, not a runtime readiness
                  claim. The P1.1 dispatcher verifies runtime readiness.
      * `False` → catalog says no (`BACKEND_NOT_IMPLEMENTED` gap or
                  `backend not in spec.backends`).
      * `None`  → `backend` was not provided; caller did not supply
                  enough context for the catalog to decide.

    Note: this view does NOT accept `backend_obj`. Runtime probes
    are P1.1 territory; the catalog stays decoupled from any live
    backend instance.
    """
    actions: list[dict] = []
    if backend is None:
        for spec in _catalog_specs():
            actions.append(_serialize_entry(spec, enabled=None,
                                           disabled_reason=""))
    elif backend not in VALID_BACKENDS:
        raise ValueError(f"unknown backend {backend!r}")
    else:
        for spec in specs_for_backend(_catalog_specs(), backend):
            enabled, reason = _enabled_in_catalog(spec, backend)
            if not enabled and not include_disabled:
                continue
            actions.append(_serialize_entry(spec, enabled=enabled,
                                           disabled_reason=reason))
    return {
        "catalog_version": _catalog_version(),
        "catalog_digest": _catalog_digest(),
        "backend": backend,
        "actions": actions,
    }


def get_action_capability(action_id: str, backend: str) -> bool:
    """Pure-catalog convenience: is `action_id` enabled on `backend`?

    Equivalent to `get_action_catalog(backend, include_disabled=True)`
    → look up `action_id` → return `enabled`. Without instantiating
    the full catalog payload.

    P0.1: static only. Runtime readiness is P1.1.
    """
    payload = get_action_catalog(backend, include_disabled=True)
    for entry in payload["actions"]:
        if entry["action_id"] == action_id:
            return entry["enabled"] is True
    return False


# ---------------------------------------------------------------------------
# Read-only view of the static capability map (Issue 1 fix)
# ---------------------------------------------------------------------------


def backend_capability_view() -> Mapping[str, Mapping[str, bool]]:
    """Return an immutable view of `BACKEND_CAPABILITIES`.

    The returned object does NOT expose a mutable dict. Tests and
    future consumers can read `view[action_id][backend]` but cannot
    accidentally mutate the catalog's static state. Returns a fresh
    `MappingProxyType` wrapper around the live dict — mutations to
    the underlying dict (only via `build_registry`) are visible
    through the proxy.

    Note: this is a P0.1 convenience for diagnostics; the canonical
    answer for "is X supported on Y" is `is_statically_supported` in
    `registry.py` or `get_action_capability` above.
    """
    from . import registry as _registry
    return MappingProxyType(_registry.BACKEND_CAPABILITIES)


# ---------------------------------------------------------------------------
# Entry serialization
# ---------------------------------------------------------------------------


def _serialize_entry(
    spec: ActionSpec, *, enabled: bool | None, disabled_reason: str
) -> dict:
    # `status_provider` makes the source of `enabled` explicit
    # (Review Issue 4). For execution_provider == "server_inline"
    # entries, "status_provider" == "server_inline" tells consumers
    # that `enabled=True` comes from the inline handler in server.py,
    # not from a backend method probe.
    return {
        "action_id": spec.action_id,
        "action_code": spec.action_code,
        "tool_name": spec.tool_name,
        "description": spec.description,
        "category": spec.category,
        "input_schema": dict(spec.input_schema),
        "result_schema": dict(spec.result_schema),
        "backends": list(spec.backends),
        "requires": list(spec.requires),
        "side_effect": spec.side_effect,
        "risk": spec.risk,
        "rollback_class": spec.rollback_class,
        "default_screenshot": spec.default_screenshot,
        "transition_policy": spec.transition_policy,
        "preferred_over": list(spec.preferred_over),
        "execution_provider": spec.execution_provider,
        "status_provider": spec.execution_provider,
        "enabled": enabled,
        "disabled_reason": disabled_reason,
    }


# ---------------------------------------------------------------------------
# Lazy package-internal access to the catalog singletons
# ---------------------------------------------------------------------------


def _catalog_specs() -> tuple[ActionSpec, ...]:
    from .actions_v1 import ACTIONS_V1
    return ACTIONS_V1


def _catalog_version() -> str:
    from .registry import CATALOG_VERSION
    return CATALOG_VERSION


def _catalog_digest() -> str:
    from .digest import catalog_digest
    return catalog_digest(_catalog_specs())


__all__ = [
    "status_action_space",
    "get_action_catalog",
    "get_action_capability",
    "backend_capability_view",
]