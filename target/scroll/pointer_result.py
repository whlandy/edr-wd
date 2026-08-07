"""
pointer_result.py — Unified Pointer Result Envelope (P0.3).

Separates the RAW dispatch signal from the VERIFIED UI effect across the
pointer layer, giving every pointer result a stable, machine-readable code.

Two concepts, one vocabulary:

* raw dispatch — a low-level input event was actually handed to the OS.
  The primitive (backend scroll / scroll_window / click / drag) either sent
  the event or not. Exposed as ``event_dispatched`` on the raw payload.
* verified effect — the composite layer (``ScrollResult``) turns raw
  dispatch into ``dispatched`` + ``moved`` + ``reason`` only after content
  verification (Observer/PageVerifier).

The P0.3 invariant: a raw ``ok=True`` (the event was *sent*) is NEVER
sufficient to tell a user "scroll succeeded". Only a verified composite
with ``moved=True`` is. This module centralises the stable failure codes so
the raw and composite layers agree on the same machine-readable vocabulary
instead of inventing ad-hoc string codes at each call site.

The codes here are the canonical set demanded by P0.3:

* target_occluded            — the intended window/point is covered by
                               another top-level window.
* target_ambiguous           — more than one window matches the selector.
* verification_unavailable   — post-dispatch verification could not run /
                               confirm ownership (strict or degraded).
* no_effect                  — dispatched but content did not move.
* not_dispatched             — nothing was ever handed to the OS.

Plus the adjacent raw-pointer codes already in use so the set is complete
for the pointer layer: target_not_found / point_outside_window /
ownership_mismatch / backend_unsupported / ok.

Pure module — no MCP transport. Composes on raw backend dicts (passed
through the server MCP tools) and on composite ``ScrollResult`` dicts.
"""

from __future__ import annotations

from typing import Mapping

# ── Stable machine-readable codes ──────────────────────────────────────────
# Canonical P0.3 failure vocabulary.
CODE_OK = "ok"
CODE_TARGET_OCCLUDED = "target_occluded"
CODE_TARGET_AMBIGUOUS = "target_ambiguous"
CODE_VERIFICATION_UNAVAILABLE = "verification_unavailable"
CODE_NO_EFFECT = "no_effect"
CODE_NOT_DISPATCHED = "not_dispatched"
CODE_TARGET_NOT_FOUND = "target_not_found"
CODE_POINT_OUTSIDE_WINDOW = "point_outside_window"
CODE_OWNERSHIP_MISMATCH = "ownership_mismatch"
CODE_BACKEND_UNSUPPORTED = "backend_unsupported"

# Every stable code the pointer layer may emit. A consumer that sees a code
# outside this set should treat the result as malformed.
STABLE_POINTER_CODES = frozenset(
    {
        CODE_OK,
        CODE_TARGET_OCCLUDED,
        CODE_TARGET_AMBIGUOUS,
        CODE_VERIFICATION_UNAVAILABLE,
        CODE_NO_EFFECT,
        CODE_NOT_DISPATCHED,
        CODE_TARGET_NOT_FOUND,
        CODE_POINT_OUTSIDE_WINDOW,
        CODE_OWNERSHIP_MISMATCH,
        CODE_BACKEND_UNSUPPORTED,
    }
)

# Fields a pointer result must carry after normalization.
_REQUIRED_KEYS = ("ok", "event_dispatched", "code")

# Dry-run backends report "success" without sending a real OS event. These
# method markers mean the event was NOT actually dispatched to the OS.
_DRY_RUN_METHODS = frozenset({"dry_run", "dry-run"})


# ── Code derivation ────────────────────────────────────────────────────────


