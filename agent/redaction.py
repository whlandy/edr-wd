"""
redaction.py — RedactionRegistry (P3.1 Commit D; backfilling
P2.4 deliverable).

P2.5 design gate contract D2 / D7 + P3.1 design gate D16:

  * Layer 1 — sanitisation at observation boundary.
  * Layer 2 — escape at render boundary.

This module is **Layer 1**. The registry applies regex-based
redaction to text content before persistence. It is paired
with the P1.4 `apply_text_redaction` (in `agent/trace/redaction.py`)
for the actual regex work; the registry adds:

  * `apply_to_payload(payload: dict) -> (dict, rule_ids)` —
    recursive over dict/list/scalar values, applies text
    redaction to string leaves (per D7: text fields only;
    numeric / keys are preserved).
  * `audit_artifacts(run_root: Path) -> AuditReport` —
    walks a run directory's `events.jsonl`, `step-results.json`,
    `manifest.json`, and `report.md`, scanning for any
    registered pattern. Returns zero-match counts when clean
    (per FR-P3.1-09 / acceptance #6).

The registry is **stateless** beyond its rule list. New rules
can be added at construction time. Rules are immutable
(`TextRule` is a frozen dataclass from `agent/trace/redaction.py`).

Layer-boundary notes:

  * This is NOT a universal escaper. Markdown escape lives in
    `agent/trace/markdown_escape.py`; LLM prompt escape is in
    `agent/planner/prompt.py` (Commit G). All three trust
    boundaries are distinct per D15.
  * This is NOT a parser. Sanitisation is applied AFTER
    `parse_plan` has validated the structure; malformed input
    is rejected by the parser, not silently redacted.
"""

from __future__ import annotations

import dataclasses
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from agent.trace.redaction import (  # noqa: F402  (cross-package import)
    TextRule,
    apply_text_redaction as _apply_text_redaction,
)


__all__ = [
    "RedactionRegistry",
    "RedactionAuditReport",
    "default_registry",
]


# ---------------------------------------------------------------------------
# Audit report
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RedactionAuditReport:
    """Outcome of `audit_artifacts` (FR-P3.1-09)."""

    files_scanned: tuple[str, ...]
    matches_per_file: Mapping[str, tuple[tuple[str, str], ...]]
    """Per-file list of `(rule_id, matched_text_preview)`."""

    rule_ids_registered: tuple[str, ...]
    matches_total: int

    @property
    def ok(self) -> bool:
        return self.matches_total == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "files_scanned": list(self.files_scanned),
            "matches_per_file": {
                f: [list(match) for match in matches]
                for f, matches in self.matches_per_file.items()
            },
            "rule_ids_registered": list(self.rule_ids_registered),
            "matches_total": self.matches_total,
            "ok": self.ok,
        }


