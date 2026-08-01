"""
ids.py — Cross-process sortable ID generators (P0.2 Open Decision #3
revised after P0.2 review).

Decision (recorded in CHANGELOG.md):

    P0.1 review was satisfied with stdlib `uuid.uuid4()` because P0.1
    catalog is immutable and does not need runtime IDs. P0.2 review
    identified that trace_id / event_id / evidence_id / plan_id MUST
    be **globally sortable** across processes for the append-only
    trace architecture (§12). UUIDv7 requires Python 3.14; ULID needs
    a 26-char encoding helper. P0.2 ships a stdlib-only sortable ID:

        {timestamp_ms:013d}-{seq:04x}-{scope}-{rand8}

        Examples:
            0000755312345-0001-PLAN-9c5e4a13
            0000755312345-0002-STEP-d04ab1d2
            0000755312346-0001-REQ-7e1c2c4f

    Sort properties:

        * Cross-process: the 13-digit unix_ms timestamp dominates
          lexicographic order. Two IDs from different processes
          sort correctly **as long as wall clocks are synchronized
          (or at most slightly skewed)**. Hosts with large clock
          skew can violate the total order; the 8-hex random
          suffix gives ~32 bits of entropy that mitigates
          accidental collisions in that scenario, but does not
          make a single skewed host's IDs globally consistent.
        * Same-process same-ms: the 4-hex monotonic counter breaks
          ties. Counter resets whenever the millisecond rolls
          over, so the full ID stays sortable within a process.
        * Same-process same-ms counter cap: `seq` is 4 hex
          characters, i.e. 65536 IDs per millisecond per process.
          Beyond that, the counter overflows and IDs may not be
          unique within the millisecond. For the EDR-WD test
          workload this is far above any realistic rate; if it
          ever becomes a constraint, the seq field should be
          widened to 6 hex (16M/ms) rather than adding a new
          prefix.
        * Total length: 13 + 1 + 4 + 1 + scope + 1 + 8 = 28 + len(scope).
        * Random suffix (8 hex) defends against accidental
          collisions when two processes produce the same
          timestamp + same seq (e.g. clock skew).

Stability: the format is the contract. Changing scope names,
separator, or the hex case is a MAJOR bump. Widening the
timestamp or seq fields is a MINOR bump. Switching the underlying
clock source is a MAJOR bump.

Thread safety: the per-millisecond counter is guarded by a
module-level `_lock`. Two threads in the same process get distinct
sequence numbers.
"""

from __future__ import annotations

import secrets
import threading
import time


# Scope tags — kept short so IDs fit in logs / event payloads.
SCOPE_PLAN     = "PLAN"
SCOPE_STEP     = "STEP"
SCOPE_REQ      = "REQ"     # request_id (P1.1 idempotency)
SCOPE_CP       = "CP"      # checkpoint_id (P2.1)
SCOPE_EVT      = "EVT"     # event_id (P1.3)
SCOPE_BRANCH   = "BR"      # branch_id (P2.2)
SCOPE_SNAP     = "SNAP"    # snapshot_id (P0.3)
SCOPE_EVID     = "EVID"    # evidence_id (P1.4)
SCOPE_TRACE    = "TRACE"   # trace_id (P1.3)

VALID_SCOPES: frozenset[str] = frozenset({
    SCOPE_PLAN, SCOPE_STEP, SCOPE_REQ, SCOPE_CP, SCOPE_EVT,
    SCOPE_BRANCH, SCOPE_SNAP, SCOPE_EVID, SCOPE_TRACE,
})


# Per-millisecond counter state. The lock guards the (last_ms, counter)
# tuple. When `time.time_ns()` shows a new millisecond, the counter
# resets to zero. The lock is acquired only when the milliseconds are
# equal, which keeps the hot path nearly uncontended.
_seq_lock = threading.Lock()
_last_ms: int = -1
_counter: int = 0


def _next_seq_for(now_ns: int) -> tuple[str, int]:
    """Return (13-digit-ms-string, 4-hex-seq) for `now_ns`.

    The function is internally synchronised: `now_ms` is computed
    outside the lock to minimise contention; the lock is only taken
    when we need to either increment the counter or reset it on a
    millisecond rollover.
    """
    global _last_ms, _counter
    now_ms = now_ns // 1_000_000
    with _seq_lock:
        if now_ms != _last_ms:
            _last_ms = now_ms
            _counter = 0
        else:
            _counter += 1
        return f"{now_ms:013d}", _counter


def _new_id(scope: str) -> str:
    """Build a new sortable ID with the given scope tag."""
    if scope not in VALID_SCOPES:
        raise ValueError(
            f"unknown id scope {scope!r}; expected one of {sorted(VALID_SCOPES)}"
        )
    # Snapshot both clock readings up front so a slow call between
    # `_next_seq_for` and `secrets.token_hex` cannot produce an ID
    # where the seq counter looks earlier than the random suffix.
    now_ns = time.time_ns()
    ms, counter = _next_seq_for(now_ns)
    rand = secrets.token_hex(4)  # 8 hex chars
    return f"{ms}-{counter:04x}-{scope}-{rand}"


# Public per-scope constructors. Each one exists so callers cannot
# accidentally invent scope names that bypass VALID_SCOPES.

def new_plan_id()    -> str: return _new_id(SCOPE_PLAN)
def new_step_id()    -> str: return _new_id(SCOPE_STEP)
def new_request_id() -> str: return _new_id(SCOPE_REQ)
def new_checkpoint_id() -> str: return _new_id(SCOPE_CP)
def new_event_id()   -> str: return _new_id(SCOPE_EVT)
def new_branch_id()  -> str: return _new_id(SCOPE_BRANCH)
def new_snapshot_id()-> str: return _new_id(SCOPE_SNAP)
def new_evidence_id()-> str: return _new_id(SCOPE_EVID)
def new_trace_id()   -> str: return _new_id(SCOPE_TRACE)


# Reset hook — used by tests to make IDs deterministic across runs.
# Not part of the public API; intentionally not in __all__.
def _reset_seq() -> None:
    """Test-only: reset the per-ms counter. Do not call from
    production code."""
    global _last_ms, _counter
    with _seq_lock:
        _last_ms = -1
        _counter = 0


def _id_parts(id_: str) -> tuple[str, int, str, str]:
    """Parse an ID produced by this module. Useful for tests.

    Returns (ms_str, seq_int, scope, rand_hex).
    """
    parts = id_.split("-")
    if len(parts) != 4:
        raise ValueError(f"malformed ID: {id_!r}")
    ms_str, seq_hex, scope, rand = parts
    if scope not in VALID_SCOPES:
        raise ValueError(f"unknown scope {scope!r} in {id_!r}")
    if len(rand) != 8:
        raise ValueError(f"unexpected random length in {id_!r}")
    seq_int = int(seq_hex, 16)
    return ms_str, seq_int, scope, rand


__all__ = [
    "SCOPE_PLAN", "SCOPE_STEP", "SCOPE_REQ", "SCOPE_CP",
    "SCOPE_EVT", "SCOPE_BRANCH", "SCOPE_SNAP", "SCOPE_EVID",
    "SCOPE_TRACE", "VALID_SCOPES",
    "new_plan_id", "new_step_id", "new_request_id",
    "new_checkpoint_id", "new_event_id", "new_branch_id",
    "new_snapshot_id", "new_evidence_id", "new_trace_id",
]