def code_for(payload: Mapping) -> str:
    """Best-effort stable code for a raw pointer payload.

    Prefers an explicit ``code`` already on the payload (backend-produced
    codes such as target_occluded); falls back to ``ok``-derived codes
    otherwise. Never raises and never returns a non-stable string.
    """
    existing = payload.get("code")
    if existing in STABLE_POINTER_CODES:
        return existing
    if payload.get("ok") is False:
        error = str(payload.get("error") or "").lower()
        if "occlud" in error or "occluded" in error:
            return CODE_TARGET_OCCLUDED
        if "ambiguous" in error:
            return CODE_TARGET_AMBIGUOUS
        if "not found" in error or "not_found" in error:
            return CODE_TARGET_NOT_FOUND
        if "outside" in error or "out of" in error:
            return CODE_POINT_OUTSIDE_WINDOW
        if "verification" in error or "could not be verified" in error:
            return CODE_VERIFICATION_UNAVAILABLE
        return CODE_NOT_DISPATCHED
    return CODE_OK


def event_dispatched_for(payload: Mapping) -> bool:
    """Whether a real low-level input event was handed to the OS.

    A successful backend return (``ok is True``) normally means the event was
    sent — EXCEPT in dry-run mode, where the backend reports success without
    touching the OS. An explicit ``event_dispatched`` key wins when present.
    """
    if "event_dispatched" in payload:
        return bool(payload["event_dispatched"])
    if payload.get("ok") is not True:
        return False
    method = str(payload.get("method") or "")
    if method.lower() in _DRY_RUN_METHODS:
        return False
    return True


def normalize(payload: Mapping, *, scope: str = "screen_unscoped", tool: str) -> dict:
    """Wrap a raw backend pointer payload into the unified envelope.

    Additive only — every existing key is preserved (so existing consumers
    and tests that read ``ok`` / ``clicks`` / ``point`` keep working) and the
    envelope fields are added:

      * ``ok``              — backend-level success (may be True without an
                              effect; see ``is_verified_success``).
      * ``event_dispatched``— a real OS input event was actually sent.
      * ``code``            — stable machine-readable code.
      * ``scope``           — where the pointer acted.
      * ``tool``            — which MCP primitive produced it.

    ``ok`` and the raw semantics are intentionally NOT enough to call a
    pointer action a success; only the composite layer's ``moved`` is.
    """
    out = dict(payload)
    out["ok"] = payload.get("ok") is True
    out["event_dispatched"] = event_dispatched_for(payload)
    out["code"] = code_for(payload)
    out["scope"] = scope
    out["tool"] = tool
    return out


# ── Composite bridge ───────────────────────────────────────────────────────


def reason_to_code(reason: str | None) -> str:
    """Map a composite ``ScrollResult.reason`` to a stable pointer code."""
    if reason in ("no_effect", "no_scroll_effect"):
        return CODE_NO_EFFECT
    if reason in ("not_dispatched",):
        return CODE_NOT_DISPATCHED
    if reason in ("verification_unavailable",):
        return CODE_VERIFICATION_UNAVAILABLE
    if reason in ("target_occluded", "target_ambiguous", "target_not_found",
                  "point_outside_window", "ownership_mismatch"):
        return reason
    return CODE_OK


def is_verified_success(payload: Mapping) -> bool:
    """True only when a VERIFIED composite reports a real movement.

    Guards the P0.3 invariant: neither raw ``ok=true`` nor
    ``event_dispatched=true`` counts as user-visible success — only a
    composite with ``moved is True`` does.
    """
    return payload.get("moved") is True and payload.get("dispatched") is True


__all__ = [
    "CODE_OK",
    "CODE_TARGET_OCCLUDED",
    "CODE_TARGET_AMBIGUOUS",
    "CODE_VERIFICATION_UNAVAILABLE",
    "CODE_NO_EFFECT",
    "CODE_NOT_DISPATCHED",
    "CODE_TARGET_NOT_FOUND",
    "CODE_POINT_OUTSIDE_WINDOW",
    "CODE_OWNERSHIP_MISMATCH",
    "CODE_BACKEND_UNSUPPORTED",
    "STABLE_POINTER_CODES",
    "code_for",
    "event_dispatched_for",
    "normalize",
    "reason_to_code",
    "is_verified_success",
]
