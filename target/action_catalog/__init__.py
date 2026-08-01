"""
__init__.py — Public surface of the action_catalog package.

Re-exports the stable API used by target/server.py and by tests:

    from action_catalog import (
        CATALOG_VERSION,
        ACTIONS_V1,
        catalog_digest,
        status_action_space,
        get_action_catalog,
        get_action_capability,
        backend_capability_view,
        # P1.1 territory (importable, but P0.1 does not call them):
        runtime_capability_for,
        runtime_capability_view,
    )

Stability: the names in `__all__` here are the public API for P0.1.
Anything not in `__all__` is implementation detail and may change
without notice in future checkpoints.

Layering reminder (per PR review):

* P0.1 owns "which actions exist and which backends statically
  support them" and renders `status.action_space` + `get_action_catalog`
  from that. P0.1 never probes a live backend instance.

* `runtime_capability.py` is the bridge to P1.1: it answers
  "can this backend object execute this method right now?".
  P0.1 does NOT import or call it; P1.1 dispatcher will.

* The catalog remains the single source of truth. `restore_edr` is
  in the catalog with `execution_provider="server_inline"` and the
  dispatch lives in server.py. No `extra_tools` override, no second
  capability table, no `hasattr` probe in the views layer.
"""

from __future__ import annotations

from .actions_v1 import ACTIONS_V1
from .digest import canonical_catalog_bytes, catalog_digest
from .enums import (
    BACKEND_NOT_IMPLEMENTED,
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
from .registry import (
    CATALOG_VERSION,
    build_registry,
    get_spec,
    get_spec_by_code,
    get_spec_by_tool,
    is_statically_supported,
    live_enablement_for,
    specs_for_backend,
)
# Public runtime probe (P1.1 will consume). P0.1 imports the symbols
# for documentation but never CALLS them — see runtime_capability.py.
from .runtime_capability import runtime_capability_for, runtime_capability_view
from .views import (
    backend_capability_view,
    get_action_capability,
    get_action_catalog,
    status_action_space,
)


# Eager construction: validate the V1 entries at module import so that
# any invariant violation is surfaced immediately, not on first use.
build_registry(ACTIONS_V1)


__all__ = [
    # Version
    "CATALOG_VERSION",
    # Catalog entries (frozen)
    "ACTIONS_V1",
    # Dataclasses
    "ActionSpec",
    "ActionEntry",
    "CatalogConstructionError",
    # Enums
    "VALID_CATEGORY",
    "VALID_SIDE_EFFECT",
    "VALID_RISK",
    "VALID_ROLLBACK_CLASS",
    "VALID_DEFAULT_SCREENSHOT",
    "VALID_TRANSITION_POLICY",
    "VALID_REQUIRES",
    "VALID_EXECUTION_PROVIDER",
    "VALID_BACKENDS",
    "BACKEND_NOT_IMPLEMENTED",
    # Registry helpers
    "build_registry",
    "get_spec",
    "get_spec_by_tool",
    "get_spec_by_code",
    "is_statically_supported",
    "specs_for_backend",
    "live_enablement_for",
    # Digest
    "canonical_catalog_bytes",
    "catalog_digest",
    # Views
    "status_action_space",
    "get_action_catalog",
    "get_action_capability",
    "backend_capability_view",
    # P1.1 territory (documented, not called by P0.1)
    "runtime_capability_for",
    "runtime_capability_view",
]