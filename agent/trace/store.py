"""
store.py — TraceStore (architecture §12.3, FR-P1.3-01..10).

Manages three on-disk artefacts per trace:

    <root>/<trace_id>/
        events.jsonl     — append-only, one event per line
        manifest.json    — written on finalize
        observations/<sid>.json  — (P1.4; not touched by P1.3)

The store enforces three crash-safety contracts:

  1. Append mode: every `append()` opens the file in append mode,
     writes one canonical-JSON line, fsyncs, and closes. A process
     crash mid-write leaves the prior bytes intact; the partial
     line is repaired on next open.

  2. fsync after mutating actions (architecture §12.3): every event
     that could become a recover-from-crash story (action_result,
     checkpoint creation, failures, terminal events) must be
     durable before the executor proceeds. P1.3 store calls
     fsync on every event for simplicity — the cost is one syscall
     per event, which is fine for the MVP. A future checkpoint can
     batch if needed.

  3. Recovery on open: if the file ends with an incomplete final
     line, the store hashes the partial bytes for diagnostics,
     truncates the file to the last complete newline, fsyncs, and
     appends a `trace_recovered` event whose parent/hash follows
     the last valid event. The resulting JSONL and chain verify
     (FR-P1.3-06).

Corruption in any earlier line (FR-P1.3-07) raises `ChainCorrupted`
on open; no partial repair is attempted.
"""

from __future__ import annotations

import dataclasses
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from target.protocol_models.canonical_json import canonical_bytes

from .events import (
    SCHEMA_VERSION,
    TERMINAL_EVENT_TYPES,
    EventType,
    TraceEvent,
    from_dict,
)
from .ids import (
    generate_branch_id,
    generate_call_id,
    generate_event_id,
    generate_trace_id,
)
from .integrity import (
    IntegrityReport,
    canonical_event_hash,
    load_events,
    verify_chain,
)
from .manifest import SCHEMA_VERSION as MANIFEST_SCHEMA_VERSION
from .manifest import ManifestRecord


class TraceError(Exception):
    """Base for store errors."""


class ChainCorrupted(TraceError):
    """Raised on open when an earlier line fails to parse or hash."""


class AlreadyFinalized(TraceError):
    """Raised when append/finalize is called after a terminal event."""


@dataclass(frozen=True)
class OpenResult:
    trace_id: str
    recovered: bool  # True if a crash-tail was repaired
    issues: tuple[str, ...]  # human-readable issues on open


@dataclass
class _OpenState:
    """Mutable per-trace open state held inside the store instance."""
    trace_id: str
    root: Path
    events_path: Path
    last_event_hash: str | None
    next_sequence: int
    finalized: bool = False
    branch_id: str = ""


