"""
conditions.py — Pre-dispatch checks (P1.1, FR-P1.1-04, -05, -06, -07).

The dispatcher runs these checks in a fixed order before invoking
the backend:

    1. `check_live_enablement`     — backend supports this action?
    2. `check_requires`            — connected_window / window_lock / target_ref
    3. `check_action_code_match`   — caller-supplied code matches spec
    4. `check_ownership`           — target_ref.process_name matches
                                    the active window's process_name

Each check returns `None` on success or an `ActionReceipt`
describing the failure. Returning a receipt (rather than raising)
keeps the dispatch path exception-free: every failure surfaces as
a receipt the caller can inspect.

The "active window" for `check_ownership` comes from the backend's
`lock_state` (architecture §10): the dispatcher calls
`backend.get_window_lock()` and uses the lock snapshot's process
information. The P0.3 observation snapshot is **not** used here
because `target_ref.snapshot_id` may be unknown to the dispatcher
at dispatch time; P1.1 keeps ownership checks backend-mediated.
"""

from __future__ import annotations

from typing import Any, Mapping

from action_catalog import ActionSpec
from observations import ObservationRef, is_live

from .receipts import (
    CODE_ACTION_CODE_MISMATCH,
    CODE_BACKEND_DISABLED,
    CODE_MISSING_SELECTOR_HINT,
    CODE_OWNERSHIP_MISMATCH,
    CODE_PRECONDITION_FAILED,
    ActionReceipt,
)


def check_live_enablement(
    spec: ActionSpec,
    enabled: bool,
    disabled_reason: str | None,
    *,
    action_id: str,
    action_code: str | None,
    request_id: str | None,
) -> ActionReceipt | None:
    """FR-P1.1-04: disabled action for the current backend returns
    `backend_disabled`."""
    if enabled:
        return None
    return ActionReceipt.from_error(
        code=CODE_BACKEND_DISABLED,
        action_id=action_id,
        action_code=action_code,
        request_id=request_id,
        message=(
            f"action {action_id!r} is disabled for the current "
            f"backend ({disabled_reason!r})"
        ),
        disabled_reason=disabled_reason or "not supported by backend",
    )


def check_requires(
    spec: ActionSpec,
    *,
    action_id: str,
    action_code: str | None,
    request_id: str | None,
    has_connected_window: bool,
    has_window_lock: bool,
    target_ref: ObservationRef | Mapping[str, Any] | None,
) -> ActionReceipt | None:
    """FR-P1.1-05: enforce the spec's `requires` tuple.

    Requires category strings (architecture §10):

      * `connected_window` — caller must have a live connect()
        against a window. Caller passes `has_connected_window`.
      * `window_lock`      — caller must have a verified lock.
        Caller passes `has_window_lock`.
      * `target_ref`       — caller must supply a non-null target
        reference. For actions that ALSO need selector-driven
        resolution (gui.click, gui.type_text, gui.select,
        observe.control_text), the reference must carry at least
        one selector_hint key; otherwise we surface
        `missing_selector_hint`.

    This check does not validate the contents of target_ref beyond
    presence + selector_hint shape; deeper resolution is the
    P0.3 resolver's job.
    """
    missing: list[str] = []
    for req in spec.requires:
        if req == "connected_window" and not has_connected_window:
            missing.append("connected_window")
        elif req == "window_lock" and not has_window_lock:
            missing.append("window_lock")
        elif req == "target_ref":
            if target_ref is None:
                missing.append("target_ref")
            elif isinstance(target_ref, Mapping) and not target_ref:
                missing.append("target_ref")
            else:
                # Specs that need *selector_hint* in addition to a
                # target_ref. These are the semantic-input actions
                # that don't ship a built-in selector fallback.
                needs_selector = spec.category in {
                    "semantic_input", "observation",
                } and spec.action_id not in {
                    # observe.control_tree / observe.find_control /
                    # observe.windows do not require a selector.
                    "observe.control_tree",
                    "observe.find_control",
                    "observe.windows",
                    "observe.screenshot",
                    "observe.window_lock",
                }
                if needs_selector and isinstance(target_ref, Mapping):
                    if "selector_hint" not in target_ref:
                        return ActionReceipt.from_error(
                            code=CODE_MISSING_SELECTOR_HINT,
                            action_id=action_id,
                            action_code=action_code,
                            request_id=request_id,
                            message=(
                                f"action {action_id!r} requires a "
                                f"selector_hint in target_ref"
                            ),
                        )
    if missing:
        return ActionReceipt.from_error(
            code=CODE_PRECONDITION_FAILED,
            action_id=action_id,
            action_code=action_code,
            request_id=request_id,
            message=(
                f"action {action_id!r} is missing required "
                f"preconditions: {missing}"
            ),
            missing=tuple(missing),
        )
    return None


def check_action_code_match(
    spec: ActionSpec,
    supplied_code: str | None,
    *,
    action_id: str,
    request_id: str | None,
) -> ActionReceipt | None:
    """FR-P1.1-06: caller may supply an `action_code`; if supplied,
    it must match `spec.action_code`. If not supplied, the check
    passes silently (the dispatcher uses the catalog's code)."""
    if supplied_code is None:
        return None
    if supplied_code == spec.action_code:
        return None
    return ActionReceipt.from_error(
        code=CODE_ACTION_CODE_MISMATCH,
        action_id=action_id,
        action_code=supplied_code,
        request_id=request_id,
        message=(
            f"action_code {supplied_code!r} does not match the "
            f"code {spec.action_code!r} declared for action_id "
            f"{action_id!r}"
        ),
    )


def check_ownership(
    target_ref: ObservationRef | Mapping[str, Any],
    backend,
    *,
    action_id: str,
    action_code: str | None,
    request_id: str | None,
) -> ActionReceipt | None:
    """FR-P1.1-07: target ownership mismatch is rejected before any
    backend method call.

    Implementation: read the backend's lock snapshot via
    `backend.get_window_lock()`. If the lock's process_name does
    not match the target_ref's `expected_process_name`, surface
    `ownership_mismatch`. If no lock exists, fall through (the
    `connected_window` / `window_lock` requires-checks have already
    gated this).
    """
    if isinstance(target_ref, ObservationRef):
        expected = target_ref.expected_process_name
    elif isinstance(target_ref, Mapping):
        expected = str(target_ref.get("expected_process_name", "") or "")
    else:
        expected = ""
    if not expected:
        return None
    lock = _safe_get_window_lock(backend)
    if not lock or not lock.get("ok"):
        return None
    actual = str(lock.get("process_name", "") or "")
    if not actual:
        return None
    if actual == expected:
        return None
    return ActionReceipt.from_error(
        code=CODE_OWNERSHIP_MISMATCH,
        action_id=action_id,
        action_code=action_code,
        request_id=request_id,
        message=(
            f"target_ref.expected_process_name={expected!r} does "
            f"not match the active lock's process_name={actual!r}"
        ),
        extras={
            "expected_process_name": expected,
            "actual_process_name": actual,
        },
    )


def _safe_get_window_lock(backend) -> dict | None:
    method = getattr(backend, "get_window_lock", None)
    if not callable(method):
        return None
    try:
        result = method()
    except Exception:
        return None
    if not isinstance(result, dict):
        return None
    return result


__all__ = [
    "check_live_enablement",
    "check_requires",
    "check_action_code_match",
    "check_ownership",
]