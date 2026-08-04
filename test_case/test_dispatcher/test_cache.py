"""
P1.1 acceptance gate — bounded LRU idempotency cache.

FR-P1.1-02 / FR-P1.1-08.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest


_REPO = Path(__file__).resolve().parents[2]
_TARGET = _REPO / "target"

if str(_TARGET) not in sys.path:
    sys.path.insert(0, str(_TARGET))


import action_dispatcher as ad          # noqa: E402


@pytest.fixture(autouse=True)
def _clear():
    ad.cache_clear()
    yield
    ad.cache_clear()


def _receipt(action_id: str = "gui.click"):
    return ad.ActionReceipt.from_ok(
        action_id=action_id,
        action_code="A020",
        request_id="R",
        result={"ok": True},
    )


def test_store_then_lookup_round_trip():
    r = _receipt()
    ad.store("R1", r)
    assert ad.lookup("R1") == r


def test_lookup_misses_unknown():
    assert ad.lookup("never-stored") is None


def test_lookup_misses_empty_string():
    """Empty / non-string request_ids are treated as cache miss
    rather than raising."""
    assert ad.lookup("") is None
    assert ad.lookup(None) is None
    assert ad.lookup(123) is None


def test_store_ignores_empty_or_non_string():
    """Empty / non-string keys are dropped silently — caller's
    responsibility to ensure request_id is a non-empty string."""
    r = _receipt()
    ad.store("", r)
    ad.store(None, r)
    assert ad.lru_size() == 0


def test_lru_eviction_at_cap():
    """Insert cap+1 entries; the first inserted is evicted
    (FIFO at the front when cap is exceeded)."""
    cap = 4
    for i in range(cap):
        ad.store(f"R{i}", _receipt(), max_entries=cap)
    assert ad.lru_size() == cap

    # Touch an earlier entry so it becomes "recent"; inserting a
    # new entry should evict the oldest still-cold one (R0 if
    # untouched; here all are cold, so R0 is evicted).
    ad.store("R4", _receipt(), max_entries=cap)
    assert ad.lru_size() == cap
    assert ad.lookup("R0") is None
    assert ad.lookup("R4") is not None


def test_lru_promotes_recently_used():
    cap = 3
    ad.store("A", _receipt(), max_entries=cap)
    ad.store("B", _receipt(), max_entries=cap)
    ad.store("C", _receipt(), max_entries=cap)
    # Touch A so it becomes the most recently used.
    ad.lookup("A")
    ad.store("D", _receipt(), max_entries=cap)
    assert ad.lookup("A") is not None  # promoted
    assert ad.lookup("B") is None     # evicted
    assert ad.lookup("C") is not None
    assert ad.lookup("D") is not None


def test_default_max_entries_constant():
    assert ad.DEFAULT_MAX_ENTRIES == 1024


def test_capacity_remaining_shrinks():
    cap = 3
    ad.store("A", _receipt(), max_entries=cap)
    ad.store("B", _receipt(), max_entries=cap)
    assert ad.lru_capacity_remaining(cap) == 1
    ad.store("C", _receipt(), max_entries=cap)
    assert ad.lru_capacity_remaining(cap) == 0
    # Insert one more — eviction kicks in, capacity still 0.
    ad.store("D", _receipt(), max_entries=cap)
    assert ad.lru_capacity_remaining(cap) == 0


# ---------------------------------------------------------------------------
# P1.1 review #2: inflight_lock exception cleanup
# ---------------------------------------------------------------------------


def test_inflight_lock_removed_after_exception():
    """P1.1 review #2: when the body of an `inflight_lock` block
    raises, the lock must be released AND removed from the
    inflight map so subsequent callers do not see a stale entry.
    """
    try:
        with ad.inflight_lock("R-exc"):
            raise RuntimeError("backend boom")
    except RuntimeError:
        pass
    # inflight map must be empty after the exception.
    assert ad.inflight_count() == 0


def test_inflight_lock_subsequent_caller_starts_fresh_after_exception():
    """After a prior caller raises inside inflight_lock, a fresh
    caller using the same request_id must succeed (no leftover
    lock blocking the wait)."""
    # First caller raises.
    with pytest.raises(RuntimeError):
        with ad.inflight_lock("R-after-exc"):
            raise RuntimeError("first call boom")
    assert ad.inflight_count() == 0
    # Second caller runs cleanly.
    entered = False
    with ad.inflight_lock("R-after-exc"):
        entered = True
    assert entered is True


def test_inflight_lock_releases_under_nested_block():
    """Two nested inflight_lock scopes (different ids) release
    independently. Outer raises; inner lock still cleaned up."""
    with pytest.raises(RuntimeError):
        with ad.inflight_lock("R-outer"):
            with ad.inflight_lock("R-inner"):
                assert ad.inflight_count() == 2
            assert ad.inflight_count() == 1
            raise RuntimeError("outer boom")
    assert ad.inflight_count() == 0


def test_inflight_lock_concurrent_caller_blocks_then_succeeds():
    """Thread B blocks on Thread A's inflight_lock; when A finishes
    cleanly, B acquires the lock and runs.

    Choreography:

      1. A enters inflight_lock("R-pair") and signals `a_inflight`.
      2. B waits for `a_inflight`, then tries to enter the same
         inflight_lock. B is forced to wait until A exits.
      3. A sleeps briefly, then exits its block.
      4. B's acquire returns. Both threads exit cleanly and
         `inflight_count` drops back to zero.

    We do NOT attempt to assert that B "waited" before A exited
    (such an assertion is racy by nature); the positive
    guarantee we test is that B eventually acquires the lock
    after A, and that the inflight map is empty afterwards.
    """
    a_inflight = threading.Event()
    b_attempting = threading.Event()
    b_acquired = threading.Event()

    def caller_a():
        with ad.inflight_lock("R-pair"):
            a_inflight.set()
            assert b_attempting.wait(timeout=2.0), "B never attempted to acquire"
            # Give B time to block on the same request-id lock before A exits.
            time.sleep(0.05)
        # Exiting the block; B should now be able to acquire.

    def caller_b():
        assert a_inflight.wait(timeout=2.0), "A never entered"
        b_attempting.set()
        with ad.inflight_lock("R-pair"):
            # We acquired the lock; signal completion.
            b_acquired.set()

    tb = threading.Thread(target=caller_b)
    tb.start()
    ta = threading.Thread(target=caller_a)
    ta.start()
    ta.join(timeout=3.0)
    tb.join(timeout=3.0)
    assert b_acquired.is_set(), "B never acquired the lock"
    assert ad.inflight_count() == 0
    assert not ta.is_alive()
    assert not tb.is_alive()
