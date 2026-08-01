"""
__init__.py — Public surface of the action_dispatcher package (P1.1).

Layering (parallel to action_catalog, protocol_models, observations):

    target/action_dispatcher/
        runtime.py     — server_instance_id + backend resolver
        receipts.py    — ActionReceipt envelope + stable codes
        cache.py       — bounded LRU idempotency cache
        mapping.py     — action_id <-> tool_name mapping; coverage
        conditions.py  — pre-dispatch checks (enablement, requires,
                         action_code, ownership)
        dispatch.py    — top-level dispatch() entry point

Reviewer gate (architecture §24): P1.1 stops here. P1.2 atomic
executor consumes `dispatch()` and adds the per-step state machine.
"""

from __future__ import annotations

from .cache import (
    DEFAULT_MAX_ENTRIES,
    clear as cache_clear,
    inflight_count,
    inflight_lock,
    lookup,
    lru_capacity_remaining,
    lru_size,
    store,
)
from .conditions import (
    check_action_code_match,
    check_live_enablement,
    check_ownership,
    check_requires,
)
from .dispatch import dispatch
from .mapping import (
    MUTATING_SIDE_EFFECTS,
    ActionDispatchMap,
    check_backend_coverage,
    is_mutating,
)
from .receipts import (
    CODE_ACTION_CODE_MISMATCH,
    CODE_BACKEND_DISABLED,
    CODE_BACKEND_ERROR,
    CODE_DISPATCH_TARGET_MISSING,
    CODE_INVALID_REQUEST_ID,
    CODE_MISSING_SELECTOR_HINT,
    CODE_OK,
    CODE_OWNERSHIP_MISMATCH,
    CODE_PRECONDITION_FAILED,
    CODE_UNKNOWN_ACTION_ID,
    STABLE_DISPATCH_CODES,
    ActionReceipt,
    normalize,
)
from .runtime import (
    BackendNotConfiguredError,
    get_backend,
    get_server_instance_id,
    reset_server_instance_id_for_tests,
    set_backend_resolver,
)


__all__ = [
    # Public entry point
    "dispatch",
    # Stable codes
    "STABLE_DISPATCH_CODES",
    "CODE_OK",
    "CODE_UNKNOWN_ACTION_ID",
    "CODE_BACKEND_DISABLED",
    "CODE_PRECONDITION_FAILED",
    "CODE_ACTION_CODE_MISMATCH",
    "CODE_OWNERSHIP_MISMATCH",
    "CODE_DISPATCH_TARGET_MISSING",
    "CODE_MISSING_SELECTOR_HINT",
    "CODE_BACKEND_ERROR",
    "CODE_INVALID_REQUEST_ID",
    # Envelope
    "ActionReceipt",
    "normalize",
    # Idempotency cache
    "DEFAULT_MAX_ENTRIES",
    "lookup",
    "store",
    "cache_clear",
    "lru_size",
    "lru_capacity_remaining",
    "inflight_lock",
    "inflight_count",
    # Mapping
    "MUTATING_SIDE_EFFECTS",
    "ActionDispatchMap",
    "is_mutating",
    "check_backend_coverage",
    # Conditions
    "check_live_enablement",
    "check_requires",
    "check_action_code_match",
    "check_ownership",
    # Runtime
    "BackendNotConfiguredError",
    "get_backend",
    "get_server_instance_id",
    "reset_server_instance_id_for_tests",
    "set_backend_resolver",
]