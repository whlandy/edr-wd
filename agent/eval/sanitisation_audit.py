"""
sanitisation_audit.py — P3.2.B sensitive-content audit layer.

This module implements the **audit** side of the D18 / D21
sanitisation policy. It is intentionally SEPARATE from
`reports.py`:

    * `reports.py` owns the structural schema. It does NOT
      invoke any pattern-matching heuristic.
    * `sanitisation_audit.py` owns the audit. It walks
      ONLY scoped (untrusted) fields and returns findings.
    * The audit NEVER raises. The caller decides whether
      to reject the report based on findings.

Scope discipline (the key correctness property):

    The audit walks only fields that may legitimately
    contain user-observed content. These are configured
    via `DEFAULT_SCOPED_PATHS` and are matched as
    TOP-LEVEL or NESTED path segments, not as substring
    matches against field names.

    Fields OUTSIDE the scope (e.g. `metric_id`,
    `dataset_id`, `unit`, `direction`, `decision`,
    `fixture_id`, `run_id`) are NEVER scanned. A
    `metric_id = "system_latency"` MUST NOT trigger a
    false-positive prompt finding; a 64-char hex string
    in `dataset_id` MUST NOT trigger a false-positive
    token finding.

Public API:

    * `SensitiveFinding` — single audit finding.
    * `SensitiveAuditResult` — collection of findings.
    * `audit_report_sensitive_content(report_dict,
      *, scoped_paths=None) -> SensitiveAuditResult`

Pattern classes:

    * Token patterns: 40+ hex chars, 40+ base64 chars.
    * Selector patterns: DOM/XPath, querySelector.
    * Prompt patterns: "You are an AI", "system:",
      "assistant:", "<|...|>".
    * Screenshot pattern: data:image/<type>;base64,...

Severity model:

    * "block" — content that MUST NOT be present in a
      sanitised report (selectors, long hex tokens,
      screenshots, prompt leakage). A CI gate with
      default policy SHOULD reject reports with
      `block` findings.
    * "warn" — content that MAY be present but SHOULD
      be reviewed (e.g. short base64 strings).
    * "info" — informational; no action required.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, FrozenSet, Iterable, Mapping


# ---------------------------------------------------------------------------
# Path-scope discipline
# ---------------------------------------------------------------------------


DEFAULT_SCOPED_PATHS: FrozenSet[str] = frozenset(
    {
        # Per-fixture expected/observed payloads.
        "expected",
        "observed",
        # Common nested summary keys for sanitised observed.
        "summary",
        # Common nested value keys.
        "value",
        # LLM prompt carriers.
        "prompt",
        # Screenshot carriers.
        "screenshot",
        # Diff/digest carriers (long strings may appear).
        "diff",
    }
)
"""Top-level and nested path SEGMENTS that the audit
walks. A path like
`fixture_results/0/observed/summary/value`
matches because each of `observed`, `summary`, `value`
is in the scope. A path like `metrics/parse_success_rate/direction`
does NOT match because none of those segments are in the
scope (it walks but the walk terminates at the leaf
string scan).

The audit walks every leaf STRING under a scoped path
segment and checks it against the pattern catalogue. Strings
under non-scoped segments are NOT scanned.

