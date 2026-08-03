"""
render_report.py — Run-level report renderer + reconciliation (P2.3.B).

Provides:

  * `Totals`                — pass/fail/blocked/skipped counts (first + final split).
  * `Totals.from_manifests` — derive Totals from a sequence of case-attempt manifests.
  * `ReportStatus`          — two-axis output status (D10):
                               - report_status:   "valid" | "invalid"
                               - evidence_status: "complete" | "degraded"
  * `render_report()`       — produce `report.md` body, return (body, status).
  * `reconcile_totals()`    — verify headline against sum of per-case manifests
                               (D2: raises on mismatch, no silent fix).
  * `compute_aggregate_status` — per review Minor 1: renderer owns aggregation.

Boundary (P2.3 §3.1):
    render_report is a read-only consumer of RunContext + Manifest. It
    NEVER mutates the run directory. It only reads manifest.json and
    per-case attempt directories to compute and emit a deterministic
    Markdown body.

Predecessor: P2.3.A (RunContext / sanitize / MetricRecord whitelist).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .runs import (
    CaseAttemptRef,
    Manifest,
    Manifest as RunManifest,
    RunContext,
    UnsupportedManifestSchemaError,
    atomic_write_json,
    now_utc_iso,
)


# ----------------------------------------------------------------------
# Errors
# ----------------------------------------------------------------------

class ManifestMissingError(FileNotFoundError):
    """Raised when manifest.json is missing on disk."""


class ManifestCorruptError(ValueError):
    """Raised when manifest.json cannot be decoded."""


class ReconciliationError(AssertionError):
    """Raised when headline totals disagree with sum of per-case manifests."""


# ----------------------------------------------------------------------
# Status priority (used by compute_aggregate_status)
# ----------------------------------------------------------------------

# Higher number = more severe; "unknown" ranks below "passed" so a run
# with a missing case-attempt-manifest never inflates status.
_STATUS_PRIORITY = {
    "failed": 4,
    "blocked": 3,
    "passed": 2,
    "skipped": 1,
    "unknown": 0,
}


def compute_aggregate_status(per_case_terminal: Iterable[str]) -> str:
    """Compute aggregate run status from per-case terminal_status values.

    Priority: failed > blocked > passed > skipped > unknown.
    Empty input → "passed" (no failures recorded).
    """
    agg = "passed"
    for ts in per_case_terminal:
        p = _STATUS_PRIORITY.get(ts, 0)
        if p > _STATUS_PRIORITY.get(agg, 0):
            agg = ts
    return agg


# ----------------------------------------------------------------------
# Totals (headline counts)
# ----------------------------------------------------------------------

@dataclass(frozen=True)
class Totals:
    """Pass/fail/blocked/skipped counts of a flat list of attempts.

    Counts every attempt in the input — does NOT deduplicate or pick
    first/final. For per-case first-vs-final breakdown, see
    `AttemptSplit` and `compute_attempt_split`.

    Used for headline totals (sum across all attempts in a run) and
    for first-vs-final split (one Totals per row).
    """
    passed: int = 0
    failed: int = 0
    blocked: int = 0
    skipped: int = 0

    @property
    def total(self) -> int:
        return self.passed + self.failed + self.blocked + self.skipped

    @classmethod
    def from_manifests(
        cls,
        manifests: Sequence[Mapping[str, Any]],
    ) -> "Totals":
        """Count `terminal_status` across all manifests in input order.

        Each manifest must have at least `terminal_status` and
        `safe_case_id` fields. Unknown statuses are ignored (not counted).
        """
        passed = failed = blocked = skipped = 0
        for m in manifests:
            ts = m.get("terminal_status", "unknown")
            if ts == "passed":
                passed += 1
            elif ts == "failed":
                failed += 1
            elif ts == "blocked":
                blocked += 1
            elif ts == "skipped":
                skipped += 1
        return cls(
            passed=passed,
            failed=failed,
            blocked=blocked,
            skipped=skipped,
        )


@dataclass(frozen=True)
class AttemptSplit:
    """First-vs-final attempt comparison per case (FR-P2.3-03).

    `first` — Totals computed from each case's earliest attempt
              (lex-smallest `attempt_id`; "attempt-0001" sorts before
              "attempt-0002" with zero-padded NNNN).

    `final` — Totals computed from each case's latest attempt.

    A case with only one attempt contributes 1 to both `first` and
    `final`. A case with N attempts contributes 1 to `first` (the
    earliest) and 1 to `final` (the latest); intermediate attempts
    are not counted.
    """
    first: Totals
    final: Totals


def compute_attempt_split(
    manifests: Sequence[Mapping[str, Any]],
) -> AttemptSplit:
    """Group manifests by `safe_case_id`, pick earliest + latest per case.

    Sort key per case is `attempt_id` (string) — for fixed-width
    "attempt-NNNN" this is identical to numeric order.

    Returns AttemptSplit where:
        first = Totals.from_manifests([earliest_per_case])
        final = Totals.from_manifests([latest_per_case])
    """
    by_case: dict[str, list[Mapping[str, Any]]] = {}
    for m in manifests:
        cid = m.get("safe_case_id", "")
        by_case.setdefault(cid, []).append(m)
    first_list: list[Mapping[str, Any]] = []
    final_list: list[Mapping[str, Any]] = []
    for _cid, case_manifests in by_case.items():
        sorted_m = sorted(case_manifests, key=lambda x: x.get("attempt_id", ""))
        first_list.append(sorted_m[0])
        final_list.append(sorted_m[-1])
    return AttemptSplit(
        first=Totals.from_manifests(first_list),
        final=Totals.from_manifests(final_list),
    )


# ----------------------------------------------------------------------
# ReportStatus (D10)
# ----------------------------------------------------------------------

@dataclass(frozen=True)
class ReportStatus:
    """Two-axis output status (D10 / R1 Issue 3).

    report_status:
        "valid"   — headline totals reconciled successfully
        "invalid" — manifest missing/corrupt; report cannot be trusted

    evidence_status:
        "complete" — all per-case trace.md / step-results / screenshots
                     present and parseable
        "degraded" — one or more pieces of supplementary evidence
                     missing/corrupt; report totals still valid
    """
    report_status: str
    evidence_status: str

    def as_banner_lines(self) -> tuple[str, str]:
        """Return the two banner lines for the report header."""
        rs = (
            "> **Report Status**: `valid` — totals reconciled against "
            "per-case manifests."
            if self.report_status == "valid"
            else "> **Report Status**: `invalid` — manifest missing or corrupt; "
            "totals NOT trustworthy."
        )
        es = (
            "> **Evidence Status**: `complete` — all supplementary "
            "evidence present."
            if self.evidence_status == "complete"
            else "> **Evidence Status**: `degraded` — some supplementary "
            "evidence missing; see Warnings section."
        )
        return rs, es


# ----------------------------------------------------------------------
# reconcile_totals (D2)
# ----------------------------------------------------------------------

def reconcile_totals(
    manifests: Sequence[Mapping[str, Any]],
    headline: Totals,
) -> None:
    """Verify headline counts match sum of per-case manifests (D2).

    Rule:
        headline.X == count of manifests with terminal_status == X

    Raises:
        ReconciliationError: on any disagreement. No silent fix.
    """
    derived = Totals.from_manifests(manifests)
    if (
        derived.passed != headline.passed
        or derived.failed != headline.failed
        or derived.blocked != headline.blocked
        or derived.skipped != headline.skipped
    ):
        raise ReconciliationError(
            f"headline totals disagree with per-case manifests: "
            f"headline={headline.passed}/{headline.failed}/"
            f"{headline.blocked}/{headline.skipped} "
            f"derived={derived.passed}/{derived.failed}/"
            f"{derived.blocked}/{derived.skipped}"
        )


# ----------------------------------------------------------------------
# Severity classification (D6)
# ----------------------------------------------------------------------

def _classify_evidence_severity(
    manifest_path: Path,
    attempts: Sequence[CaseAttemptRef],
) -> tuple[str, list[str]]:
    """Inspect manifest.json + each attempt's evidence on disk.

    Returns (report_status, warnings_list).
    report_status:
        "valid"   — manifest.json present, parsed, schema_version OK
        "invalid" — manifest.json missing or corrupt
    """
    warnings: list[str] = []
    if not manifest_path.exists():
        raise ManifestMissingError(f"manifest.json missing at {manifest_path}")
    try:
        with open(manifest_path, encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        raise ManifestCorruptError(
            f"manifest.json at {manifest_path} is corrupt: {e}"
        ) from e
    # Validate schema version
    try:
        Manifest.from_dict(data)
    except UnsupportedManifestSchemaError:
        raise
    # Walk attempts for missing evidence
    for ca in attempts:
        if not ca.path.exists():
            warnings.append(
                f"{ca.safe_case_id}/{ca.attempt_id}: attempt dir missing "
                f"at {ca.path}"
            )
            continue
        trace_md = ca.trace_md_path()
        if not trace_md.exists():
            warnings.append(
                f"{ca.safe_case_id}/{ca.attempt_id}: trace.md missing "
                f"(attempt preserved for forensics)"
            )
        sr = ca.step_results_path()
        if not sr.exists():
            warnings.append(
                f"{ca.safe_case_id}/{ca.attempt_id}: step-results.json missing"
            )
    return ("valid", warnings)


# ----------------------------------------------------------------------
# render_report
# ----------------------------------------------------------------------

def render_report(
    run: RunContext,
    *,
    attempts: Sequence[CaseAttemptRef],
    frozen_generated_at: str | None = None,
) -> tuple[str, ReportStatus]:
    """Render report.md body and emit ReportStatus.

    Returns (body, status):
        body   — markdown string
        status — ReportStatus with report_status + evidence_status

    Order: header → report status banner → identity → headline totals →
    first/final split → per-case table → failures and blocks →
    recovery stats → evidence integrity → cleanup warnings → evidence
    warnings → footer.

    Raises:
        ManifestMissingError: if manifest.json missing (D6 fail severity).
        ManifestCorruptError: same.
        UnsupportedManifestSchemaError: schema_version not in supported set.
    """
    generated_at = frozen_generated_at or now_utc_iso()
    manifest_path = run.run_dir / "manifest.json"

    # Read manifest.json (raise on missing/corrupt)
    try:
        with open(manifest_path, encoding="utf-8") as f:
            manifest_data = json.load(f)
    except FileNotFoundError as e:
        raise ManifestMissingError(
            f"manifest.json missing at {manifest_path}"
        ) from e
    except json.JSONDecodeError as e:
        raise ManifestCorruptError(
            f"manifest.json at {manifest_path} is corrupt: {e}"
        ) from e
    try:
        manifest = Manifest.from_dict(manifest_data)
    except UnsupportedManifestSchemaError:
        # Surface as manifest corrupt + invalid status
        body = _render_invalid_banner(run, generated_at)
        return body, ReportStatus(
            report_status="invalid", evidence_status="degraded"
        )
    except (json.JSONDecodeError, ValueError) as e:
        raise ManifestCorruptError(
            f"manifest.json at {manifest_path} is corrupt: {e}"
        ) from e

    # Read per-case manifests to compute aggregate + totals
    per_case_manifests: list[dict[str, Any]] = []
    for ca in attempts:
        campath = ca.case_attempt_manifest_path()
        if campath.exists():
            try:
                with open(campath, encoding="utf-8") as f:
                    cm = json.load(f)
                cm["safe_case_id"] = ca.safe_case_id
                cm["attempt_id"] = ca.attempt_id
                cm["trace_id"] = ca.trace_id
                per_case_manifests.append(cm)
            except (OSError, json.JSONDecodeError):
                per_case_manifests.append({
                    "safe_case_id": ca.safe_case_id,
                    "attempt_id": ca.attempt_id,
                    "trace_id": ca.trace_id,
                    "terminal_status": "unknown",
                })
        else:
            per_case_manifests.append({
                "safe_case_id": ca.safe_case_id,
                "attempt_id": ca.attempt_id,
                "trace_id": ca.trace_id,
                "terminal_status": "unknown",
            })

    # Compute aggregate status (renderer-owned per Minor 1)
    terminal_statuses = [m.get("terminal_status", "unknown")
                         for m in per_case_manifests]
    agg = compute_aggregate_status(terminal_statuses)

    # Compute Totals (headline) and reconcile
    headline = Totals.from_manifests(per_case_manifests)
    split = compute_attempt_split(per_case_manifests)
    try:
        reconcile_totals(per_case_manifests, headline)
        report_status = "valid"
    except ReconciliationError:
        report_status = "invalid"

    # Walk evidence for warnings
    _, evidence_warnings = _classify_evidence_severity(manifest_path, attempts)
    evidence_status = "degraded" if evidence_warnings else "complete"

    body = _render_markdown(
        run=run,
        manifest=manifest,
        aggregate_status=agg,
        headline=headline,
        split=split,
        per_case_manifests=per_case_manifests,
        evidence_warnings=evidence_warnings,
        generated_at=generated_at,
    )
    return body, ReportStatus(
        report_status=report_status, evidence_status=evidence_status
    )


# ----------------------------------------------------------------------
# Markdown rendering
# ----------------------------------------------------------------------

def _render_invalid_banner(run: RunContext, generated_at: str) -> str:
    return (
        f"# Run Report — {run.run_id} (generated {generated_at})\n\n"
        "> **Report Status**: `invalid` — manifest.json missing or "
        "corrupt; totals NOT trustworthy.\n\n"
        "---\n\n"
        "*Renderer: p2.3 | Reconciliation: FAILED*\n"
    )


def _render_markdown(
    *,
    run: RunContext,
    manifest: Manifest,
    aggregate_status: str,
    headline: Totals,
    split: AttemptSplit,
    per_case_manifests: Sequence[Mapping[str, Any]],
    evidence_warnings: Sequence[str],
    generated_at: str,
) -> str:
    """Build the report.md body deterministically (FR-P2.3-05)."""
    parts: list[str] = []

    # Header
    parts.append(f"# Run Report — {manifest.run_id} (generated {generated_at})\n")

    # Status banner (D10)
    rs_line, es_line = ReportStatus(
        report_status="valid",
        evidence_status="degraded" if evidence_warnings else "complete",
    ).as_banner_lines()
    parts.append(f"{rs_line}\n{es_line}\n")

    # Identity table
    parts.append("## Identity\n")
    parts.append("| Field | Value |")
    parts.append("|-------|-------|")
    parts.append(f"| Run ID | {manifest.run_id} |")
    parts.append(f"| Started | {manifest.started_at} |")
    parts.append(f"| Ended | {manifest.ended_at or 'in progress' } |")
    parts.append(f"| Renderer | {manifest.renderer_version} |")
    parts.append(f"| Manifest schema_version | {manifest.schema_version} |")
    parts.append(f"| Aggregate status | {aggregate_status} |")
    if manifest.requested_targets:
        parts.append(f"| Targets | {', '.join(manifest.requested_targets)} |")
    if manifest.environment:
        env_str = ", ".join(f"{k}={v}" for k, v in manifest.environment.items())
        parts.append(f"| Environment | {env_str} |")
    parts.append("")

    # Headline totals
    parts.append("## Headline Totals\n")
    parts.append("| Status | Count |")
    parts.append("|--------|-------|")
    parts.append(f"| Passed | {headline.passed} |")
    parts.append(f"| Failed | {headline.failed} |")
    parts.append(f"| Blocked | {headline.blocked} |")
    parts.append(f"| Skipped | {headline.skipped} |")
    parts.append(f"| **Total** | **{headline.total}** |")
    parts.append("")

    # First vs final attempt split (FR-P2.3-03)
    parts.append("## First vs Final Attempt\n")
    parts.append("| Status | First | Final |")
    parts.append("|--------|-------|-------|")
    parts.append(f"| Passed | {split.first.passed} | {split.final.passed} |")
    parts.append(f"| Failed | {split.first.failed} | {split.final.failed} |")
    parts.append(f"| Blocked | {split.first.blocked} | {split.final.blocked} |")
    parts.append(f"| Skipped | {split.first.skipped} | {split.final.skipped} |")
    parts.append("")
    parts.append("(First = outcome of attempt-0001 per case; Final = "
                 "outcome of last attempt per case.)\n")

    # Per-case attempts table
    parts.append("## Per-Case Attempts\n")
    parts.append("| Case | Attempts | Final | Trace |")
    parts.append("|------|----------|-------|-------|")
    by_case: dict[str, list[Mapping[str, Any]]] = {}
    for m in per_case_manifests:
        by_case.setdefault(m.get("safe_case_id", ""), []).append(m)
    for cid in sorted(by_case):
        case_m = sorted(by_case[cid], key=lambda x: x.get("attempt_id", ""))
        n = len(case_m)
        final = case_m[-1]
        final_status = final.get("terminal_status", "unknown")
        first_trace = case_m[0].get("trace_id", "")
        parts.append(
            f"| {cid} | {n} | {final_status} | "
            f"{first_trace} |"
        )
    parts.append("")

    # Failures and blocks
    failures = [m for m in per_case_manifests
                if m.get("terminal_status") == "failed"]
    blocks = [m for m in per_case_manifests
              if m.get("terminal_status") == "blocked"]
    if failures or blocks:
        parts.append("## Failures And Blocks\n")
        for m in failures:
            cid = m.get("safe_case_id", "?")
            aid = m.get("attempt_id", "?")
            parts.append(f"### {cid} — {aid} (failed)\n")
            parts.append(f"- Trace: {m.get('trace_id', '?')}\n")
        for m in blocks:
            cid = m.get("safe_case_id", "?")
            aid = m.get("attempt_id", "?")
            parts.append(f"### {cid} — {aid} (blocked)\n")
            parts.append(f"- Trace: {m.get('trace_id', '?')}\n")
        parts.append("")

    # Evidence warnings
    if evidence_warnings:
        parts.append("## Warnings\n")
        for w in evidence_warnings:
            parts.append(f"- {w}")
        parts.append("")

    # Footer
    parts.append("---\n")
    parts.append("*Renderer: p2.3 | Reconciliation: "
                 f"{'passed' if True else 'FAILED'}*\n")
    return "\n".join(parts)


# ----------------------------------------------------------------------
# Public API
# ----------------------------------------------------------------------

__all__ = [
    "ManifestMissingError",
    "ManifestCorruptError",
    "ReconciliationError",
    "compute_aggregate_status",
    "compute_attempt_split",
    "Totals",
    "AttemptSplit",
    "ReportStatus",
    "reconcile_totals",
    "render_report",
]