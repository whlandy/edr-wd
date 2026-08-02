"""P1.3 acceptance gate — event ids (FR-P1.3-10)."""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest


_REPO = Path(__file__).resolve().parents[2]
for p in (str(_REPO / "target"), str(_REPO / "agent")):
    if p not in sys.path:
        sys.path.insert(0, p)


from trace import (  # noqa: E402
    generate_branch_id,
    generate_call_id,
    generate_event_id,
    generate_plan_id,
    generate_trace_id,
)


def test_event_id_format():
    eid = generate_event_id(now_ns=1_700_000_000_000_000_000)
    assert eid.startswith("EVT-")
    assert len(eid) == len("EVT-") + 16  # 12 hex ms + 4 hex random
    suffix = eid.split("-", 1)[1]
    assert re.fullmatch(r"[0-9a-f]{16}", suffix), suffix


def test_event_id_unique():
    """Same `now_ns`, different randoms — all distinct."""
    ids = {generate_event_id(now_ns=1_700_000_000_000_000_000) for _ in range(50)}
    assert len(ids) == 50


def test_trace_id_format():
    tid = generate_trace_id()
    assert tid.startswith("TR-")
    assert len(tid) == len("TR-") + 16


def test_branch_id_format():
    bid = generate_branch_id()
    assert bid.startswith("BR-")
    assert len(bid) == len("BR-") + 16


def test_plan_id_format():
    pid = generate_plan_id()
    assert pid.startswith("PLAN-")
    assert len(pid) == len("PLAN-") + 16


def test_call_id_format():
    cid = generate_call_id()
    assert cid.startswith("CALL-")
    assert len(cid) == len("CALL-") + 16
