"""
ids.py — Snapshot/target ID helpers (P0.3).

This module re-uses the P0.2 sortable ID generator for snapshot
identifiers and adds a deterministic target_id formatter. Snapshot
ids are global (cross-process sortable); target ids are observation-
local (T0001, T0002, ... within one snapshot, see architecture §8.1).

P0.3 deliberately does not introduce a new ID format; the snapshot_id
format is whatever P0.2 produced (`{ms:013d}-{seq:04x}-{SNAP}-{rand8}`),
and target_id is a zero-padded 4-digit decimal per-snapshot ordinal.
The resolver's typed-error codes live in `enums.py`.
"""

from __future__ import annotations

from protocol_models.ids import (
    SCOPE_SNAP,
    VALID_SCOPES,
    new_snapshot_id,
)


__all__ = [
    "SCOPE_SNAP",
    "VALID_SCOPES",
    "new_snapshot_id",
    "format_target_id",
    "parse_target_id",
    "TARGET_ID_FORMAT",
    "TARGET_ID_REGEX",
]


# Architecture §8.1: target_id is a 4-digit zero-padded decimal
# (T0001, T0002, ...). Deterministic per snapshot.
TARGET_ID_FORMAT = "T{:04d}"
TARGET_ID_REGEX = r"^T(\d{4,})$"


def format_target_id(ordinal: int) -> str:
    """Format a 1-based ordinal into a target_id (T0001, T0002, ...).

    The resolver uses 1-based ordinals (T0001 is the first target).
    """
    if not isinstance(ordinal, int) or ordinal < 1:
        raise ValueError(
            f"target_id ordinal must be a positive int, got {ordinal!r}"
        )
    return TARGET_ID_FORMAT.format(ordinal)


def parse_target_id(target_id: str) -> int:
    """Return the 1-based ordinal encoded in `target_id`.

    Raises ValueError if the string is not a valid target_id.
    """
    import re
    m = re.match(TARGET_ID_REGEX, target_id)
    if not m:
        raise ValueError(f"malformed target_id: {target_id!r}")
    return int(m.group(1))