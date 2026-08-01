"""
enums.py — P0.3 enum sets + typed-error codes.

Stable codes for the observation / resolution pipeline. Architecture
§8.2 enumerates the typed errors that the resolver may raise. The
set is small and stable for V1.

Stability (architecture §7.2):

  * Adding a new error code is a MINOR bump.
  * Removing or renaming one is a MAJOR bump.
  * Adding a new target kind or resolution strategy is a MINOR bump.
"""

from __future__ import annotations


# Architecture §8.2 — Target kinds (V1).
TARGET_KIND_WINDOW  = "window"
TARGET_KIND_CONTROL = "control"

VALID_TARGET_KINDS: frozenset[str] = frozenset({
    TARGET_KIND_WINDOW,
    TARGET_KIND_CONTROL,
})


# Architecture §8.2 — Stable typed-error codes surfaced by the
# resolver and the snapshot registry. Consumers must match on these
# strings, never on exception type alone (architecture §19).
CODE_TARGET_STALE              = "target_stale"
CODE_TARGET_NOT_FOUND          = "target_not_found"
CODE_TARGET_AMBIGUOUS           = "target_ambiguous"
CODE_OWNERSHIP_MISMATCH         = "ownership_mismatch"
CODE_FALLBACK_NOT_ALLOWED       = "fallback_not_allowed"
CODE_INVALID_SNAPSHOT_ID        = "invalid_snapshot_id"
CODE_INVALID_TARGET_ID          = "invalid_target_id"

ARCHITECTURE_P0_3_OBSERVATION_CODES: frozenset[str] = frozenset({
    CODE_TARGET_STALE,
    CODE_TARGET_NOT_FOUND,
    CODE_TARGET_AMBIGUOUS,
    CODE_OWNERSHIP_MISMATCH,
    CODE_FALLBACK_NOT_ALLOWED,
    CODE_INVALID_SNAPSHOT_ID,
    CODE_INVALID_TARGET_ID,
})


# Resolver pipeline strategy steps (architecture §8.2). Listed as
# stable strings so trace events / debugging output can reference them
# by name rather than by index.
STRATEGY_OWNERSHIP       = "ownership_check"
STRATEGY_NATIVE_IDENTITY = "exact_native_identity"
STRATEGY_FINGERPRINT     = "exact_fingerprint"
STRATEGY_SELECTOR        = "backend_native_selector"
STRATEGY_TEXT            = "role_type_text"
STRATEGY_RECT_PROXIMITY  = "rect_proximity"

VALID_RESOLUTION_STRATEGIES: frozenset[str] = frozenset({
    STRATEGY_OWNERSHIP,
    STRATEGY_NATIVE_IDENTITY,
    STRATEGY_FINGERPRINT,
    STRATEGY_SELECTOR,
    STRATEGY_TEXT,
    STRATEGY_RECT_PROXIMITY,
})


__all__ = [
    "TARGET_KIND_WINDOW",
    "TARGET_KIND_CONTROL",
    "VALID_TARGET_KINDS",
    "CODE_TARGET_STALE",
    "CODE_TARGET_NOT_FOUND",
    "CODE_TARGET_AMBIGUOUS",
    "CODE_OWNERSHIP_MISMATCH",
    "CODE_FALLBACK_NOT_ALLOWED",
    "CODE_INVALID_SNAPSHOT_ID",
    "CODE_INVALID_TARGET_ID",
    "ARCHITECTURE_P0_3_OBSERVATION_CODES",
    "STRATEGY_OWNERSHIP",
    "STRATEGY_NATIVE_IDENTITY",
    "STRATEGY_FINGERPRINT",
    "STRATEGY_SELECTOR",
    "STRATEGY_TEXT",
    "STRATEGY_RECT_PROXIMITY",
    "VALID_RESOLUTION_STRATEGIES",
]