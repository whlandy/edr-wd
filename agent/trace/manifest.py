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
  * `integrity_verification_result` (the IntegrityReport summary).

P1.3 ships the schema; consumers can ignore unknown fields.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from .integrity import IntegrityReport


SCHEMA_VERSION = "1.0.0"


@dataclass(frozen=True)
class ManifestRecord:
    trace_id: str
    branch_heads: tuple[str, ...]
    catalog_digest: str
    evidence_counts: Mapping[str, int]
    terminal_status: str
    integrity_verification_result: IntegrityReport

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "trace_id": self.trace_id,
            "branch_heads": list(self.branch_heads),
            "catalog_digest": self.catalog_digest,
            "evidence_counts": dict(self.evidence_counts),
            "terminal_status": self.terminal_status,
            "integrity_verification_result": self.integrity_verification_result.to_dict(),
        }


def from_dict(data: Mapping[str, Any]) -> ManifestRecord:
    """Reconstruct a ManifestRecord from a JSON-loaded dict."""
    inv = data.get("integrity_verification_result") or {"ok": True, "issues": []}
    return ManifestRecord(
        trace_id=data["trace_id"],
        branch_heads=tuple(data.get("branch_heads") or ()),
        catalog_digest=data["catalog_digest"],
        evidence_counts=dict(data.get("evidence_counts") or {}),
        terminal_status=data["terminal_status"],
        integrity_verification_result=IntegrityReport.from_dict(inv),
    )


__all__ = ["SCHEMA_VERSION", "ManifestRecord", "from_dict"]