"""P1.3 acceptance gate — manifest schema (FR-P1.3-09).

`manifest.json` records the trace-level facts that survive after
the chain is closed:

  * `trace_id`, `branch_heads`
  * `catalog_digest` (must match the catalog the executor ran
    against; cross-checked against the in-process catalog at
    report-finalize time)
  * `evidence_counts` (per-kind tally)
  * `terminal_status`
  * `integrity_verification_result`

This test pins the schema; deeper report-time use is P2.3.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


_REPO = Path(__file__).resolve().parents[2]
for p in (str(_REPO / "target"), str(_REPO / "agent")):
    if p not in sys.path:
        sys.path.insert(0, p)


from trace import IntegrityIssue, IntegrityReport, ManifestRecord  # noqa: E402,F401


def test_manifest_required_fields():
    """All fields required by FR-P1.3-09 are present after dict round-trip."""
    m = ManifestRecord(
        trace_id="TR-1",
        branch_heads=("EVT-1",),
        catalog_digest="sha256:abc123",
        evidence_counts={"screenshot": 3, "expectation": 9},
        terminal_status="passed",
        integrity_verification_result=IntegrityReport.ok_report(),
    )
    d = m.to_dict()
    for key in ("schema_version", "trace_id", "branch_heads",
                "catalog_digest", "evidence_counts", "terminal_status",
                "integrity_verification_result"):
        assert key in d, key


def test_manifest_carries_catalog_digest():
    """FR-P1.3-09: catalog_digest field present."""
    m = ManifestRecord(
        trace_id="TR-1",
        branch_heads=(),
        catalog_digest="sha256:0ecbda1584b1baa0cf8ada2c38fc6c2037b7f2f8334c34b33f682a0ae9e28d07",
        evidence_counts={},
        terminal_status="passed",
        integrity_verification_result=IntegrityReport.ok_report(),
    )
    assert "0ecbda1584b1" in m.to_dict()["catalog_digest"]


def test_manifest_terminal_status_values():
    """Terminal status uses the same vocabulary as case outcome."""
    for status in ("passed", "failed", "blocked", "skipped", "aborted"):
        m = ManifestRecord(
            trace_id="TR-1",
            branch_heads=(),
            catalog_digest="sha256:abc",
            evidence_counts={},
            terminal_status=status,
            integrity_verification_result=IntegrityReport.ok_report(),
        )
        assert m.to_dict()["terminal_status"] == status


def test_manifest_branch_heads_is_tuple():
    """branch_heads is a tuple (immutable) so the dataclass stays hashable."""
    m = ManifestRecord(
        trace_id="TR-1",
        branch_heads=("EVT-a", "EVT-b"),
        catalog_digest="sha256:abc",
        evidence_counts={},
        terminal_status="passed",
        integrity_verification_result=IntegrityReport.ok_report(),
    )
    d = m.to_dict()
    assert isinstance(d["branch_heads"], list)
    assert d["branch_heads"] == ["EVT-a", "EVT-b"]


def test_manifest_integrity_issues_propagate():
    """A non-empty integrity report surfaces in the manifest."""
    report = IntegrityReport(
        ok=False,
        issues=(IntegrityIssue(
            sequence_no=2, event_id="EVT-x",
            issue="event_hash mismatch",
            code="bad_hash",
        ),),
    )
    m = ManifestRecord(
        trace_id="TR-1",
        branch_heads=("EVT-1",),
        catalog_digest="sha256:abc",
        evidence_counts={},
        terminal_status="failed",
        integrity_verification_result=report,
    )
    d = m.to_dict()
    assert d["integrity_verification_result"]["ok"] is False
    assert len(d["integrity_verification_result"]["issues"]) == 1
    assert d["integrity_verification_result"]["issues"][0]["code"] == "bad_hash"


def test_manifest_round_trip():
    """P1.3 review #1 Blocker 2: ManifestRecord must survive
    to_dict -> from_dict byte-identically."""
    from trace import IntegrityReport, IntegrityIssue
    from trace.manifest import from_dict as manifest_from_dict
    original = ManifestRecord(
        trace_id="TR-rt",
        branch_heads=("EVT-a", "EVT-b", "EVT-c"),
        catalog_digest="sha256:abc",
        evidence_counts={"screenshot": 3, "expectation": 9, "action": 5},
        terminal_status="failed",
        integrity_verification_result=IntegrityReport(
            ok=False,
            issues=(
                IntegrityIssue(
                    sequence_no=2, event_id="EVT-x",
                    issue="event_hash mismatch: stored=sha256:a, recomputed=sha256:b",
                    code="bad_hash",
                ),
                IntegrityIssue(
                    sequence_no=3, event_id="EVT-y",
                    issue="sequence_no=3, expected 4",
                    code="bad_sequence",
                ),
            ),
        ),
    )
    rebuilt = manifest_from_dict(original.to_dict())
    assert rebuilt == original


def test_integrity_report_round_trip():
    """IntegrityReport round-trips independently."""
    from trace import IntegrityReport, IntegrityIssue
    original = IntegrityReport(
        ok=False,
        issues=(IntegrityIssue(
            sequence_no=5, event_id="EVT-5",
            issue="previous_hash mismatch",
            code="bad_previous_hash",
        ),),
    )
    rebuilt = IntegrityReport.from_dict(original.to_dict())
    assert rebuilt == original


def test_integrity_issue_round_trip():
    original = IntegrityIssue(
        sequence_no=10, event_id="EVT-10",
        issue="test issue", code="bad_hash",
    )
    rebuilt = IntegrityIssue.from_dict(original.to_dict())
    assert rebuilt == original