"""
cleanup_cascade.py — P2.4.C cascade semantics + read-time display_status.

This module owns the cascade rule that turns a case-attempt
manifest's `terminal_status` into a "displayed" status for the
report. The cascade is **read-time only**: it does NOT mutate the
manifest on disk (D17).

Cascade rule (P2.4 design §4 D12):

    display_status(m) = "failed"
        if m["terminal_status"] == "passed"
           and m["cleanup_status"] == "failed"
           and m["cleanup_outcome_critical"] is True
        else m["terminal_status"]

Equivalently:
    * If cleanup_status is "unknown" → no cascade (D13 backward
      compat — old manifests and never-ran cleanup both fall here).
    * If cleanup_status is "passed" → no cascade.
    * If cleanup_status is "failed" AND outcome_critical is True →
      cascade to "failed".
    * If cleanup_status is "failed" AND outcome_critical is False →
      no cascade (warning only; outcome remains `terminal_status`).

The cascade rule is the **only** place that flips `terminal_status`
based on cleanup. The renderer MUST NOT add additional inference
(D11 / D17). All downstream consumers (totals, attempt split,
aggregate status, per-case table) read `display_status(m)`.

Per-attempt tables (the `Per-Case Attempts` section) display the
`display_status` for the final attempt. The `Cleanup Warnings`
section is separate: it shows every case where `cleanup_status ==
"failed"` regardless of cascade outcome — so users can see
non-critical cleanup failures as informational.
"""

from __future__ import annotations

from typing import Any, Mapping


# Terminal status values used by the cascade. Mirrors the enum used
# in `compute_aggregate_status` and `Totals` (render_report.py).
TERMINAL_PASSED = "passed"
TERMINAL_FAILED = "failed"
TERMINAL_BLOCKED = "blocked"
TERMINAL_SKIPPED = "skipped"
TERMINAL_UNKNOWN = "unknown"

# Cleanup status values (mirror agent/trace/manifest.py).
CLEANUP_PASSED = "passed"
CLEANUP_FAILED = "failed"
CLEANUP_UNKNOWN = "unknown"


def compute_display_status(manifest: Mapping[str, Any]) -> str:
    """Compute the displayed status for a case-attempt manifest.

    Implements the cascade rule from design §4 D12:

      display_status =
          "failed"
              if terminal_status == "passed"
                 and cleanup_status == "failed"
                 and cleanup_outcome_critical is True
          else terminal_status

    Read-time only — does NOT mutate the manifest dict.

    Backward compat (D13):
      * Missing `cleanup_status` → treated as "unknown" → no cascade.
      * Missing `cleanup_outcome_critical` → treated as False →
        no cascade (per design: only critical+failed triggers).

    Args:
        manifest: case-attempt manifest dict (must contain at minimum
            `terminal_status`; cleanup fields are optional).

    Returns:
        One of `"passed"`, `"failed"`, `"blocked"`, `"skipped"`,
        `"unknown"`. The cascade rule preserves the original terminal
        status in all cases except the explicit
        (terminal=passed, cleanup=failed, outcome_critical=True)
        combination.
    """
    terminal = manifest.get("terminal_status", TERMINAL_UNKNOWN)
    cleanup_status = manifest.get("cleanup_status", CLEANUP_UNKNOWN)
    cleanup_outcome_critical = bool(
        manifest.get("cleanup_outcome_critical", False)
    )

    if (
        terminal == TERMINAL_PASSED
        and cleanup_status == CLEANUP_FAILED
        and cleanup_outcome_critical is True
    ):
        return TERMINAL_FAILED

    return terminal


def has_cleanup_failure(manifest: Mapping[str, Any]) -> bool:
    """Whether this manifest has a non-passed cleanup status.

    Returns True iff `cleanup_status` is exactly `"failed"`. Used by
    the `Cleanup Warnings` markdown section to decide whether to
    surface a case in the warnings table — regardless of whether the
    cascade rule flipped the displayed status.

    Distinct from `compute_display_status`:
      * `compute_display_status` decides the *case-level* outcome.
      * `has_cleanup_failure` decides whether the *cleanup* itself
        surfaced a problem (informational).
    """
    return manifest.get("cleanup_status", CLEANUP_UNKNOWN) == CLEANUP_FAILED


__all__ = [
    "compute_display_status",
    "has_cleanup_failure",
    "TERMINAL_PASSED",
    "TERMINAL_FAILED",
    "TERMINAL_BLOCKED",
    "TERMINAL_SKIPPED",
    "TERMINAL_UNKNOWN",
    "CLEANUP_PASSED",
    "CLEANUP_FAILED",
    "CLEANUP_UNKNOWN",
]