# ---------------------------------------------------------------------------
# RedactionRegistry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RedactionRegistry:
    """Stateless text redaction registry.

    Construction:

      * `rules=[...]` — list of `TextRule` (regex patterns with
        stable rule_ids).
      * `policy_text_keys=None` — optional callable that, given
        a key name, returns True if the value SHOULD be
        redacted. Default: True for keys whose name ends in
        `_text`, `_description`, `_value`, `description`, `text`,
        `value`, `window_title`, `process_name`, `args`,
        `message`, `note`, `comment`, `args` — a heuristic for
        "user-controlled text"; explicit field-type metadata
        in payload can override (e.g. `{"_type": "numeric"}`).

    The registry does NOT recurse into anything that is not a
    dict / list / string / number / bool / None. Other types
    raise TypeError.

    Numeric values (int / float) are NEVER modified. Per D7
    field-type contract.

    """

    rules: tuple[TextRule, ...]
    # `redact_keys` was an early heuristic (key-tag text fields).
    # Removed in P3.1.D: per FR-P3.1-09, EVERY string leaf is
    # redacted (user-controlled args may contain anything).
    # Numeric / bool / None leaves pass through verbatim (D7).
    # Kept here as a no-op slot for future per-key overrides
    # (e.g. opt-in additional scrubbing for specific fields).
    redact_keys: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        # Compile each rule once; fail fast on bad regex.
        compiled = []
        for r in self.rules:
            r.compile()  # raises re.error if invalid
            compiled.append(r)
        # rules are already a tuple from the dataclass; no
        # further work needed.

    # ---- Layer-1 surface ----

    def apply_text(self, text: str) -> tuple[str, list[str]]:
        """Layer-1: redacts a single string. Returns
        `(redacted, fired_rule_ids)`."""
        return _apply_text_redaction(text, list(self.rules))

    def apply_to_payload(
        self, payload: Mapping[str, Any],
    ) -> tuple[dict[str, Any], list[str]]:
        """Layer-1: redacts text fields inside a payload.

        Walks dict / list / string leaves. Numeric / bool /
        None leaves are preserved verbatim. Per D7, numeric
        values MUST NOT be modified; keys are passed through.

        Returns `(redacted_payload, fired_rule_ids)`.
        `redacted_payload` is a NEW dict; the input is not
        mutated.
        """
        fired: list[str] = []
        out = _walk(payload, self.rules, fired)
        return out, fired

    # ---- Audit surface (FR-P3.1-09 / acceptance #6) ----

    def audit_artifacts(self, run_root: Path) -> RedactionAuditReport:
        """Walk a run directory and confirm that no registered
        rule's pattern appears anywhere in the persisted
        artifacts. Zero matches = clean.

        Scans:
          * `<run_root>/**/events.jsonl`
          * `<run_root>/**/step-results.json`
          * `<run_root>/**/manifest.json`
          * `<run_root>/**/report.md`
          * `<run_root>/**/trace.md`
          * `<run_root>/**/*.json`

        JSON files are decoded; `events.jsonl` is parsed line by
        line. Binary files are skipped.

        Returns `RedactionAuditReport` with `ok=True` iff
        `matches_total == 0`.
        """
        matches_per_file: dict[str, list[tuple[str, str]]] = {}
        files_scanned: list[str] = []

        targets: list[Path] = []
        if run_root.exists():
            for pattern in (
                "events.jsonl", "step-results.json", "manifest.json",
                "report.md", "trace.md",
            ):
                targets.extend(run_root.rglob(pattern))
            targets.extend(p for p in run_root.rglob("*.json")
                           if p.is_file())

        for path in sorted(set(targets)):
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            files_scanned.append(str(path))
            for line_no, line in enumerate(text.splitlines(), start=1):
                if not line:
                    continue
                fired = self._scan_line(line)
                if fired:
                    matches_per_file.setdefault(
                        str(path), [],
                    ).extend(
                        (rule_id, line[:80]) for rule_id in fired
                    )
                # also annotate by line number? keep simple.

        # Flatten matches across files for `matches_total`.
        matches_total = sum(len(m) for m in matches_per_file.values())

        return RedactionAuditReport(
            files_scanned=tuple(files_scanned),
            matches_per_file={
                f: tuple(m) for f, m in matches_per_file.items()
            },
            rule_ids_registered=tuple(r.rule_id for r in self.rules),
            matches_total=matches_total,
        )

    def _scan_line(self, line: str) -> list[str]:
        """Return rule_ids that match anywhere in `line`."""
        fired: list[str] = []
        for rule in self.rules:
            if rule.compile().search(line):
                fired.append(rule.rule_id)
        return fired


# ---------------------------------------------------------------------------
# Walker
# ---------------------------------------------------------------------------


def _walk(
    value: Any,
    rules: Sequence[TextRule],
    fired: list[str],
) -> Any:
    """Recursive walker for `apply_to_payload`.

    Redacts every STRING LEAF in the payload (per FR-P3.1-09
    "no secret arguments": user-controlled args may contain
    anything, so we do not key-tag text fields). Numeric /
    bool / None leaves pass through unchanged (per D7 field-
    type contract).
    """
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for k, v in value.items():
            out[k] = _walk(v, rules, fired)
        return out
    if isinstance(value, list):
        return [_walk(item, rules, fired) for item in value]
    if isinstance(value, tuple):
        return tuple(
            _walk(item, rules, fired) for item in value
        )
    if isinstance(value, str):
        red, ids = _apply_text_redaction(value, list(rules))
        fired.extend(ids)
        return red
    # Numeric / bool / None pass through verbatim. Per D7: the
    # field-type-aware sanitisation preserves numeric fields.
    return value


# ---------------------------------------------------------------------------
# Default registry (FR-P3.1-09)
# ---------------------------------------------------------------------------


def default_registry() -> RedactionRegistry:
    """Return a registry with sensible V1 defaults.

    The defaults cover common secret patterns. P3.2 / P3.3 may
    extend with environment-specific patterns.
    """
    rules = (
        TextRule(
            rule_id="email",
            pattern=r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",
        ),
        TextRule(
            rule_id="phone",
            pattern=r"\b\d{3}[-.]?\d{3,4}[-.]?\d{4}\b",
        ),
        # AWS access keys (canonical format).
        TextRule(
            rule_id="aws_access_key_id",
            pattern=r"\bAKIA[0-9A-Z]{16}\b",
        ),
        # Generic bearer tokens (very rough; consumer should
        # pin to a specific shape).
        TextRule(
            rule_id="bearer_token",
            pattern=r"\bBearer\s+[A-Za-z0-9._\-]{20,}\b",
        ),
        # PEM block header.
        TextRule(
            rule_id="pem_block",
            pattern=r"-----BEGIN [A-Z ]+-----",
        ),
    )
    return RedactionRegistry(rules=rules)