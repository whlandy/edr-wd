"""
registry.py — Catalog construction, validation, and views (P0.1).

P0.1 owns these questions:

    * "Which actions exist?"  →  `ACTIONS_V1`, validated at import.
    * "Is action X statically supported on backend B?"
        →  `BACKEND_CAPABILITIES[action_id][backend] = bool`.
    * "Is action X currently enabled for the live target?"
        →  `live_enablement_for(action_id, backend_name)` looks up
            `BACKEND_CAPABILITIES` only. It does NOT inspect
            `hasattr(backend_obj, tool_name)` because that decision
            belongs to the P1.1 dispatcher, not to P0.1.

P0.1 explicitly does NOT answer:

    * "How do I call this action?"  →  P1.1 dispatcher.
    * "Is the live backend's method actually callable right now?"
        →  P1.1 dispatcher checks `hasattr(backend, tool_name)`
            (or, for `execution_provider == "server_inline"`, looks up
            the inline handler in server.py).

Stability: any field change in any `ActionSpec` perturbs the digest.
Add a new field to `ActionSpec` only with a corresponding patch bump
in `CATALOG_VERSION`.

Construction errors use a stable `code` and a `value` payload:

    CatalogConstructionError(code="duplicate_action_id",
                             value="gui.click")
"""

from __future__ import annotations

from .enums import (
    VALID_BACKENDS,
    VALID_CATEGORY,
    VALID_DEFAULT_SCREENSHOT,
    VALID_EXECUTION_PROVIDER,
    VALID_REQUIRES,
    VALID_ROLLBACK_CLASS,
    VALID_RISK,
    VALID_SIDE_EFFECT,
    VALID_TRANSITION_POLICY,
)
from .models import ActionEntry, ActionSpec, CatalogConstructionError


# ---------------------------------------------------------------------------
# Catalog version (architecture §7.2)
# ---------------------------------------------------------------------------

CATALOG_VERSION = "1.0.1"


# ---------------------------------------------------------------------------
# Backend capability map (P0.1 view)
#
# Architecture §7.3: static catalog membership and live capability are
# separate. The catalog declares the **set of backends an action supports**
# (via `spec.backends`); the **enabled flag for a specific backend** is
# decided by inspecting the backend instance, which is P1.1 territory.
#
# This map exists only to answer "is action X statically applicable to
# backend B?" without inspecting a live backend object. P0.1 reads from
# here; P1.1 keeps reading from here too (for static validation) and adds
# its own `hasattr`/dispatcher resolution on top.
# ---------------------------------------------------------------------------

# Format: { action_id: { backend_name: bool } }
# All keys are present (every action that is statically supported on a
# backend maps to True; the map intentionally does NOT include actions
# that are NOT statically supported on a backend — those are filtered
# out before this map is consulted).
BACKEND_CAPABILITIES: dict[str, dict[str, bool]] = {}


def _build_capability_map(specs: tuple[ActionSpec, ...]) -> dict[str, dict[str, bool]]:
    """Index each spec by action_id → {backend: True/False}.

    The map defaults to True for every `(action_id, backend)` pair
    declared in `spec.backends`, then is overridden by
    `BACKEND_NOT_IMPLEMENTED` (P0.1 review: the static capability
    map must honour the not-implemented table, otherwise the
    dispatcher would treat macOS type_text as supported even though
    it is listed as not implemented).

    Static only. Live enablement is decided by P1.1 dispatcher.
    """
    from .enums import BACKEND_NOT_IMPLEMENTED
    out: dict[str, dict[str, bool]] = {}
    for spec in specs:
        out[spec.action_id] = {}
        for b in spec.backends:
            # BACKEND_NOT_IMPLEMENTED keys are tool_name, not
            # action_id. Map them.
            not_impl_tools = BACKEND_NOT_IMPLEMENTED.get(b, {})
            if spec.tool_name in not_impl_tools:
                out[spec.action_id][b] = False
            else:
                out[spec.action_id][b] = True
    return out


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _validate_enums(spec: ActionSpec) -> None:
    if spec.category not in VALID_CATEGORY:
        raise CatalogConstructionError(
            "invalid_category",
            f"action_id={spec.action_id!r} has invalid category={spec.category!r}",
            value=spec.action_id,
        )
    if spec.side_effect not in VALID_SIDE_EFFECT:
        raise CatalogConstructionError(
            "invalid_side_effect",
            f"action_id={spec.action_id!r} has invalid side_effect={spec.side_effect!r}",
            value=spec.action_id,
        )
    if spec.risk not in VALID_RISK:
        raise CatalogConstructionError(
            "invalid_risk",
            f"action_id={spec.action_id!r} has invalid risk={spec.risk!r}",
            value=spec.action_id,
        )
    if spec.rollback_class not in VALID_ROLLBACK_CLASS:
        raise CatalogConstructionError(
            "invalid_rollback_class",
            f"action_id={spec.action_id!r} has invalid rollback_class={spec.rollback_class!r}",
            value=spec.action_id,
        )
    if spec.default_screenshot not in VALID_DEFAULT_SCREENSHOT:
        raise CatalogConstructionError(
            "invalid_default_screenshot",
            f"action_id={spec.action_id!r} has invalid default_screenshot={spec.default_screenshot!r}",
            value=spec.action_id,
        )
    if spec.transition_policy not in VALID_TRANSITION_POLICY:
        raise CatalogConstructionError(
            "invalid_transition_policy",
            f"action_id={spec.action_id!r} has invalid transition_policy={spec.transition_policy!r}",
            value=spec.action_id,
        )
    if spec.execution_provider not in VALID_EXECUTION_PROVIDER:
        raise CatalogConstructionError(
            "invalid_execution_provider",
            f"action_id={spec.action_id!r} has invalid execution_provider={spec.execution_provider!r}",
            value=spec.action_id,
        )
    for r in spec.requires:
        if r not in VALID_REQUIRES:
            raise CatalogConstructionError(
                "invalid_requires",
                f"action_id={spec.action_id!r} has invalid requires={r!r}",
                value=spec.action_id,
            )
    for b in spec.backends:
        if b not in VALID_BACKENDS:
            raise CatalogConstructionError(
                "invalid_backend",
                f"action_id={spec.action_id!r} has invalid backend={b!r}",
                value=spec.action_id,
            )


