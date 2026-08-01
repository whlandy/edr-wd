"""
receipts.py — ActionReceipt envelope (P1.1).

Every dispatcher call returns an `ActionReceipt`. Legacy backend
methods return `{"ok": bool, "error": str | None, ...}` dicts;
newer patterns may return richer payloads. `normalize()` collapses
both into a stable envelope shape.

ActionReceipt fields (architecture §7.4):
  * code           — stable string from the dispatch outcome table
                    (`ok`, `unknown_action_id`, `backend_disabled`,
                    `precondition_failed`, `action_code_mismatch`,
                    `ownership_mismatch`, `dispatch_target_missing`,
                    `missing_selector_hint`, `error`, ...)
  * ok             — bool, mirroring the backend's intent
  * action_id      — the action that was dispatched (None if
                    unknown)
  * action_code    — the supplied action_code (None if not
                    supplied)
  * request_id     — the supplied idempotency token (None if not
                    supplied)
  * server_instance_id — the instance that produced this receipt
  * result         — the original backend payload (dict or None)
  * disabled_reason — for `backend_disabled` only
  * missing        — list of requirement strings not satisfied
                    (`precondition_failed`); otherwise None
  * message        — human-readable summary
  * extras         — catch-all for forward-compatibility

Stability (architecture §7.2):
  * Adding a new field to the envelope is a MINOR bump.
  * Removing or renaming a field is a MAJOR bump.
  * The set of stable codes is listed in
    `STABLE_DISPATCH_CODES` below.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from .runtime import get_server_instance_id


# Stable dispatch outcome codes (architecture §7.4 + FR-P1.1-03, -04,
# -05, -06, -07, -10).
CODE_OK                          = "ok"
CODE_UNKNOWN_ACTION_ID           = "unknown_action_id"
CODE_BACKEND_DISABLED            = "backend_disabled"
CODE_PRECONDITION_FAILED         = "precondition_failed"
CODE_ACTION_CODE_MISMATCH        = "action_code_mismatch"
CODE_OWNERSHIP_MISMATCH          = "ownership_mismatch"
CODE_DISPATCH_TARGET_MISSING     = "dispatch_target_missing"
CODE_MISSING_SELECTOR_HINT       = "missing_selector_hint"
CODE_BACKEND_ERROR               = "backend_error"
CODE_INVALID_REQUEST_ID          = "invalid_request_id"

STABLE_DISPATCH_CODES: frozenset[str] = frozenset({
    CODE_OK,
    CODE_UNKNOWN_ACTION_ID,
    CODE_BACKEND_DISABLED,
    CODE_PRECONDITION_FAILED,
    CODE_ACTION_CODE_MISMATCH,
    CODE_OWNERSHIP_MISMATCH,
    CODE_DISPATCH_TARGET_MISSING,
    CODE_MISSING_SELECTOR_HINT,
    CODE_BACKEND_ERROR,
    CODE_INVALID_REQUEST_ID,
})


@dataclass(frozen=True)
class ActionReceipt:
    """Envelope returned by every dispatcher call.

    Construction helpers (`from_ok`, `from_error`, etc.) keep the
    call sites concise and prevent accidental field-name drift.
    """

    code: str
    ok: bool
    action_id: str | None
    action_code: str | None = None
    request_id: str | None = None
    server_instance_id: str = field(default_factory=get_server_instance_id)
    result: Mapping[str, Any] | None = None
    disabled_reason: str | None = None
    missing: tuple[str, ...] | None = None
    message: str = ""
    extras: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.code not in STABLE_DISPATCH_CODES:
            # Defensive — call sites use the helpers below; an
            # unknown code here is a programmer error.
            raise ValueError(
                f"unknown ActionReceipt code {self.code!r}; "
                f"expected one of {sorted(STABLE_DISPATCH_CODES)}"
            )

    # ---- serialization -------------------------------------------------

    def to_dict(self) -> dict:
        """Return a JSON-friendly dict. Reserved fields are always
        present (even when None) so consumers can rely on the
        shape."""
        return {
            "ok": self.ok,
            "code": self.code,
            "action_id": self.action_id,
            "action_code": self.action_code,
            "request_id": self.request_id,
            "server_instance_id": self.server_instance_id,
            "result": self.result,
            "disabled_reason": self.disabled_reason,
            "missing": list(self.missing) if self.missing else None,
            "message": self.message,
            "extras": dict(self.extras) if self.extras else {},
        }

    def to_json(self) -> str:
        import json
        return json.dumps(self.to_dict(), ensure_ascii=False)

    # ---- construction helpers ------------------------------------------

    @classmethod
    def from_ok(
        cls,
        *,
        action_id: str,
        action_code: str | None,
        request_id: str | None,
        result: Mapping[str, Any] | None,
        message: str = "ok",
    ) -> "ActionReceipt":
        return cls(
            code=CODE_OK,
            ok=True,
            action_id=action_id,
            action_code=action_code,
            request_id=request_id,
            result=result,
            message=message,
        )

    @classmethod
    def from_error(
        cls,
        *,
        code: str,
        action_id: str | None,
        action_code: str | None = None,
        request_id: str | None = None,
        message: str,
        disabled_reason: str | None = None,
        missing: tuple[str, ...] | None = None,
        result: Mapping[str, Any] | None = None,
        extras: Mapping[str, Any] | None = None,
    ) -> "ActionReceipt":
        return cls(
            code=code,
            ok=False,
            action_id=action_id,
            action_code=action_code,
            request_id=request_id,
            result=result,
            disabled_reason=disabled_reason,
            missing=missing,
            message=message,
            extras=extras or {},
        )


def normalize(
    backend_payload: Any,
    *,
    action_id: str,
    action_code: str | None,
    request_id: str | None,
) -> ActionReceipt:
    """Wrap a raw backend return value into an ActionReceipt.

    `backend_payload` may be a dict (the legacy shape) or None
    (some backend methods return None on success). Anything else is
    passed through in `extras` so future backend shapes don't break
    normalisation.
    """
    if backend_payload is None:
        return ActionReceipt.from_ok(
            action_id=action_id,
            action_code=action_code,
            request_id=request_id,
            result=None,
            message="ok (no payload)",
        )
    if isinstance(backend_payload, Mapping):
        ok = bool(backend_payload.get("ok", True))
        if ok:
            return ActionReceipt.from_ok(
                action_id=action_id,
                action_code=action_code,
                request_id=request_id,
                result=backend_payload,
                message="ok",
            )
        # Legacy error shape: {"ok": False, "error": "..."}
        message = str(backend_payload.get("error", "backend error"))
        return ActionReceipt.from_error(
            code=CODE_BACKEND_ERROR,
            action_id=action_id,
            action_code=action_code,
            request_id=request_id,
            message=message,
            result=backend_payload,
        )
    # Non-dict payload — pass through in a fresh extras dict so
    # `from_ok` (which has no `extras` kwarg) is not called with
    # an unknown parameter.
    return ActionReceipt(
        code=CODE_OK,
        ok=True,
        action_id=action_id,
        action_code=action_code,
        request_id=request_id,
        result=None,
        message="ok (non-mapping payload)",
        extras={"value": backend_payload},
    )


__all__ = [
    "ActionReceipt",
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
    "normalize",
]