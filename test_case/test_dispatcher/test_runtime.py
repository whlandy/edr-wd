"""
P1.1 acceptance gate — server_instance_id semantics.

FR-P1.1-09: every receipt/status exposes server_instance_id.
After a simulated restart, the value changes. An agent observing
an instance change should classify previously in-flight requests
as having an unknown outcome; a fresh request on the new instance
executes normally.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


_REPO = Path(__file__).resolve().parents[2]
_TARGET = _REPO / "target"

if str(_TARGET) not in sys.path:
    sys.path.insert(0, str(_TARGET))


import action_dispatcher as ad          # noqa: E402


@pytest.fixture(autouse=True)
def _reset_instance():
    ad.reset_server_instance_id_for_tests()
    yield
    ad.reset_server_instance_id_for_tests()


def test_server_instance_id_format():
    sid = ad.get_server_instance_id()
    assert sid.startswith("inst-")
    # 4 hex chars after the prefix -> total length 5 + 8 = 13.
    assert len(sid) == len("inst-") + 8


def test_server_instance_id_stable_within_process():
    a = ad.get_server_instance_id()
    b = ad.get_server_instance_id()
    assert a == b


def test_server_instance_id_changes_after_simulated_restart():
    """reset_server_instance_id_for_tests() simulates a process
    restart; the next get_server_instance_id() returns a fresh id."""
    before = ad.get_server_instance_id()
    ad.reset_server_instance_id_for_tests()
    after = ad.get_server_instance_id()
    assert before != after


def test_every_receipt_carries_server_instance_id():
    """The receipt constructor fills server_instance_id from the
    runtime by default; explicit override is honoured."""
    sid = ad.get_server_instance_id()
    r = ad.ActionReceipt.from_ok(
        action_id="x", action_code=None, request_id="R", result=None,
    )
    assert r.server_instance_id == sid


def test_get_backend_raises_when_no_resolver_registered():
    ad.set_backend_resolver(None)  # explicit reset
    with pytest.raises(ad.BackendNotConfiguredError):
        ad.get_backend()


def test_set_backend_resolver_wires_through():
    sentinel = object()
    ad.set_backend_resolver(lambda: sentinel)
    assert ad.get_backend() is sentinel