class TraceStore:
    """One open trace, one TraceStore instance.

    Not thread-safe across traces; each trace should have its own
    store. Within a trace, the store serialises appends via an
    internal lock if needed (P1.3 keeps the implementation
    single-threaded for clarity).
    """

    def __init__(self, root: Path) -> None:
        self._root = Path(root)

    # ------------------------------------------------------------------
    # Open
    # ------------------------------------------------------------------

    def open(self, trace_id: str | None = None, *, create: bool = True) -> OpenResult:
        """Open (or create) the trace directory.

        On create: mkdir, write no events, generate trace_id.
        On existing: read all events, verify the chain, repair a
        crash-tail if needed (FR-P1.3-06). Raise ChainCorrupted on
        any other error.
        """
        if trace_id is None:
            trace_id = generate_trace_id()
        trace_root = self._root / trace_id
        trace_root.mkdir(parents=True, exist_ok=(create or trace_root.exists()))
        events_path = trace_root / "events.jsonl"

        # First-time open: emit a trace_started + environment_recorded
        # to bootstrap the chain.
        if not events_path.exists() or events_path.stat().st_size == 0:
            if not create:
                raise FileNotFoundError(
                    f"trace {trace_id} has no events.jsonl and create=False"
                )
            self._state = _OpenState(
                trace_id=trace_id,
                root=trace_root,
                events_path=events_path,
                last_event_hash=None,
                next_sequence=1,
                branch_id=generate_branch_id(),
            )
            # Bootstrap with trace_started.
            self._append_raw_event(self._build_event(
                event_type=EventType.TRACE_STARTED,
                payload={"created_by": "TraceStore.open", "schema": SCHEMA_VERSION},
                parent_event_id=None,
                caused_by_event_id=None,
                plan_id=None,
                case_id=None,
                step_id=None,
                call_id=None,
            ))
            return OpenResult(
                trace_id=trace_id, recovered=False, issues=(),
            )

        # Existing file: detect crash-tail BEFORE parsing.
        raw = events_path.read_text(encoding="utf-8")
        has_crash_tail = bool(raw) and not raw.endswith("\n")

        if has_crash_tail:
            # Strip the partial trailing line so the strict parser
            # only sees complete events. The truncated raw is what
            # we hash + verify; the dropped tail is recorded as
            # diagnostic bytes in the trace_recovered event.
            truncated_for_parse = raw.rsplit("\n", 1)[0] + "\n"
        else:
            truncated_for_parse = raw

        events = load_events(truncated_for_parse)
        report = verify_chain(events)
        if not report.ok:
            # Any tampering in any prior line: refuse to open.
            raise ChainCorrupted(
                f"events.jsonl failed integrity check: "
                + "; ".join(i.issue for i in report.issues)
            )

        recovered = False
        recovered_issues: list[str] = []

        if has_crash_tail:
            recovered_issues.append("incomplete final line detected")
            tail = raw.rsplit("\n", 1)[-1]
            last_valid_event = events[-1]
            # Truncate the file to drop the partial tail (already
            # truncated_for_parse above; mirror that to disk).
            events_path.write_text(truncated_for_parse, encoding="utf-8")
            # fsync the truncation.
            with events_path.open("r+b") as fh:
                fh.flush()
                os.fsync(fh.fileno())

            # Append trace_recovered event whose parent is the
            # last VALID event (events[-1]). The hash is computed
            # over the recovered event with this parent + previous_hash.
            self._state = _OpenState(
                trace_id=trace_id,
                root=trace_root,
                events_path=events_path,
                last_event_hash=last_valid_event.event_hash,
                next_sequence=last_valid_event.sequence_no + 1,
                branch_id=last_valid_event.branch_id,
            )
            self._append_raw_event(self._build_event(
                event_type=EventType.TRACE_RECOVERED,
                payload={
                    "tail_bytes_hex": tail.encode("utf-8").hex(),
                    "tail_sha256": "sha256:" + __import__("hashlib").sha256(
                        tail.encode("utf-8")
                    ).hexdigest(),
                    "truncated_at_seq": last_valid_event.sequence_no,
                },
                parent_event_id=last_valid_event.event_id,
                caused_by_event_id=None,
                plan_id=None,
                case_id=None,
                step_id=None,
                call_id=None,
            ))
            recovered = True

        # Finalise state.
        if not recovered:
            self._state = _OpenState(
                trace_id=trace_id,
                root=trace_root,
                events_path=events_path,
                last_event_hash=events[-1].event_hash if events else None,
                next_sequence=(events[-1].sequence_no + 1) if events else 1,
                branch_id=events[-1].branch_id if events else generate_branch_id(),
            )

        return OpenResult(
            trace_id=trace_id,
            recovered=recovered,
            issues=tuple(recovered_issues),
        )

    # ------------------------------------------------------------------
    # Append
    # ------------------------------------------------------------------

    def append(self, event: TraceEvent) -> TraceEvent:
        """Compute the chain hash and append the event.

        Callers should construct a TraceEvent WITHOUT `event_hash`
        (use `append_dict` for the convenient form). The store fills
        in `previous_hash` (from the prior event), `sequence_no`,
        and `event_hash` automatically.
        """
        if self._state.finalized:
            raise AlreadyFinalized(
                f"trace {self._state.trace_id} already finalized"
            )
        # Fill chain fields.
        filled = self._build_event(
            event_type=event.event_type,
            payload=event.payload,
            parent_event_id=event.parent_event_id,
            caused_by_event_id=event.caused_by_event_id,
            plan_id=event.plan_id,
            case_id=event.case_id,
            step_id=event.step_id,
            call_id=event.call_id,
            sequence_no=event.sequence_no or self._state.next_sequence,
            event_id=event.event_id or generate_event_id(),
            recorded_at=event.recorded_at,
            previous_hash=event.previous_hash or self._state.last_event_hash,
        )
        self._append_raw_event(filled)
        return filled

    def append_dict(
        self,
        event_type: EventType,
        *,
        payload: dict | None = None,
        parent_event_id: str | None = None,
        caused_by_event_id: str | None = None,
        plan_id: str | None = None,
        case_id: str | None = None,
        step_id: str | None = None,
        call_id: str | None = None,
        event_id: str | None = None,
        recorded_at: str | None = None,
    ) -> TraceEvent:
        """Convenience: build + append in one call."""
        return self.append(TraceEvent(
            schema_version=SCHEMA_VERSION,
            trace_id=self._state.trace_id,
            branch_id=self._state.branch_id,
            event_id=event_id or "",
            sequence_no=0,
            parent_event_id=parent_event_id,
            caused_by_event_id=caused_by_event_id,
            event_type=event_type,
            recorded_at=recorded_at or _utcnow_iso(),
            plan_id=plan_id,
            case_id=case_id,
            step_id=step_id,
            call_id=call_id,
            payload=payload or {},
            previous_hash=None,
            event_hash="",
        ))

    # ------------------------------------------------------------------
    # Finalize
    # ------------------------------------------------------------------

    def finalize(self, terminal: EventType, payload: dict | None = None) -> TraceEvent:
        """Append a terminal event (trace_completed / trace_aborted) and mark finalised."""
        if terminal not in TERMINAL_EVENT_TYPES:
            raise ValueError(
                f"finalize() requires a terminal event type, got {terminal!r}"
            )
        ev = self.append_dict(terminal, payload=payload or {})
        self._state.finalized = True
        return ev

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def read_events(self) -> list[TraceEvent]:
        if not self._state.events_path.exists():
            return []
        return load_events(self._state.events_path.read_text(encoding="utf-8"))

    def verify(self) -> IntegrityReport:
        """Re-verify the on-disk chain. Used by tests and finalize()."""
        events = self.read_events()
        return verify_chain(events)

    @property
    def trace_id(self) -> str:
        return self._state.trace_id

    @property
    def branch_id(self) -> str:
        return self._state.branch_id

    @property
    def root(self) -> Path:
        return self._state.root

    @property
    def events_path(self) -> Path:
        return self._state.events_path

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _build_event(
        self,
        *,
        event_type: EventType,
        payload: dict,
        parent_event_id: str | None,
        caused_by_event_id: str | None,
        plan_id: str | None,
        case_id: str | None,
        step_id: str | None,
        call_id: str | None,
        sequence_no: int | None = None,
        event_id: str | None = None,
        recorded_at: str | None = None,
        previous_hash: str | None = None,
    ) -> TraceEvent:
        seq = sequence_no or self._state.next_sequence
        eid = event_id or generate_event_id()
        prev = previous_hash if previous_hash is not None else self._state.last_event_hash
        ev = TraceEvent(
            schema_version=SCHEMA_VERSION,
            trace_id=self._state.trace_id,
            branch_id=self._state.branch_id,
            event_id=eid,
            sequence_no=seq,
            parent_event_id=parent_event_id,
            caused_by_event_id=caused_by_event_id,
            event_type=event_type,
            recorded_at=recorded_at or _utcnow_iso(),
            plan_id=plan_id,
            case_id=case_id,
            step_id=step_id,
            call_id=call_id,
            payload=payload,
            previous_hash=prev,
            event_hash="",
        )
        # Compute hash and stamp.
        h = canonical_event_hash(ev)
        ev = dataclasses.replace(ev, event_hash=h)
        return ev

    def _append_raw_event(self, ev: TraceEvent) -> None:
        """Append one event line to events.jsonl with fsync.

        The line is a single canonical-bytes JSON object followed
        by a newline character; no in-line indentation (per JSONL).
        """
        line = canonical_bytes(ev.to_dict()) + b"\n"
        with self._state.events_path.open("ab") as fh:
            fh.write(line)
            fh.flush()
            os.fsync(fh.fileno())

        self._state.last_event_hash = ev.event_hash
        self._state.next_sequence = ev.sequence_no + 1


def _utcnow_iso() -> str:
    import datetime as _dt
    return _dt.datetime.now(tz=_dt.timezone.utc).isoformat(timespec="milliseconds")


__all__ = [
    "TraceStore",
    "OpenResult",
    "TraceError",
    "ChainCorrupted",
    "AlreadyFinalized",
]
