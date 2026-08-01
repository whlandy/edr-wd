"""
canonical_json.py — Deterministic JSON for P0.2 wire models (architecture §10).

This module exposes a generic canonical-bytes helper used by the
P0.2 wire models (and any future consumer that needs content-addressed
identity). P0.1's `action_catalog.digest.canonical_catalog_bytes` is
field-specific (it knows the catalog fields); this helper is generic
and works on any dataclass / dict / list structure.

Determinism guarantees:

    * JSON: sort_keys=True, separators=(",", ":"), ensure_ascii=False.
    * Bytes: UTF-8.
    * Order: dictionary keys ascending, list order preserved.
    * Floating point: rounded to 6 decimal places to avoid
      platform-specific repr noise (architecture §10 step 1 strict
      JSON; NaN/Inf are rejected).

P0.2 rule: this helper MUST NOT depend on Pydantic. It accepts plain
dataclasses (already P0.1-style) and dicts. If a future checkpoint
moves to Pydantic, this module will gain a `model_to_canonical_dict`
adapter but stays stdlib-only.

Caller responsibility: the caller decides which fields to expose.
For example, when hashing an `ActionReceipt`, the caller must NOT
include the `request_id` field — it is the *subject* of the digest,
not its input. Always pre-filter to the fields you intend to
content-address.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, is_dataclass
from typing import Any


def _normalize(value: Any) -> Any:
    """Recursively normalise a value to a JSON-safe representation.

    * dataclass → dict via `asdict`
    * dict → dict with sorted keys (handled by json.dumps itself)
    * list / tuple → list (preserves declaration order)
    * set / frozenset → sorted list of normalized elements. Sorting
      uses the canonical JSON bytes of each element so that the
      sort key is deterministic across runs (a set of
      `{"b","a"}` and a set of `{"a","b"}` produce identical
      output). This preserves the determinism contract.
    * float → rounded, or None if NaN / Inf
    * int / str / bool / None → unchanged
    * everything else → raise TypeError with a helpful message
    """
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            raise TypeError(
                f"canonical_json does not accept NaN/Inf floats (got {value!r})"
            )
        return round(value, 6)
    if isinstance(value, dict):
        return {str(k): _normalize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalize(v) for v in value]
    if isinstance(value, (set, frozenset)):
        # Sort by canonical JSON bytes so two sets with the same
        # elements but different runtime iteration order produce
        # byte-identical output. This is the determinism fix
        # flagged by the P0.2 review.
        normalised = [_normalize(v) for v in value]
        normalised.sort(
            key=lambda x: json.dumps(
                x,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            )
        )
        return normalised
    if is_dataclass(value) and not isinstance(value, type):
        return _normalize(asdict(value))
    raise TypeError(
        f"canonical_json does not support value of type {type(value).__name__}"
    )


def canonical_bytes(obj: Any) -> bytes:
    """Serialize `obj` to deterministic UTF-8 JSON bytes.

    The output is byte-identical for two inputs whose normalized
    forms are equal (e.g. dicts with the same keys/values regardless
    of insertion order; nested structures recursively).
    """
    normalized = _normalize(obj)
    return json.dumps(
        normalized,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def canonical_sha256(obj: Any) -> str:
    """Return `sha256:<hex>` over `canonical_bytes(obj)`."""
    return "sha256:" + hashlib.sha256(canonical_bytes(obj)).hexdigest()


__all__ = ["canonical_bytes", "canonical_sha256"]