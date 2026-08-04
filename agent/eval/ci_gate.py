"""
ci_gate.py — P3.2.F CI gate with sanitised output (D21).

Implements P3.2 design gate contract **D21** (CI
failure semantics — sanitised expected/observed
output) and the **CI gate** layer (the final
verdict layer that aggregates runner output).

The CI gate is a CONSUMER of `EvaluationReport`. It
does NOT:

  * Re-compute metrics (P3.2.C owns formulas).
  * Re-evaluate thresholds (P3.2.D owns the pure
    evaluator; the runner wires it).
  * Modify the report schema (P3.2.B owns).
  * Execute fixtures (P3.2.E owns; runner is the
    coordinator).

The CI gate ONLY:

  1. Reads the report's `threshold_decisions`.
  2. Maps the three-state threshold policy
     (pass / fail / ungated) to a single
     `pass` / `fail` verdict.
  3. Renders a sanitised JSON output (per D21)
     suitable for CI pipelines.
  4. Exposes a stable exit-code contract.

Layer boundary (per round 2 review of P3.2.B / C / D / E):

    reports.py (P3.2.B) — structural schema
         ↑
    runner.py   (P3.2.E) — coordinates + digest
         ↑
    ci_gate.py  (P3.2.F) — verdict + sanitised render
         ↑
    CI pipeline (consumes JSON output)
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from typing import (
    Any,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
)

from agent.eval.reports import (
    EvaluationReport,
    ThresholdDecision,
)


# ---------------------------------------------------------------------------
# Gate outcome / exit-code contract
# ---------------------------------------------------------------------------

GATE_OUTCOME_PASS: str = "pass"
GATE_OUTCOME_FAIL: str = "fail"

ALL_GATE_OUTCOMES: frozenset[str] = frozenset(
    {GATE_OUTCOME_PASS, GATE_OUTCOME_FAIL}
)

# Stable exit codes for CI pipelines.
#
#   0 — gate pass
#   1 — gate fail (one or more thresholded metrics failed,
#         OR one or more metrics have missing declaration)
#   2 — input error (could not read / parse the report)
#   3 — internal error (unexpected exception)
EXIT_CODE_PASS: int = 0
EXIT_CODE_FAIL: int = 1
EXIT_CODE_INPUT_ERROR: int = 2
EXIT_CODE_INTERNAL_ERROR: int = 3


# ---------------------------------------------------------------------------
# Gate decision
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GateDecision:
    """The CI gate's verdict on a single
    `EvaluationReport`.

    Aggregation rule (per D20 + D21):

      * `pass`  — every thresholded decision has
        `decision == "pass"`.
      * `fail`  — at least one thresholded decision has
        `decision == "fail"`. Missing declarations are
        represented as `decision == "fail"` by the
        runner (per P3.2.D), so they flow naturally into
        this rule.
      * `ungated` decisions are recorded but do NOT
        influence the verdict.

    The CI gate does NOT raise on missing declarations
    or unknown decision strings — it routes them to
    `failures`. This is the safest default for a CI
    pipeline: any ambiguity → fail loud.
    """

    outcome: str  # "pass" | "fail"
    reason: str
    passes: Tuple[ThresholdDecision, ...]
    failures: Tuple[ThresholdDecision, ...]
    ungated: Tuple[ThresholdDecision, ...]

    def is_passing(self) -> bool:
        return self.outcome == GATE_OUTCOME_PASS

    def exit_code(self) -> int:
        return (
            EXIT_CODE_PASS if self.outcome == GATE_OUTCOME_PASS
            else EXIT_CODE_FAIL
        )


def evaluate_gate(report: EvaluationReport) -> GateDecision:
    """Aggregate the report's `threshold_decisions` into a
    single CI gate verdict.

    The gate consumes the decisions already produced by
    the runner (P3.2.E) via the threshold evaluator
    (P3.2.D). It does NOT recompute anything.

    Behaviour:

      * Iterates over `report.threshold_decisions`.
      * For each decision with `thresholded == True`:
          - `decision == "pass"` → recorded in `passes`.
          - `decision == "fail"` → recorded in `failures`.
          - any other decision string → recorded in
            `failures` (safer default).
      * For each decision with `thresholded == False`:
          - recorded in `ungated`.
      * Verdict is `fail` iff `failures` is non-empty.
        Otherwise `pass`.
    """
    if not isinstance(report, EvaluationReport):
        raise TypeError(
            f"evaluate_gate expects EvaluationReport; "
            f"got {type(report).__name__}"
        )

    passes: List[ThresholdDecision] = []
    failures: List[ThresholdDecision] = []
    ungated: List[ThresholdDecision] = []

    for decision in report.threshold_decisions:
        if decision.thresholded:
            if decision.decision == "pass":
                passes.append(decision)
            else:
                # Includes "fail" (threshold breach) and
                # any unknown decision string (treated as
                # fail for CI safety).
                failures.append(decision)
        else:
            # thresholded=False → either "ungated" or
            # missing-declaration-as-fail.
            if decision.decision == "ungated":
                ungated.append(decision)
            else:
                # Missing declaration routed here; treat
                # as a failure to fail loud.
                failures.append(decision)

    if failures:
        outcome = GATE_OUTCOME_FAIL
        reason = _build_fail_reason(failures)
    else:
        outcome = GATE_OUTCOME_PASS
        reason = _build_pass_reason(passes, ungated)

    return GateDecision(
        outcome=outcome,
        reason=reason,
        passes=tuple(passes),
        failures=tuple(failures),
        ungated=tuple(ungated),
    )


def _build_fail_reason(failures: Sequence[ThresholdDecision]) -> str:
    """Build a human-readable failure reason."""
    n = len(failures)
    metric_ids = sorted({d.metric_id for d in failures})
    return (
        f"{n} thresholded metric(s) failed: {', '.join(metric_ids)}"
    )


def _build_pass_reason(
    passes: Sequence[ThresholdDecision],
    ungated: Sequence[ThresholdDecision],
) -> str:
    """Build a human-readable pass reason."""
    n_pass = len(passes)
    n_ungated = len(ungated)
    if n_pass == 0 and n_ungated == 0:
        return "no thresholded metrics; gate trivially passes"
    parts = [f"{n_pass} thresholded metric(s) passed"]
    if n_ungated:
        parts.append(f"{n_ungated} ungated metric(s) recorded")
    return "; ".join(parts)


# ---------------------------------------------------------------------------
# Sanitised rendering (per D21)
# ---------------------------------------------------------------------------

CI_GATE_SCHEMA_VERSION: str = "ci-gate-v1"


@dataclass(frozen=True)
class GateOutput:
    """Sanitised CI gate output.

    The output NEVER contains:

      * raw exception messages
      * prompt / screenshot / log content
      * unstructured text (only D21-kinded fields)

    It is safe to publish to CI dashboards or external
    consumers.
    """

    schema_version: str
    dataset: Mapping[str, str]
    planner_version: str
    execution_profile: str
    run_id: str
    started_at: str
    ended_at: str
    reproducibility_digest: str
    outcome: str
    reason: str
    summary: Mapping[str, Any]
    failures: Tuple[Mapping[str, Any], ...]
    ungated_metrics: Tuple[Mapping[str, Any], ...]
    fixture_summary: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "dataset": dict(self.dataset),
            "planner_version": self.planner_version,
            "execution_profile": self.execution_profile,
            "run_id": self.run_id,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "reproducibility_digest": self.reproducibility_digest,
            "outcome": self.outcome,
            "reason": self.reason,
            "summary": dict(self.summary),
            "failures": [dict(f) for f in self.failures],
            "ungated_metrics": [dict(u) for u in self.ungated_metrics],
            "fixture_summary": dict(self.fixture_summary),
        }


def render_sanitised_gate_output(
    decision: GateDecision,
    report: EvaluationReport,
) -> GateOutput:
    """Render a sanitised `GateOutput` from a
    `GateDecision` + `EvaluationReport`.

    Layer boundary:

      * Reads ONLY the public fields of `EvaluationReport`
        (no private attributes).
      * Does NOT include raw `expected` / `observed`
        payloads; only metric + threshold summary.
      * Does NOT include raw exception messages.
      * Emits D21-conformant structured output only.
    """
    return GateOutput(
        schema_version=CI_GATE_SCHEMA_VERSION,
        dataset={
            "id": report.dataset_id,
            "version": report.dataset_version,
        },
        planner_version=report.planner_version,
        execution_profile=report.execution_profile,
        run_id=report.run_id,
        started_at=_isoformat_utc(report.started_at),
        ended_at=_isoformat_utc(report.ended_at),
        reproducibility_digest=report.reproducibility_digest,
        outcome=decision.outcome,
        reason=decision.reason,
        summary={
            "passes": len(decision.passes),
            "failures": len(decision.failures),
            "ungated": len(decision.ungated),
            "total_metrics": (
                len(decision.passes)
                + len(decision.failures)
                + len(decision.ungated)
            ),
        },
        failures=_render_decisions(decision.failures),
        ungated_metrics=_render_decisions(decision.ungated),
        fixture_summary=_sanitised_fixture_summary(report),
    )


def _render_decisions(
    decisions: Sequence[ThresholdDecision],
) -> Tuple[Mapping[str, Any], ...]:
    """Render sanitised decision records.

    Per D21: threshold + observed + decision string;
    NO raw payload content.
    """
    rendered: List[Mapping[str, Any]] = []
    for d in decisions:
        record: dict[str, Any] = {
            "metric_id": d.metric_id,
            "observed": d.observed,
            "threshold": d.threshold,
            "decision": d.decision,
        }
        rendered.append(record)
    return tuple(rendered)


def _sanitised_fixture_summary(
    report: EvaluationReport,
) -> dict[str, Any]:
    """Per-fixture sanitised summary.

    Per D21:
      * NO raw exception messages.
      * NO raw expected/observed content.
      * ONLY counts + sanitised status labels.
    """
    by_status: dict[str, int] = {}
    errored_classes: set[str] = set()
    errored_message_classes: set[str] = set()
    for fr in report.fixture_results:
        by_status[fr.status] = by_status.get(fr.status, 0) + 1
        if fr.status == "errored":
            err_class = fr.observed.get("_error_class", "unknown")
            if isinstance(err_class, str):
                errored_classes.add(err_class)
            err_msg_class = fr.observed.get("_error_message_class")
            if isinstance(err_msg_class, str):
                errored_message_classes.add(err_msg_class)
    return {
        "total": len(report.fixture_results),
        "by_status": by_status,
        "errored_classes": sorted(errored_classes),
        "errored_message_classes": sorted(errored_message_classes),
    }


def _isoformat_utc(ts: Any) -> str:
    """Format a datetime as ISO-8601 UTC. Defensive
    against non-datetime inputs (e.g., already-serialised
    strings when reading from JSON)."""
    if hasattr(ts, "isoformat"):
        return ts.isoformat()
    return str(ts)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


class CIInputError(ValueError):
    """Raised when the CLI cannot parse its input."""


def cli_evaluate_report(
    report_path: str,
    *,
    output_format: str = "json",
) -> Tuple[int, str]:
    """CLI worker: read a report, evaluate the gate,
    render output, return (exit_code, output_text).

    This function is the testable core of the CLI. The
    `main` wrapper handles argv parsing and process exit.

    Args:
      report_path: path to the JSON `EvaluationReport`.
      output_format: "json" (default) | "verdict" (only
        the pass/fail string).

    Returns:
      Tuple of (exit_code, output_text). Output_text is
      JSON-serialised per D21 (or a one-line verdict
      string).
    """
    if output_format not in {"json", "verdict"}:
        raise CIInputError(
            f"output_format MUST be 'json' or 'verdict'; "
            f"got {output_format!r}"
        )

    # 1. Read + parse report.
    try:
        with open(report_path, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except FileNotFoundError as exc:
        raise CIInputError(
            f"report file not found: {report_path}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise CIInputError(
            f"report file is not valid JSON: {exc}"
        ) from exc

    try:
        report = EvaluationReport.from_dict(payload)
    except (KeyError, TypeError, ValueError) as exc:
        raise CIInputError(
            f"failed to parse EvaluationReport: {exc}"
        ) from exc

    # 2. Evaluate gate.
    decision = evaluate_gate(report)

    # 3. Render output.
    if output_format == "verdict":
        output_text = decision.outcome
    else:
        rendered = render_sanitised_gate_output(decision, report)
        output_text = json.dumps(
            rendered.to_dict(),
            indent=2,
            sort_keys=True,
        )

    return decision.exit_code(), output_text


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI entry point.

    Usage:
        python -m agent.eval.ci_gate <report.json>
        python -m agent.eval.ci_gate <report.json> --verdict

    Exit codes:
        0 — gate pass
        1 — gate fail
        2 — input error (cannot parse report)
        3 — internal error (unexpected)
    """
    if argv is None:
        argv = sys.argv[1:]

    if not argv or argv[0] in {"-h", "--help"}:
        print(
            "Usage: python -m agent.eval.ci_gate "
            "<report.json> [--verdict]",
            file=sys.stderr,
        )
        return EXIT_CODE_PASS  # help is not a failure

    report_path = argv[0]
    output_format = "verdict" if "--verdict" in argv else "json"

    try:
        exit_code, output_text = cli_evaluate_report(
            report_path, output_format=output_format
        )
    except CIInputError as exc:
        print(f"input error: {exc}", file=sys.stderr)
        return EXIT_CODE_INPUT_ERROR
    except Exception as exc:  # noqa: BLE001
        print(f"internal error: {exc}", file=sys.stderr)
        return EXIT_CODE_INTERNAL_ERROR

    print(output_text)
    return exit_code


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())