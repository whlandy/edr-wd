"""
__init__.py — Public surface of the observations package (P0.3).

Layering (parallel to action_catalog and protocol_models):

    target/observations/
        enums.py            — TARGET_KIND_* + stable error codes
        ids.py              — snapshot_id + target_id helpers
        models.py           — Target, ObservationSnapshot (strict)
        fingerprint.py      — per-target stable-field digest
        assignment.py       — deterministic target_id assignment
        snapshot.py         — build_snapshot from pre-collected data
        invalidate.py       — snapshot registry (P1.1 dispatcher
                              wires this to mutating actions)
        resolver.py         — 6-step pipeline + typed errors

P0.3 owns: deterministic observation-local identity and stable
typed-resolution. P0.3 does NOT probe live backends (FR-P0.3-08); it
consumes pre-collected target dicts and returns ObservationSnapshot.

server.py wiring (snapshot_id + per-control target_id on the
existing dump_tree/find_control/list_windows) is an integration
commit, gated on review and on the existence of a backend adapter
that can produce the target dicts. For P0.3 the wiring is a
shim that does the deterministic re-id using synthetic dicts in
tests; production backend wiring is deferred.

Reviewer gate (architecture §24): P0.3 stops here. P1.1 dispatcher
consumes `resolve_target` and `invalidate_snapshot`.
"""

from __future__ import annotations

from .enums import (
    ARCHITECTURE_P0_3_OBSERVATION_CODES,
    CODE_FALLBACK_NOT_ALLOWED,
    CODE_INVALID_SNAPSHOT_ID,
    CODE_INVALID_TARGET_ID,
    CODE_OWNERSHIP_MISMATCH,
    CODE_TARGET_AMBIGUOUS,
    CODE_TARGET_NOT_FOUND,
    CODE_TARGET_STALE,
    STRATEGY_FINGERPRINT,
    STRATEGY_NATIVE_IDENTITY,
    STRATEGY_OWNERSHIP,
    STRATEGY_RECT_PROXIMITY,
    STRATEGY_SELECTOR,
    STRATEGY_TEXT,
    TARGET_KIND_CONTROL,
    TARGET_KIND_WINDOW,
    VALID_RESOLUTION_STRATEGIES,
    VALID_TARGET_KINDS,
)
from .fingerprint import (
    IDENTITY_FIELDS,
    OWNERSHIP_FIELDS,
    STABLE_FIELDS,
    FingerprintResult,
    compute_fingerprint,
    compute_fingerprint_from_dict,
)
from .ids import (
    SCOPE_SNAP,
    TARGET_ID_FORMAT,
    TARGET_ID_REGEX,
    VALID_SCOPES,
    format_target_id,
    new_snapshot_id,
    parse_target_id,
)
from .invalidate import (
    forget,
    invalidate_snapshot,
    is_live,
    mark_invalid,
    mark_live,
    reset_for_tests,  # re-exported under its plain name for tests
)
from .models import (
    OBSERVATION_SCHEMA_VERSION,
    ObservationSnapshot,
    Target,
)
# P0.3 uses its own observer-side ref type (see ref.py docstring).
# protocol_models.TargetRef is the planner-side type and is not
# re-exported from this package to avoid coupling.
from .assignment import AssignmentResult, assign_target_ids
from .ref import OBSERVATION_REF_SCHEMA_VERSION, ObservationRef
from .resolver import TargetResolutionError, resolve_target
from .snapshot import build_snapshot


__all__ = [
    # Schema versions
    "OBSERVATION_SCHEMA_VERSION",
    "OBSERVATION_REF_SCHEMA_VERSION",
    # Models
    "Target",
    "ObservationSnapshot",
    "ObservationRef",
    # Enums
    "TARGET_KIND_WINDOW",
    "TARGET_KIND_CONTROL",
    "VALID_TARGET_KINDS",
    "ARCHITECTURE_P0_3_OBSERVATION_CODES",
    "CODE_TARGET_STALE",
    "CODE_TARGET_NOT_FOUND",
    "CODE_TARGET_AMBIGUOUS",
    "CODE_OWNERSHIP_MISMATCH",
    "CODE_FALLBACK_NOT_ALLOWED",
    "CODE_INVALID_SNAPSHOT_ID",
    "CODE_INVALID_TARGET_ID",
    "VALID_RESOLUTION_STRATEGIES",
    "STRATEGY_OWNERSHIP",
    "STRATEGY_NATIVE_IDENTITY",
    "STRATEGY_FINGERPRINT",
    "STRATEGY_SELECTOR",
    "STRATEGY_TEXT",
    "STRATEGY_RECT_PROXIMITY",
    # IDs
    "SCOPE_SNAP",
    "VALID_SCOPES",
    "new_snapshot_id",
    "format_target_id",
    "parse_target_id",
    "TARGET_ID_FORMAT",
    "TARGET_ID_REGEX",
    # Fingerprint
    "IDENTITY_FIELDS",
    "OWNERSHIP_FIELDS",
    # Back-compat alias for code that imported STABLE_FIELDS in
    # P0.3 review #1; the semantics changed (ownership fields
    # removed). Kept as an alias pointing at IDENTITY_FIELDS so
    # that pre-review imports keep working.
    "STABLE_FIELDS",
    "FingerprintResult",
    "compute_fingerprint",
    "compute_fingerprint_from_dict",
    # Snapshot registry
    "mark_live",
    "mark_invalid",
    "invalidate_snapshot",
    "is_live",
    "forget",
    # Test-only reset. P1.1 review: this is now part of the
    # public surface (still documented as test-only) so the
    # dispatcher's test suite can drive the registry deterministically.
    "reset_for_tests",
    # Snapshot building
    "AssignmentResult",
    "assign_target_ids",
    "build_snapshot",
    # Resolver
    "TargetResolutionError",
    "resolve_target",
]