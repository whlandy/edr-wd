"""
ref.py — P0.3-specific TargetRef (observer perspective).

Why a separate type?

  * `protocol_models.TargetRef` was designed in P0.2 to carry the
    *planned* target identity a dispatcher will use. Its
    `target_id` is required because a plan author must know which
    target they intend.
  * P0.3 is the *observation* layer. A planner often does NOT yet
    know the per-snapshot ordinal — they have a snapshot_id and a
    selector hint, and expect the resolver to find the matching
    target. Requiring `target_id` would force observers to invent
    fake ordinals.
  * This module deliberately stays separate from
    `protocol_models.TargetRef` so that P0.3 review (which has its
    own review-gate rules) can evolve `ObservationRef` without
    touching the merged P0.2 contract. When P1.1 dispatcher lands,
    it will bridge `ObservationRef` -> resolved `Target` ->
    `protocol_models.TargetRef`.

This module does NOT redefine the strict-mode machinery: it imports
`_strict_from_dict_kwargs`, `_require_str`, `_allow_none`,
`ProtocolModelError` from `protocol_models.models`. That keeps the
error codes consistent across the protocol stack.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from protocol_models.models import ProtocolModelError

from ._strict import (
    _allow_none_or_str,
    _require_str,
    _strict_from_dict_kwargs,
)


OBSERVATION_REF_SCHEMA_VERSION = "1.0.0"


@dataclass(frozen=True)
class ObservationRef:
    """Observer-side reference to a target in a snapshot.

    Fields mirror `protocol_models.TargetRef` but with two
    observer-friendly differences:

      * `target_id` is optional (empty string is the "unknown"
        state). The resolver falls back to `selector_hint`.
      * `selector_hint` may carry an extended key
        `text_contains` (substring match) and `rect` (rect-proximity
        match). These are documented in `resolver.py`.

    Strict mode: `from_dict` rejects unknown fields at the
    `ObservationRef` path.
    """

    snapshot_id: str
    target_id: str = ""
    expected_process_name: str = ""
    fingerprint: str | None = None
    selector_hint: Mapping[str, Any] | None = None

    _ALLOWED: frozenset[str] = frozenset({
        "snapshot_id", "target_id", "expected_process_name",
        "fingerprint", "selector_hint",
    })

    def __post_init__(self) -> None:
        _require_str(self.snapshot_id, path="snapshot_id",
                     field_name="snapshot_id")
        if not isinstance(self.target_id, str):
            raise ProtocolModelError(
                "type_error",
                f"target_id must be a string, got {type(self.target_id).__name__}",
                path="target_id",
                value=type(self.target_id).__name__,
            )
        if self.expected_process_name:
            _require_str(self.expected_process_name,
                         path="expected_process_name",
                         field_name="expected_process_name")
        _allow_none_or_str(self.fingerprint, path="fingerprint",
                            field_name="fingerprint")
        if self.selector_hint is not None and not isinstance(
            self.selector_hint, Mapping
        ):
            raise ProtocolModelError(
                "type_error",
                f"selector_hint must be a mapping, got {type(self.selector_hint).__name__}",
                path="selector_hint",
                value=type(self.selector_hint).__name__,
            )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ObservationRef":
        kwargs = _strict_from_dict_kwargs(
            cls._ALLOWED, data, path="ObservationRef",
        )
        # Empty target_id is the "unknown" state; JSON often omits it.
        kwargs.setdefault("target_id", "")
        return cls(**kwargs)


__all__ = [
    "OBSERVATION_REF_SCHEMA_VERSION",
    "ObservationRef",
]