This discipline prevents false positives on `metric_id`,
`dataset_id`, `unit`, `direction`, etc."""


# ---------------------------------------------------------------------------
# Pattern catalogue
# ---------------------------------------------------------------------------


# Patterns that, when matched, MUST block a report from
# being treated as sanitised. Conservative — false positives
# are tolerable if the audit policy is configurable.
_BLOCK_PATTERNS: tuple[re.Pattern[str], ...] = (
    # DOM / XPath selectors.
    re.compile(r"//[A-Za-z]+(\[@[A-Za-z]+=)?"),
    re.compile(r"/html/body/"),
    re.compile(r"document\.querySelector\("),
    # Confirmation tokens: long hex / base64 strings.
    re.compile(r"\b[a-f0-9]{40,}\b", re.IGNORECASE),
    # Raw screenshots (data:image/...;base64,...).
    re.compile(r"data:image/(png|jpeg|jpg|gif|bmp|webp);base64,"),
    # LLM prompt leakage markers.
    re.compile(r"You are an AI", re.IGNORECASE),
    re.compile(r"^system:\s", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^assistant:\s", re.IGNORECASE | re.MULTILINE),
    re.compile(r"<\|.*?\|>"),
)

# Patterns that, when matched, SHOULD be reviewed but are
# not blocking. E.g. short base64 strings are common in
# metric_value payloads.
_WARN_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b[A-Za-z0-9+/]{40,}={0,2}\b"),
)


# ---------------------------------------------------------------------------
# Finding types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SensitiveFinding:
    """A single audit finding.

    `severity` is one of "info" | "warn" | "block". Callers
    decide whether `block` findings cause rejection; the
    audit itself never raises.
    """

    path: str
    severity: str  # "info" | "warn" | "block"
    pattern: str  # the regex pattern that matched
    matched_text: str  # excerpt of the matched string
    message: str

    def __post_init__(self) -> None:
        if self.severity not in {"info", "warn", "block"}:
            raise ValueError(
                f"severity MUST be 'info' | 'warn' | 'block'; "
                f"got {self.severity!r}"
            )


@dataclass(frozen=True)
class SensitiveAuditResult:
    """Collection of audit findings for a report."""

    findings: tuple[SensitiveFinding, ...]

    @property
    def has_block_findings(self) -> bool:
        return any(f.severity == "block" for f in self.findings)

    @property
    def has_warn_findings(self) -> bool:
        return any(f.severity == "warn" for f in self.findings)

    @property
    def block_findings(self) -> tuple[SensitiveFinding, ...]:
        return tuple(f for f in self.findings if f.severity == "block")

    @property
    def warn_findings(self) -> tuple[SensitiveFinding, ...]:
        return tuple(f for f in self.findings if f.severity == "warn")

    def __bool__(self) -> bool:
        return bool(self.findings)


# ---------------------------------------------------------------------------
# Audit function
# ---------------------------------------------------------------------------


def _walk_scoped_strings(
    value: Any,
    path: str,
    scoped_paths: FrozenSet[str],
) -> Iterable[tuple[str, str]]:
    """Walk a JSON-safe structure, yielding `(path, string)`
    pairs ONLY for strings reachable through a path that
    passes through at least one scoped segment.

    "Path passes through a scoped segment" means at least
    one segment in the `/`-separated path is in
    `scoped_paths`. For example:
      * `fixture_results/0/expected/value` has `expected`
        and `value` in the path → strings under this path
        ARE scanned.
      * `metrics/parse_success_rate/metric_id` has no
        scoped segment → strings under this path are NOT
        scanned.
      * `fixture_results/0/fixture_id` has no scoped
        segment → the fixture_id string is NOT scanned.
    """
    yield from _walk_with_scope(
        value, path, scoped_paths, scoped_so_far=False
    )


def _walk_with_scope(
    value: Any,
    path: str,
    scoped_paths: FrozenSet[str],
    scoped_so_far: bool,
) -> Iterable[tuple[str, str]]:
    """Recursive helper that tracks scope state along the
    path. Strings are emitted only when `scoped_so_far` is
    True; otherwise they're skipped (the field is not in
    a scoped region)."""
    if isinstance(value, str):
        if scoped_so_far:
            yield (path, value)
        return
    if isinstance(value, Mapping):
        for k, v in value.items():
            new_path = f"{path}/{k}" if path else k
            new_scoped = scoped_so_far or (k in scoped_paths)
            yield from _walk_with_scope(
                v, new_path, scoped_paths, new_scoped
            )
        return
    if isinstance(value, (list, tuple)):
        for i, v in enumerate(value):
            new_path = f"{path}/{i}"
            yield from _walk_with_scope(
                v, new_path, scoped_paths, scoped_so_far
            )
        return
    # Other primitives (numbers, bool, None): nothing to emit.


def _audit_string(
    path: str, string: str
) -> Iterable[SensitiveFinding]:
    """Check a string against the pattern catalogue and emit
    findings."""
    for pattern in _BLOCK_PATTERNS:
        match = pattern.search(string)
        if match is not None:
            yield SensitiveFinding(
                path=path,
                severity="block",
                pattern=pattern.pattern,
                matched_text=match.group(0)[:80],
                message=(
                    f"scoped string at {path!r} matches blocked "
                    f"pattern {pattern.pattern!r}; reject unless "
                    f"explicitly opted out"
                ),
            )
    for pattern in _WARN_PATTERNS:
        match = pattern.search(string)
        if match is not None:
            yield SensitiveFinding(
                path=path,
                severity="warn",
                pattern=pattern.pattern,
                matched_text=match.group(0)[:80],
                message=(
                    f"scoped string at {path!r} matches warned "
                    f"pattern {pattern.pattern!r}; review"
                ),
            )


def audit_report_sensitive_content(
    report_dict: Mapping[str, Any],
    *,
    scoped_paths: FrozenSet[str] | None = None,
) -> SensitiveAuditResult:
    """Audit a serialised report for sensitive content.

    Walks ONLY paths that pass through a segment in
    `scoped_paths` (default `DEFAULT_SCOPED_PATHS`). Returns
    findings; NEVER raises.

    Caller policy:

        The caller decides whether `block` findings cause
        rejection. A CI gate may reject by default; a
        historical artefact importer may opt to record
        warnings instead.

    The audit is deterministic — same input produces same
    findings, suitable for inclusion in audit logs.
    """
    effective_scope = (
        scoped_paths if scoped_paths is not None else DEFAULT_SCOPED_PATHS
    )
    findings: list[SensitiveFinding] = []
    for path, string in _walk_scoped_strings(
        report_dict, "", effective_scope
    ):
        findings.extend(_audit_string(path, string))
    return SensitiveAuditResult(findings=tuple(findings))


__all__ = [
    "DEFAULT_SCOPED_PATHS",
    "SensitiveFinding",
    "SensitiveAuditResult",
    "audit_report_sensitive_content",
]