def _check_uniqueness(specs: tuple[ActionSpec, ...]) -> None:
    seen_ids: set[str] = set()
    seen_codes: set[str] = set()
    seen_tools: set[str] = set()
    for spec in specs:
        if spec.action_id in seen_ids:
            raise CatalogConstructionError(
                "duplicate_action_id",
                f"action_id={spec.action_id!r} is registered more than once",
                value=spec.action_id,
            )
        seen_ids.add(spec.action_id)
        if spec.action_code is not None:
            if spec.action_code in seen_codes:
                raise CatalogConstructionError(
                    "duplicate_action_code",
                    f"action_code={spec.action_code!r} is registered more than once",
                    value=spec.action_code,
                )
            seen_codes.add(spec.action_code)
        if spec.tool_name in seen_tools:
            raise CatalogConstructionError(
                "duplicate_tool_name",
                f"tool_name={spec.tool_name!r} is registered more than once",
                value=spec.tool_name,
            )
        seen_tools.add(spec.tool_name)


def build_registry(specs: tuple[ActionSpec, ...]) -> tuple[ActionSpec, ...]:
    """Validate specs (enums + uniqueness) and return them.

    Side effect: rebuilds `BACKEND_CAPABILITIES` so any change to the
    catalog spec list takes effect. Callers normally use `ACTIONS_V1`
    directly.
    """
    for spec in specs:
        _validate_enums(spec)
    _check_uniqueness(specs)
    global BACKEND_CAPABILITIES
    BACKEND_CAPABILITIES = _build_capability_map(specs)
    return specs


# ---------------------------------------------------------------------------
# Public lookup helpers (P0.1 read-only views)
# ---------------------------------------------------------------------------


def get_spec(specs: tuple[ActionSpec, ...], action_id: str) -> ActionSpec | None:
    """Return the spec for `action_id`, or None if absent."""
    for spec in specs:
        if spec.action_id == action_id:
            return spec
    return None


def get_spec_by_tool(
    specs: tuple[ActionSpec, ...], tool_name: str
) -> ActionSpec | None:
    """Return the spec whose `tool_name` matches, or None."""
    for spec in specs:
        if spec.tool_name == tool_name:
            return spec
    return None


def get_spec_by_code(
    specs: tuple[ActionSpec, ...], action_code: str
) -> ActionSpec | None:
    """Return the spec whose `action_code` matches, or None."""
    for spec in specs:
        if spec.action_code == action_code:
            return spec
    return None


def is_statically_supported(
    specs: tuple[ActionSpec, ...], action_id: str, backend: str
) -> bool:
    """True iff `action_id` declares `backend` in `spec.backends`.

    Pure catalog lookup; does not touch a live backend instance.
    """
    spec = get_spec(specs, action_id)
    if spec is None:
        return False
    return backend in spec.backends


def specs_for_backend(
    specs: tuple[ActionSpec, ...], backend: str
) -> tuple[ActionSpec, ...]:
    """Return every spec that statically supports `backend`."""
    return tuple(s for s in specs if backend in s.backends)


# ---------------------------------------------------------------------------
# Live enablement view (P0.1)
#
# P0.1 reports enablement purely from the catalog's static capability
# map. It does NOT inspect a live backend instance — that decision is
# P1.1 dispatcher territory (and would couple the catalog to the
# AutomationBackend Protocol).
#
# The `live_enablement_for` function returns:
#
#     (enabled, disabled_reason)
#
# where `enabled` is True iff the action is statically supported on
# the backend. If the caller asks about a backend the catalog does not
# know about, it returns (False, "backend_not_in_catalog"). For
# unknown actions, (False, "action_id_not_in_catalog").
# ---------------------------------------------------------------------------


def live_enablement_for(
    specs: tuple[ActionSpec, ...],
    action_id: str,
    backend: str,
) -> tuple[bool, str]:
    """Decide static enablement of `action_id` for `backend`.

    P0.1: this is a catalog-only lookup. P1.1 dispatcher will layer
    on a `hasattr(backend_obj, tool_name)` check (or, for
    `execution_provider == "server_inline"`, look up the inline
    handler in server.py) and may downgrade enabled → False based
    on live backend state.
    """
    spec = get_spec(specs, action_id)
    if spec is None:
        return False, "action_id_not_in_catalog"
    if backend not in VALID_BACKENDS:
        return False, "backend_not_in_catalog"
    if backend in spec.backends:
        return True, ""
    return False, f"not supported by {backend}"


__all__ = [
    "CATALOG_VERSION",
    "BACKEND_CAPABILITIES",
    "build_registry",
    "get_spec",
    "get_spec_by_tool",
    "get_spec_by_code",
    "is_statically_supported",
    "specs_for_backend",
    "live_enablement_for",
]