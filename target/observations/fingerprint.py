"""
fingerprint.py — Compute the per-Target fingerprint (architecture §8.3).

The fingerprint is `sha256:<hex>` over canonical JSON bytes of the
**identity** subset of a Target's fields. By design:

  * Coordinates (rect) and transient list position are EXCLUDED.
  * Auto-generated numeric control IDs are EXCLUDED unless no better
    native identity exists (FR-P0.3-04).
  * `fingerprint_fields` records which keys contributed, so a stale
    resolution can be debugged.

P0.3 review (the original implementation mixed pid / process_name
into the fingerprint, which would invalidate every target when the
process restarted). The fingerprint now covers the **identity**
fields only — those that describe what a control *is*, not the
runtime ownership of the process.

Layer separation (architecture §8.2):

  IDENTITY_FIELDS
      automation_id, native_window_id, control_type, title, text,
      kind — stable across process restarts and short-lived
      runtime churn.

  OWNERSHIP_FIELDS
      process_name, pid — used by the resolver's step-1 ownership
      check; NEVER enter the fingerprint.

P0.3 does not consult a live backend: the caller supplies a Target
(or a dict) and we compute the fingerprint from the identity
fields.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

try:
    from ..protocol_models.canonical_json import canonical_bytes
except ImportError:  # target-local deployment
    from protocol_models.canonical_json import canonical_bytes

from .models import Target


# Identity fields used to compute the fingerprint (P0.3 review).
# These describe what a control *is*. They MUST survive process
# restarts so that EDR client PID churn does not invalidate the
# entire snapshot.
IDENTITY_FIELDS: tuple[str, ...] = (
    "automation_id",
    "native_window_id",
    "control_type",
    "title",
    "text",
    "kind",
)

# Ownership fields — used by the resolver's ownership check
# (architecture §8.2 step 1). These describe the runtime host of
# the control and are NOT stable across process restarts.
OWNERSHIP_FIELDS: tuple[str, ...] = (
    "process_name",
    "pid",
)

# Back-compat alias. P0.3 callers that imported STABLE_FIELDS
# before the split see the same surface but with the corrected
# semantics (no ownership fields).
STABLE_FIELDS: tuple[str, ...] = IDENTITY_FIELDS


@dataclass(frozen=True)
class FingerprintResult:
    """Result of a fingerprint computation."""

    digest: str
    fields: tuple[str, ...]


def compute_fingerprint(target: Target) -> FingerprintResult:
    """Compute the identity-only fingerprint for a Target.

    The output is byte-deterministic across processes and Python
    versions: it depends only on the canonical JSON of the
    identity fields, which the P0.2 helper sorts and normalises.
    """
    payload = {
        f: _extract(target, f) for f in IDENTITY_FIELDS
    }
    raw = canonical_bytes(payload)
    import hashlib
    digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    return FingerprintResult(digest=digest, fields=IDENTITY_FIELDS)


def compute_fingerprint_from_dict(
    target_dict: Mapping[str, object],
) -> FingerprintResult:
    """Compute the fingerprint from a dict (used by snapshot
    builders that have not yet materialised a `Target`).

    Missing keys are treated as `None`. Extra keys are ignored.
    """
    payload: dict[str, object] = {}
    for f in IDENTITY_FIELDS:
        if f in target_dict:
            payload[f] = target_dict[f]
        else:
            payload[f] = None
    raw = canonical_bytes(payload)
    import hashlib
    digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    # Only fields that were actually present appear in `fields`.
    fields = tuple(f for f in IDENTITY_FIELDS if f in target_dict)
    return FingerprintResult(digest=digest, fields=fields)


def _extract(target: Target, field_name: str) -> object:
    """Return the field value, treating empty-string as None for
    optional string fields (so two Targets differing only in
    `title == ""` vs `title == None` fingerprint the same)."""
    val = getattr(target, field_name)
    if field_name in {"title", "control_type", "automation_id", "text"} and val == "":
        return None
    return val


__all__ = [
    "IDENTITY_FIELDS",
    "OWNERSHIP_FIELDS",
    "STABLE_FIELDS",
    "FingerprintResult",
    "compute_fingerprint",
    "compute_fingerprint_from_dict",
]
