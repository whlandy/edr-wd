"""
dispatch.py — Top-level dispatcher entry point (P1.1).

`dispatch(action_id, action_code, args, target_ref, request_id)`
returns an `ActionReceipt`. It runs the pre-checks, invokes the
backend method, normalises the return, invalidates any active
observation snapshot if the action was mutating, caches the
receipt under `request_id` if supplied, and returns.

Order of operations (architecture §7.4, FR-P1.1-01..10):

    1. Validate `request_id` shape (must be a non-empty string if
       supplied; surface `invalid_request_id` if not).
    2. Acquire the per-request inflight lock (P1.1 review #1
       race-condition fix). All steps below run under this lock
       so concurrent calls sharing the same `request_id` serialise
       and the backend executes exactly once.
    3. Idempotency lookup — if the cache has a receipt for
       `request_id`, return it without further work.
    4. Resolve the catalog entry by `action_id`. Missing ->
       `unknown_action_id`.
    5. Check live enablement (`backend_disabled` if disabled).
       Server-inline actions skip this step.
    6. Check `requires` (`precondition_failed` /
       `missing_selector_hint`).
    7. Check `action_code` match (`action_code_mismatch`).
    8. Check ownership (`ownership_mismatch`).
    9. Resolve the backend method by `tool_name`. Missing ->
       `dispatch_target_missing`.
   10. Invoke the method, capturing exceptions -> `backend_error`.
   11. Normalise the return into an `ActionReceipt`.
   12. If the action was mutating, invalidate the active snapshot.
   13. Cache the receipt under `request_id` (if supplied).
   14. Release the inflight lock.

The dispatcher does NOT mutate global state beyond the cache and
the active-snapshot invalidation hook. Callers may invoke it
concurrently from different threads; the cache and observation
registry are already thread-safe, and the inflight lock
serialises same-request_id dispatch.
"""

from __future__ import annotations

from typing import Any, Mapping

from action_catalog import (
    ActionSpec,
    backend_capability_view,
)

from . import cache
from .conditions import (
    check_action_code_match,
    check_live_enablement,
    check_ownership,
    check_requires,
)
from .mapping import ActionDispatchMap, is_mutating
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
    ActionReceipt,
    normalize,
)
from .runtime import (
    BackendNotConfiguredError,
    get_backend,
)
from observation_bridge import invalidate_after_mutation


# Module-level default dispatch map; built lazily on first call.
_default_map: ActionDispatchMap | None = None


def _get_default_map() -> ActionDispatchMap:
    global _default_map
    if _default_map is None:
        _default_map = ActionDispatchMap.from_catalog()
    return _default_map


