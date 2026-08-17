"""
assignment.py — Deterministic target_id assignment (architecture §8.1).

Contract:

  * `AssignmentResult` is a frozen dataclass carrying the per-target
    dicts (with `target_id`, `fingerprint`, `fingerprint_fields`
    added) plus the snapshot-level `tree_digest`. P0.3 keeps the
    return type typed (no bare tuples) so callers cannot mistake
    the digest for one of the targets.

  * The output order is deterministic: sort key is
    `(process_name, pid, native_window_id, native_index)` where
    `native_index` is the position in the caller's list. The caller
    is responsible for the *backend's* stable traversal order; this
    module only applies the secondary sort keys documented in
    architecture §8.1.

  * target_id is `T0001`, `T0002`, ... per architecture §8.1. The
    first target in the sorted order is T0001.

  * `fingerprint` is computed from `IDENTITY_FIELDS` only
    (P0.3 review: pid / process_name belong to the ownership layer,
    not to the stable identity). Two observations of the same
    underlying control across an EDR client restart will produce
    the same fingerprint despite a pid change.

  * `tree_digest` is the snapshot-level content-addressed digest
    (architecture §8.3). It is computed from `IDENTITY_FIELDS`
    and excludes `target_id` so the digest is independent of input
    order and of the per-snapshot ordinals.

P0.3 never calls a live backend. Callers must supply pre-sorted
lists (or unsorted — the module sorts them by the documented key).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Iterable, Mapping

try:
    from ..protocol_models.canonical_json import canonical_bytes
except ImportError:  # target-local deployment
    from protocol_models.canonical_json import canonical_bytes

from .fingerprint import (
    IDENTITY_FIELDS,
    compute_fingerprint_from_dict,
)
from .ids import format_target_id


# Primary sort key documented in architecture §8.1. P0.3 does NOT
# ask the backend for a sort order — it relies on the caller having
# walked the target tree in the backend's stable order, then
# applies these secondary keys as a tie-breaker.
_SORT_KEYS = ("process_name", "pid", "native_window_id")


@dataclass(frozen=True)
class AssignmentResult:
    """Result of deterministic target_id assignment.

    `targets` is a tuple of dicts, each augmented with
    `target_id`, `fingerprint`, and `fingerprint_fields`. The tuple
    is ordered by `(process_name, pid, native_window_id,
    native_index)` (stable). `tree_digest` is the snapshot-level
    content hash, computed from `IDENTITY_FIELDS` only.
    """

    targets: tuple[dict, ...]
    tree_digest: str


def _sort_key(t: Mapping[str, object], native_index: int) -> tuple:
    """Return a tuple usable as a `list.sort` key. The trailing
    `native_index` is the original backend-traversal position —
    a stable tie-breaker when two targets share
    `(process_name, pid, native_window_id)`."""
    return (
        str(t.get("process_name", "")),
        t.get("pid") if t.get("pid") is not None else -1,
        str(t.get("native_window_id", "")),
        native_index,
    )


def assign_target_ids(targets: Iterable[Mapping[str, object]]) -> AssignmentResult:
    """Assign target_id + fingerprint to each target, then compute
    the snapshot's tree_digest.

    Returns an `AssignmentResult` (frozen dataclass). Each target
    dict is augmented with `target_id`, `fingerprint`, and
    `fingerprint_fields`. The list is sorted by the documented key
    (see module docstring). The function does not populate
    `snapshot_id`; the caller does that with the P0.2
    `new_snapshot_id()` generator.

    `tree_digest` is computed from the per-target fingerprints
    sorted by fingerprint digest, using canonical JSON, so two
    snapshots with the same set of targets produce the same
    tree_digest regardless of backend-traversal order.
    """
    enumerated = list(enumerate(targets))
    # Sort by documented key; native_index is the stable tie-breaker.
    enumerated.sort(key=lambda pair: _sort_key(pair[1], pair[0]))

    # First pass: assign target_ids.
    out: list[dict] = []
    for ordinal_zero_based, (_, t) in enumerate(enumerated):
        # Copy to avoid mutating the caller's dict.
        enriched = dict(t)
        ordinal = ordinal_zero_based + 1  # 1-based
        enriched["target_id"] = format_target_id(ordinal)
        fp = compute_fingerprint_from_dict(t)
        enriched["fingerprint"] = fp.digest
        enriched["fingerprint_fields"] = list(fp.fields)
        out.append(enriched)

    # Second pass: tree_digest. Sort by `fingerprint` (content-hash)
    # so the digest is independent of input order and of the
    # target_id ordinals. Sort by target_id would couple the digest
    # to the stable-sort tie-breaker; tree_digest must be a pure
    # content-addressed fingerprint (architecture §8.3).
    out_for_digest = sorted(
        [{k: v for k, v in d.items()
          if k in {"fingerprint", *IDENTITY_FIELDS}}
         for d in out],
        key=lambda d: d["fingerprint"],
    )
    tree_digest = "sha256:" + hashlib.sha256(
        canonical_bytes(out_for_digest)
    ).hexdigest()
    return AssignmentResult(
        targets=tuple(out),
        tree_digest=tree_digest,
    )


__all__ = [
    "AssignmentResult",
    "assign_target_ids",
    "_SORT_KEYS",
]
