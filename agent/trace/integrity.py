"""
integrity.py — Hash chain + tamper detection (FR-P1.3-02..05, -07).

The chain hash is `sha256(canonical_bytes(event))` where `event` is
the TraceEvent WITHOUT its `event_hash` field. The first event in
the chain has `previous_hash = None`; subsequent events use the
prior event's `event_hash`.

Verification walks the chain and reports every mismatch it finds
in an `IntegrityReport` rather than failing on the first one —
that way a corrupted trace can be diagnosed, not just rejected.

This module is pure (no I/O); it operates on in-memory event
sequences. The store does the I/O.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from protocol_models.canonical_json import canonical_bytes, canonical_sha256

from .events import TraceEvent, from_dict


@dataclass(frozen=True)
class IntegrityIssue:
    sequence_no: int
    event_id: str
    issue: str  # human-readable
    code: str   # stable: bad_hash | bad_previous_hash | bad_sequence


@dataclass(frozen=True)
class IntegrityReport:
    ok: bool
    issues: tuple[IntegrityIssue, ...] = ()

    @classmethod
    def ok_report(cls) -> "IntegrityReport":
        return cls(ok=True, issues=())

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "issues": [
                {
                    "sequence_no": i.sequence_no,
                    "event_id": i.event_id,
                    "issue": i.issue,
                    "code": i.code,
                }
                for i in self.issues
            ],
        }


def canonical_event_hash(event: TraceEvent) -> str:
    """Return ``sha256:<hex>`` for the event WITHOUT its `event_hash`.

    Per FR-P1.3-02: the hash is computed over canonical bytes of
    the event with `event_hash` excluded.
    """
    return canonical_sha256(event.to_hashable_dict())


def verify_chain(events: Sequence[TraceEvent]) -> IntegrityReport:
    """Walk the chain, reporting every mismatch."""
    issues: list[IntegrityIssue] = []

    if not events:
        return IntegrityReport.ok_report()

    expected_seq = 1
    for i, ev in enumerate(events):
        if ev.sequence_no != expected_seq:
            issues.append(IntegrityIssue(
                sequence_no=ev.sequence_no,
                event_id=ev.event_id,
                issue=f"sequence_no={ev.sequence_no}, expected {expected_seq}",
                code="bad_sequence",
            ))

        recomputed = canonical_event_hash(ev)
        if recomputed != ev.event_hash:
            issues.append(IntegrityIssue(
                sequence_no=ev.sequence_no,
                event_id=ev.event_id,
                issue=f"event_hash mismatch: stored={ev.event_hash!r}, recomputed={recomputed!r}",
                code="bad_hash",
            ))

        if i == 0:
            if ev.previous_hash is not None:
                issues.append(IntegrityIssue(
                    sequence_no=ev.sequence_no,
                    event_id=ev.event_id,
                    issue="first event must have previous_hash=None",
                    code="bad_previous_hash",
                ))
        else:
            prior = events[i - 1]
            if ev.previous_hash != prior.event_hash:
                issues.append(IntegrityIssue(
                    sequence_no=ev.sequence_no,
                    event_id=ev.event_id,
                    issue=(
                        f"previous_hash={ev.previous_hash!r} != "
                        f"prior.event_hash={prior.event_hash!r}"
                    ),
                    code="bad_previous_hash",
                ))

        expected_seq += 1

    return IntegrityReport(ok=not issues, issues=tuple(issues))


def load_events(jsonl_text: str) -> list[TraceEvent]:
    """Parse a JSONL text into a list of `TraceEvent`.

    Each non-empty line MUST be a valid `TraceEvent` dict; any
    malformed line raises ValueError. Used by the store on open.
    """
    events: list[TraceEvent] = []
    for lineno, raw in enumerate(jsonl_text.splitlines(), start=1):
        if not raw.strip():
            continue
        import json
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"events.jsonl line {lineno} is not valid JSON: {exc}"
            ) from exc
        if not isinstance(data, dict):
            raise ValueError(
                f"events.jsonl line {lineno} is not a JSON object"
            )
        events.append(from_dict(data))
    return events


__all__ = [
    "IntegrityIssue",
    "IntegrityReport",
    "canonical_event_hash",
    "verify_chain",
    "load_events",
]
