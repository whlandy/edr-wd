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

    def to_dict(self) -> dict[str, object]:
        return {
            "sequence_no": self.sequence_no,
            "event_id": self.event_id,
            "issue": self.issue,
            "code": self.code,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "IntegrityIssue":
        return cls(
            sequence_no=int(data["sequence_no"]),
            event_id=str(data["event_id"]),
            issue=str(data["issue"]),
            code=str(data["code"]),
        )


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
            "issues": [i.to_dict() for i in self.issues],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "IntegrityReport":
        return cls(
            ok=bool(data.get("ok", False)),
            issues=tuple(
                IntegrityIssue.from_dict(iss)  # type: ignore[arg-type]
                for iss in data.get("issues", []) or []
            ),
        )


def canonical_event_hash(event: TraceEvent) -> str:
    """Return ``sha256:<hex>`` for the event WITHOUT its `event_hash`.

    Per FR-P1.3-02: the hash is computed over canonical bytes of
    the event with `event_hash` excluded.
    """
    return canonical_sha256(event.to_hashable_dict())


def stamp_event(event: TraceEvent) -> TraceEvent:
    """Return a new event with `event_hash` populated.

    Used by callers (test fixtures, the store) that construct
    events without a hash and want them chained. Equivalently
    `dataclasses.replace(event, event_hash=canonical_event_hash(event))`
    but with shorter call sites.
    """
    import dataclasses
    return dataclasses.replace(event, event_hash=canonical_event_hash(event))


def verify_chain(events: Sequence[TraceEvent]) -> IntegrityReport:
    """Walk the chain, reporting every mismatch.

    Rules enforced:

      1. `event_hash` matches `canonical_event_hash(event)` (FR-P1.3-02).
      2. First event has `previous_hash = None`; subsequent events
         have `previous_hash == prior.event_hash` (FR-P1.3-04, -05).
      3. `sequence_no` is monotonic starting from 1 (defensive).
    """
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