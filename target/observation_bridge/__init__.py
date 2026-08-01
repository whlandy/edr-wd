"""
__init__.py — observation_bridge package: thin glue between
observation snapshots and the dispatcher.

P1.1 only needs the invalidation bridge. Future checkpoints may
add per-snapshot bookkeeping here (e.g. screenshot evidence
attribution for P1.4).
"""

from __future__ import annotations

from .invalidation import (
    get_active_snapshot,
    invalidate_after_mutation,
    reset_for_tests,
    set_active_snapshot,
)


__all__ = [
    "get_active_snapshot",
    "invalidate_after_mutation",
    "reset_for_tests",
    "set_active_snapshot",
]