def dispatch(
    action_id: str,
    *,
    action_code: str | None = None,
    args: Mapping[str, Any] | None = None,
    target_ref: Mapping[str, Any] | None = None,
    request_id: str | None = None,
    dispatch_map: ActionDispatchMap | None = None,
    has_connected_window: bool = True,
    has_window_lock: bool = True,
) -> ActionReceipt:
    """Dispatch a single action via the configured backend.

    See module docstring for the order of operations. The
    `dispatch_map` and `has_connected_window` / `has_window_lock`
    parameters exist for tests; the production entry point uses
    the defaults.
    """
    args = dict(args or {})
    target_ref = dict(target_ref or {})

    # Step 1 — request_id shape validation (caller-supplied only).
    if request_id is not None and not (
        isinstance(request_id, str) and request_id
    ):
        return ActionReceipt.from_error(
            code=CODE_INVALID_REQUEST_ID,
            action_id=action_id,
            action_code=action_code,
            request_id=None,
            message=(
                f"request_id must be a non-empty string when "
                f"supplied; got {type(request_id).__name__}"
            ),
        )

    # Steps 2..13 run under a per-request inflight lock so that
    # concurrent dispatch() calls sharing the same `request_id`
    # serialise. FR-P1.1-02: the backend executes exactly once
    # for a given request_id; second callers block, then read the
    # cached receipt.
    with cache.inflight_lock(request_id or ""):
        # Step 3 — idempotency lookup.
        if request_id:
            cached = cache.lookup(request_id)
            if cached is not None:
                return cached

        # Step 4 — catalog lookup.
        dmap = dispatch_map or _get_default_map()
        spec = dmap.get_by_action_id(action_id)
        if spec is None:
            return _cache_and_return(request_id, ActionReceipt.from_error(
                code=CODE_UNKNOWN_ACTION_ID,
                action_id=action_id,
                action_code=action_code,
                request_id=request_id,
                message=f"unknown action_id {action_id!r}",
            ))

        # Step 5 — live enablement (P0.1 `backend_capability_view`).
        # Server-inline actions skip this step entirely; they are
        # dispatched by target/server.py, not the dispatcher.
        if spec.execution_provider != "server_inline":
            try:
                backend_for_lookup = get_backend()
            except BackendNotConfiguredError:
                backend_for_lookup = None
            backend_kind = (
                getattr(backend_for_lookup, "backend_kind", None)
                if backend_for_lookup is not None else None
            )
            cap = backend_capability_view()
            enabled = True
            disabled_reason: str | None = None
            if backend_kind is not None:
                if backend_kind not in spec.backends:
                    enabled = False
                    disabled_reason = (
                        f"backend {backend_kind!r} does not implement "
                        f"action {action_id!r}"
                    )
                else:
                    per_backend = cap.get(action_id, {}).get(backend_kind)
                    if per_backend is False:
                        enabled = False
                        disabled_reason = (
                            f"not supported by {backend_kind}"
                        )
            fail = check_live_enablement(
                spec, enabled,
                disabled_reason=disabled_reason,
                action_id=action_id,
                action_code=action_code,
                request_id=request_id,
            )
            if fail is not None:
                return _cache_and_return(request_id, fail)

        # Step 6 — requires / preconditions.
        fail = check_requires(
            spec,
            action_id=action_id,
            action_code=action_code,
            request_id=request_id,
            has_connected_window=has_connected_window,
            has_window_lock=has_window_lock,
            target_ref=target_ref,
        )
        if fail is not None:
            return _cache_and_return(request_id, fail)

        # Step 7 — action_code match (if supplied).
        fail = check_action_code_match(
            spec, action_code,
            action_id=action_id,
            request_id=request_id,
        )
        if fail is not None:
            return _cache_and_return(request_id, fail)

        # Step 8 — ownership. The dispatcher uses the backend's lock
        # state to confirm `expected_process_name` matches.
        if target_ref:
            try:
                backend = get_backend()
            except BackendNotConfiguredError as exc:
                return _cache_and_return(request_id, ActionReceipt.from_error(
                    code=CODE_DISPATCH_TARGET_MISSING,
                    action_id=action_id,
                    action_code=action_code,
                    request_id=request_id,
                    message=str(exc),
                ))
            fail = check_ownership(
                target_ref, backend,
                action_id=action_id,
                action_code=action_code,
                request_id=request_id,
            )
            if fail is not None:
                return _cache_and_return(request_id, fail)

        # Step 9 — backend method resolution.
        try:
            backend = get_backend()
        except BackendNotConfiguredError as exc:
            return _cache_and_return(request_id, ActionReceipt.from_error(
                code=CODE_DISPATCH_TARGET_MISSING,
                action_id=action_id,
                action_code=action_code,
                request_id=request_id,
                message=str(exc),
            ))
        method = dmap.resolve_backend_method(backend, action_id)
        if method is None:
            return _cache_and_return(request_id, ActionReceipt.from_error(
                code=CODE_DISPATCH_TARGET_MISSING,
                action_id=action_id,
                action_code=action_code,
                request_id=request_id,
                message=(
                    f"backend has no method named {spec.tool_name!r} "
                    f"for action {action_id!r}"
                ),
            ))

        # Step 10 — invoke the backend method.
        try:
            backend_payload = method(**args) if args else method()
        except Exception as exc:  # noqa: BLE001 (intentional: surface as receipt)
            return _cache_and_return(request_id, ActionReceipt.from_error(
                code=CODE_BACKEND_ERROR,
                action_id=action_id,
                action_code=action_code,
                request_id=request_id,
                message=f"backend method raised: {exc}",
                extras={"exception_type": type(exc).__name__},
            ))

        # Step 11 — normalise the return.
        receipt = normalize(
            backend_payload,
            action_id=action_id,
            action_code=action_code,
            request_id=request_id,
        )

        # Step 12 — invalidate active snapshot if the action was
        # mutating AND the backend call succeeded. A failed
        # mutation must not invalidate (the world has not changed).
        if receipt.code == CODE_OK and is_mutating(spec):
            invalidated_sid = invalidate_after_mutation(
                backend_kind=type(backend).__name__,
            )
            receipt = ActionReceipt(
                code=receipt.code,
                ok=receipt.ok,
                action_id=receipt.action_id,
                action_code=receipt.action_code,
                request_id=receipt.request_id,
                server_instance_id=receipt.server_instance_id,
                result=receipt.result,
                disabled_reason=receipt.disabled_reason,
                missing=receipt.missing,
                message=receipt.message,
                extras={**receipt.extras,
                        "invalidated_snapshot_id": invalidated_sid},
            )

        # Step 13 — cache the receipt.
        return _cache_and_return(request_id, receipt)


def _cache_and_return(request_id: str | None,
                      receipt: ActionReceipt) -> ActionReceipt:
    if request_id:
        cache.store(request_id, receipt)
    return receipt


__all__ = ["dispatch"]