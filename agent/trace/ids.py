"""
ids.py — Sortable unique event identifiers (FR-P1.3-10).

P1.3 mandates UUIDv7 or ULID. Python 3.11 stdlib does not ship
`uuid.uuid7()`, so we implement a deterministic-but-sortable
event id: 12 hex chars of ms-since-epoch + 4 hex chars random.

The ms component stays within 12 hex digits until year 10889
(well outside any reasonable session lifetime), so lex-sort
matches insertion order across wall clocks.
"""

from __future__ import annotations

import os
import time
import uuid

PREFIX = "EVT"


def generate_event_id(now_ns: int | None = None) -> str:
    """Return a sortable unique id like ``EVT-0198abcd12340001``.

    `now_ns` is an injection seam for tests; defaults to the
    current monotonic-ish clock.
    """
    if now_ns is None:
        now_ns = time.time_ns()
    ms_hex = format(now_ns // 1_000_000, "012x")
    rand_hex = uuid.uuid4().hex[:4]
    return f"{PREFIX}-{ms_hex}{rand_hex}"


def generate_trace_id(now_ns: int | None = None) -> str:
    """Return ``TR-<16 hex>`` for trace identifiers."""
    if now_ns is None:
        now_ns = time.time_ns()
    ms_hex = format(now_ns // 1_000_000, "012x")
    rand_hex = uuid.uuid4().hex[:4]
    return f"TR-{ms_hex}{rand_hex}"


def generate_branch_id(now_ns: int | None = None) -> str:
    """Return ``BR-<16 hex>`` for branch identifiers."""
    if now_ns is None:
        now_ns = time.time_ns()
    ms_hex = format(now_ns // 1_000_000, "012x")
    rand_hex = uuid.uuid4().hex[:4]
    return f"BR-{ms_hex}{rand_hex}"


def generate_plan_id(now_ns: int | None = None) -> str:
    if now_ns is None:
        now_ns = time.time_ns()
    ms_hex = format(now_ns // 1_000_000, "012x")
    rand_hex = uuid.uuid4().hex[:4]
    return f"PLAN-{ms_hex}{rand_hex}"


def generate_call_id(now_ns: int | None = None) -> str:
    if now_ns is None:
        now_ns = time.time_ns()
    ms_hex = format(now_ns // 1_000_000, "012x")
    rand_hex = uuid.uuid4().hex[:4]
    return f"CALL-{ms_hex}{rand_hex}"


__all__ = [
    "PREFIX",
    "generate_event_id",
    "generate_trace_id",
    "generate_branch_id",
    "generate_plan_id",
    "generate_call_id",
]
