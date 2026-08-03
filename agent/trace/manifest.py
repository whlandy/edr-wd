"""
manifest.py — Case-attempt manifest (architecture §12.3, FR-P1.3-09).

`manifest.json` records the trace-level facts that survive even
after the event chain is closed:

  * `trace_id`, `branch_heads`
  * `catalog_digest` (must match the catalog the executor ran
    against; cross-checked against the in-process catalog at
    report-finalize time).
  * `evidence_counts` (per-kind tally: screenshots, expectations,
    actions, etc.).
  * `terminal_status` (one of `passed`, `failed`, `blocked`,
    `skipped`, `aborted`).
  * `cleanup_status` (one of `passed`, `failed`, `unknown`; default
    `unknown` for backward compat with P2.3-era manifests).
  * `cleanup_outcome_critical` (bool; default `False` for backward
    compat — P2.3 manifests did not propagate this flag, so renderer
    defaults to non-critical = no cascade).
  * `integrity_verification_result` (the IntegrityReport summary).

P1.3 ships the schema; consumers can ignore unknown fields.
P2.4 extends the schema with cleanup fields (additive, backward
compatible — SCHEMA_VERSION stays "1.0.0" per D16).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from .integrity import IntegrityReport


SCHEMA_VERSION = "1.0.0"

# Cleanup status values (P2.4 D11 / D12).
CLEANUP_STATUS_PASSED = "passed"
CLEANUP_STATUS_FAILED = "failed"
CLEANUP_STATUS_UNKNOWN = "unknown"
_CLEANUP_STATUSES: frozenset[str] = frozenset({
    CLEANUP_STATUS_PASSED,
    CLEANUP_STATUS_FAILED,
    CLEANUP_STATUS_UNKNOWN,
})


@dataclass(frozen=True)
class ManifestRecord:
    trace_id: str
    branch_heads: tuple[str, ...]
    catalog_digest: str
    evidence_counts: Mapping[str, int]
    terminal_status: str
    integrity_verification_result: IntegrityReport

    # NEW (P2.4.A) — additive fields. Defaults preserve backward
    # compat with P2.3 manifests (which had no cleanup_* fields).
    cleanup_status: str = CLEANUP_STATUS_UNKNOWN
    cleanup_outcome_critical: bool = False

    def __post_init__(self) -> None:
        if self.cleanup_status not in _CLEANUP_STATUSES:
            raise ValueError(
                f"cleanup_status must be one of {sorted(_CLEANUP_STATUSES)}, "
                f"got {self.cleanup_status!r}"
            )
        if not isinstance(self.cleanup_outcome_critical, bool):
            raise TypeError(
                f"cleanup_outcome_critical must be a bool, "
                f"got {type(self.cleanup_outcome_critical).__name__}"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "trace_id": self.trace_id,
            "branch_heads": list(self.branch_heads),
            "catalog_digest": self.catalog_digest,
            "evidence_counts": dict(self.evidence_counts),
            "terminal_status": self.terminal_status,
            "cleanup_status": self.cleanup_status,
            "cleanup_outcome_critical": self.cleanup_outcome_critical,
            "integrity_verification_result": self.integrity_verification_result.to_dict(),
        }


def from_dict(data: Mapping[str, Any]) -> ManifestRecord:
    """Reconstruct a ManifestRecord from a JSON-loaded dict.

    Backward compat (D13): missing `cleanup_status` → "unknown";
    missing `cleanup_outcome_critical` → False. P2.3-era manifests
    render unchanged (no cascade).
    """
    inv = data.get("integrity_verification_result") or {"ok": True, "issues": []}
    raw_cleanup = data.get("cleanup_status", CLEANUP_STATUS_UNKNOWN)
    raw_critical = data.get("cleanup_outcome_critical", False)
    return ManifestRecord(
        trace_id=data["trace_id"],
        branch_heads=tuple(data.get("branch_heads") or ()),
        catalog_digest=data["catalog_digest"],
        evidence_counts=dict(data.get("evidence_counts") or {}),
        terminal_status=data["terminal_status"],
        integrity_verification_result=IntegrityReport.from_dict(inv),
        cleanup_status=raw_cleanup,
        cleanup_outcome_critical=bool(raw_critical),
    )


__all__ = [
    "SCHEMA_VERSION",
    "ManifestRecord",
    "from_dict",
    "CLEANUP_STATUS_PASSED",
    "CLEANUP_STATUS_FAILED",
    "CLEANUP_STATUS_UNKNOWN",
]