"""
runner.py — P3.2.E evaluation runner with determinism
classes (D22).

Implements P3.2 design gate contract **D22**
(Reproducibility):

    * Three determinism classes:
        - D0 — byte-for-byte reproducible.
        - D1 — semantically deterministic
          (LLM with fixed prompt + model artifact).
        - D2 — non-deterministic.
    * `reproducibility_digest` is a SHA-256 over the
      full configuration that affects the evaluation:
        - dataset identity (id, version)
        - planner version
        - metric registry fingerprint
        - threshold registry fingerprint
        - sanitisation taxonomy hash
        - fixture identities
        - model fingerprint (when applicable)
        - prompt template hash (when applicable)
        - seed (when applicable)
    * The digest EXCLUDES:
        - timestamps
        - run_id
        - env paths, host/user info
    * The runner is a COORDINATOR, not an executor. It
      delegates actual fixture execution to a caller-
      provided `executor` callable. This keeps the
      runner deterministic and free of LLM / UI / OS
      dependencies.

Layer boundary (per round 2 review of P3.2.B / C / D):

    * The runner does NOT register metrics or thresholds
      (P3.2.C / P3.2.D own lifecycle).
    * The runner does NOT decide pass/fail. It populates
      `threshold_decisions` via the threshold evaluator
      (P3.2.D); the CI gate (P3.2.F) aggregates.
    * The runner does NOT modify the report schema.
      `reproducibility_digest` and `run_id` fields are
      already defined in P3.2.B (D18).
    * The runner is the FIRST module that imports from
      all earlier layers (datasets, reports, metrics,
      thresholds, sanitisation). This is intentional —
      it is the assembly point.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import (
    Any,
    Callable,
    FrozenSet,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
)

from agent.eval.datasets import Fixture, FixtureIdentity, semantic_fingerprint
from agent.eval.metrics import MetricRegistry, MetricSpec
from agent.eval.reports import (
    REPORT_SCHEMA_VERSION,
    EvaluationReport,
    FixtureResult,
    MetricValue,
)
from agent.eval.sanitisation import (
    ALL_EXPECTATION_KINDS,
    ALL_OBSERVED_KINDS,
)
from agent.eval.thresholds import (
    ThresholdRegistry,
    evaluate_all_thresholds,
)


# ---------------------------------------------------------------------------
# D22 determinism class taxonomy
# ---------------------------------------------------------------------------

DETERMINISM_CLASS_D0: str = "D0"
"""Byte-for-byte reproducible. Achieved when:
    * No LLM is involved (model_fingerprint and
      prompt_template_hash are both None).
    * Random seed is fixed.

   Same input + same code → byte-identical output.
"""

DETERMINISM_CLASS_D1: str = "D1"
"""Semantically deterministic. Achieved when an LLM is
   used but:
    * Model artifact fingerprint is fixed.
    * Prompt template hash is fixed.
    * Temperature is fixed (assumed by the caller).

   Same input + same code → semantically equivalent
   output (bytes may differ slightly across model
   versions, but the evaluator treats them as equivalent).
"""

DETERMINISM_CLASS_D2: str = "D2"
"""Non-deterministic. No seed, no LLM fingerprints, or
   any other non-reproducible input.

   Digest is still computed (for change detection) but
   the runner CANNOT guarantee run-to-run equivalence."""

ALL_DETERMINISM_CLASSES: FrozenSet[str] = frozenset(
    {
        DETERMINISM_CLASS_D0,
        DETERMINISM_CLASS_D1,
        DETERMINISM_CLASS_D2,
    }
)


# ---------------------------------------------------------------------------
# D22 digest inputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DigestInputs:
    """All reproducibility-relevant inputs (per D22).

    Includes:
      * dataset identity (id, version)
      * planner version
      * metric registry fingerprint
      * threshold registry fingerprint
      * sanitisation taxonomy hash
      * fixture identities hash
      * model fingerprint (for D1 / D0-with-LLM)
      * prompt template hash (for D1)
      * seed (for D0)

    Excludes (per D22):
      * timestamps
      * run_id
      * env paths, host / user info
    """

    dataset_id: str
    dataset_version: str
    planner_version: str
    metric_registry_fingerprint: str
    threshold_registry_fingerprint: str
    sanitisation_taxonomy_hash: str
    fixture_identities_hash: str
    model_fingerprint: Optional[str] = None
    prompt_template_hash: Optional[str] = None
    seed: Optional[int] = None

    def __post_init__(self) -> None:
        for name in (
            "dataset_id",
            "dataset_version",
            "planner_version",
            "metric_registry_fingerprint",
            "threshold_registry_fingerprint",
            "sanitisation_taxonomy_hash",
            "fixture_identities_hash",
        ):
            value = getattr(self, name)
            if not value or not isinstance(value, str):
                raise ValueError(
                    f"DigestInputs.{name} MUST be a non-empty string"
                )

    def digest(self) -> str:
        """Compute the reproducibility digest (SHA-256).

        The digest covers all semantically meaningful
        configuration. It is deterministic for a given
        configuration and excludes run-specific state
        (timestamps, run_id, env).
        """
        payload = {
            "dataset_id": self.dataset_id,
            "dataset_version": self.dataset_version,
            "planner_version": self.planner_version,
            "metric_registry_fingerprint": self.metric_registry_fingerprint,
            "threshold_registry_fingerprint": self.threshold_registry_fingerprint,
            "sanitisation_taxonomy_hash": self.sanitisation_taxonomy_hash,
            "fixture_identities_hash": self.fixture_identities_hash,
            "model_fingerprint": self.model_fingerprint,
            "prompt_template_hash": self.prompt_template_hash,
            "seed": self.seed,
        }
        encoded = json.dumps(payload, sort_keys=True).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


def classify_determinism(inputs: DigestInputs) -> str:
    """Classify the run's determinism level per D22.

    The classification rules:

      * D1 — at least one of model_fingerprint /
        prompt_template_hash is set (LLM-based with
        semantically-deterministic config).
      * D0 — no LLM fingerprints AND seed is set.
      * D2 — otherwise (no LLM, no seed → cannot
        guarantee).
    """
    has_llm = (
        inputs.model_fingerprint is not None
        or inputs.prompt_template_hash is not None
    )
    if has_llm:
        return DETERMINISM_CLASS_D1
    if inputs.seed is not None:
        return DETERMINISM_CLASS_D0
    return DETERMINISM_CLASS_D2


# ---------------------------------------------------------------------------
# D22 fingerprint helpers
# ---------------------------------------------------------------------------


def compute_metric_registry_fingerprint(registry: MetricRegistry) -> str:
    """SHA-256 over all metric spec fingerprints, sorted."""
    spec_fps = sorted(
        spec.fingerprint for spec in registry.all_specs()
    )
    payload = {"spec_fingerprints": spec_fps}
    encoded = json.dumps(payload, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def compute_threshold_registry_fingerprint(
    registry: ThresholdRegistry,
) -> str:
    """SHA-256 over all declaration fingerprints, sorted."""
    decl_fps = sorted(d.fingerprint for d in registry.all_declarations())
    payload = {"declaration_fingerprints": decl_fps}
    encoded = json.dumps(payload, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def compute_sanitisation_taxonomy_hash() -> str:
    """SHA-256 over the D21 kind taxonomy."""
    payload = {
        "expectation_kinds": sorted(ALL_EXPECTATION_KINDS),
        "observed_kinds": sorted(ALL_OBSERVED_KINDS),
    }
    encoded = json.dumps(payload, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def compute_fixture_identities_hash(fixtures: Sequence[Fixture]) -> str:
    """SHA-256 over sorted fixture semantic fingerprints."""
    fps = sorted(
        semantic_fingerprint(f.content, f.expected_outcome)
        for f in fixtures
    )
    payload = {"fixture_fingerprints": fps}
    encoded = json.dumps(payload, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def fixture_identity_to_dict(
    identity: FixtureIdentity,
) -> dict[str, Any]:
    """Convert a `FixtureIdentity` to a dict for digest
    purposes. Stable ordering via sorted keys."""
    return {
        "fixture_id": identity.fixture_id,
        "dataset_id": identity.dataset_id,
        "dataset_version": identity.dataset_version,
    }


# ---------------------------------------------------------------------------
# D22 fixture executor strategy
# ---------------------------------------------------------------------------


FixtureExecutor = Callable[[Fixture], Mapping[str, Any]]
"""Caller-provided callable that executes a fixture and
returns the observed payload as a Mapping.

The runner delegates to this callable. The runner itself
does NOT know how to execute fixtures — it only knows how
to coordinate.

Contract:
  * MUST return a `Mapping[str, Any]`.
  * MAY raise any exception; the runner captures the
    exception class and produces an `errored`
    `FixtureResult` with `observed.error_class`.
"""


class EvaluationRunnerError(RuntimeError):
    """Raised when the runner itself fails (configuration
    errors, executor contract violations).

    Note: fixture-level failures are NOT runner errors;
    they are captured as `errored` `FixtureResult`.
    """


# ---------------------------------------------------------------------------
# D22 evaluation runner
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EvaluationRunner:
    """Coordinator: builds an `EvaluationReport` from a
    sequence of fixtures and the configured registries.

    Layer boundary:
      * Coordinator, not executor. Delegates fixture
        execution to the caller-provided `executor`.
      * Pure data; immutable. Construct once, reuse.
      * Deterministic given the same inputs and
        registries.

    Lifecycle:

        runner = EvaluationRunner(
            dataset_id="...",
            dataset_version="v1",
            planner_version="p3.1.0",
            execution_profile="default",
            metric_registry=metric_registry,
            threshold_registry=threshold_registry,
            executor=my_executor,
        )
        report = runner.run(fixtures)

    The returned `EvaluationReport` is ready for the CI
    gate (P3.2.F).
    """

    dataset_id: str
    dataset_version: str
    planner_version: str
    execution_profile: str
    metric_registry: MetricRegistry
    threshold_registry: ThresholdRegistry
    executor: FixtureExecutor  # type: ignore[type-arg]

    # LLM / seed inputs (optional).
    model_fingerprint: Optional[str] = None
    prompt_template_hash: Optional[str] = None
    seed: Optional[int] = None

    def __post_init__(self) -> None:
        if not self.dataset_id:
            raise ValueError("dataset_id MUST be non-empty")
        if not self.dataset_version:
            raise ValueError("dataset_version MUST be non-empty")
        if not self.planner_version:
            raise ValueError("planner_version MUST be non-empty")
        if not self.execution_profile:
            raise ValueError("execution_profile MUST be non-empty")
        if not isinstance(self.metric_registry, MetricRegistry):
            raise ValueError(
                "metric_registry MUST be a MetricRegistry instance"
            )
        if not isinstance(self.threshold_registry, ThresholdRegistry):
            raise ValueError(
                "threshold_registry MUST be a ThresholdRegistry instance"
            )
        if not callable(self.executor):
            raise ValueError("executor MUST be callable")

    # -- digest inputs ---------------------------------------------------

    def compute_digest_inputs(
        self, fixtures: Sequence[Fixture]
    ) -> DigestInputs:
        """Build the `DigestInputs` for a given fixture set."""
        return DigestInputs(
            dataset_id=self.dataset_id,
            dataset_version=self.dataset_version,
            planner_version=self.planner_version,
            metric_registry_fingerprint=(
                compute_metric_registry_fingerprint(
                    self.metric_registry
                )
            ),
            threshold_registry_fingerprint=(
                compute_threshold_registry_fingerprint(
                    self.threshold_registry
                )
            ),
            sanitisation_taxonomy_hash=compute_sanitisation_taxonomy_hash(),
            fixture_identities_hash=compute_fixture_identities_hash(
                fixtures
            ),
            model_fingerprint=self.model_fingerprint,
            prompt_template_hash=self.prompt_template_hash,
            seed=self.seed,
        )

    # -- main entry point -------------------------------------------------

    def run(
        self,
        fixtures: Sequence[Fixture],
        *,
        run_id: Optional[str] = None,
        started_at: Optional[datetime] = None,
        ended_at: Optional[datetime] = None,
    ) -> EvaluationReport:
        """Run the evaluation and produce an
        `EvaluationReport`.

        Args:
          fixtures: the fixtures to evaluate.
          run_id: optional caller-provided run id. If not
            provided, derived from the digest prefix. NOTE:
            run_id is NOT part of the digest (per D22).
          started_at / ended_at: optional caller-provided
            timestamps (used for testing). If not provided,
            `datetime.now(timezone.utc)` is used. NOTE:
            timestamps are NOT part of the digest (per D22).

        Returns:
          A complete `EvaluationReport` ready for the CI
          gate.
        """
        started_at = self._normalise_timestamp(started_at)
        ended_at = self._normalise_timestamp(ended_at)

        # 1. Execute all fixtures.
        fixture_results = self._execute_fixtures(fixtures)

        # 2. Compute metrics (uses fixture_results).
        metrics_map = self._compute_metrics(fixture_results)

        # 3. Evaluate thresholds (uses metric values).
        decisions = evaluate_all_thresholds(
            {
                metric_id: mv.value
                for metric_id, mv in metrics_map.items()
            },
            self.threshold_registry,
        )

        # 4. Compute digest (uses registries + fixtures).
        digest_inputs = self.compute_digest_inputs(fixtures)
        digest = digest_inputs.digest()

        # 5. Resolve run_id (NOT in digest).
        if run_id is None:
            run_id = f"run-{digest[:16]}"

        return EvaluationReport(
            schema_version=REPORT_SCHEMA_VERSION,
            dataset_id=self.dataset_id,
            dataset_version=self.dataset_version,
            planner_version=self.planner_version,
            execution_profile=self.execution_profile,
            started_at=started_at,
            ended_at=ended_at,
            metrics=metrics_map,
            threshold_decisions=decisions,
            fixture_results=tuple(fixture_results),
            reproducibility_digest=digest,
            run_id=run_id,
        )

    # -- helpers ----------------------------------------------------------

    @staticmethod
    def _normalise_timestamp(
        ts: Optional[datetime],
    ) -> datetime:
        if ts is None:
            return datetime.now(timezone.utc)
        if ts.tzinfo is None:
            return ts.replace(tzinfo=timezone.utc)
        return ts

    def _execute_fixtures(
        self, fixtures: Sequence[Fixture]
    ) -> List[FixtureResult]:
        results: List[FixtureResult] = []
        for fixture in fixtures:
            results.append(self._execute_one(fixture))
        return results

    def _execute_one(self, fixture: Fixture) -> FixtureResult:
        fixture_id = fixture.identity.fixture_id
        expected = _ensure_mapping(fixture.expected_outcome)
        try:
            observed = self.executor(fixture)
        except Exception as exc:  # noqa: BLE001
            return FixtureResult(
                fixture_id=fixture_id,
                status="errored",
                expected=expected,
                observed={
                    "_error_class": type(exc).__name__,
                    "_error_message_class": _error_message_class(exc),
                },
            )
        if not isinstance(observed, Mapping):
            raise EvaluationRunnerError(
                f"executor returned non-Mapping for fixture "
                f"{fixture_id!r}: got {type(observed).__name__}. "
                f"Per D21, the observed payload MUST be a "
                f"Mapping."
            )
        return FixtureResult(
            fixture_id=fixture_id,
            status="completed",
            expected=expected,
            observed=dict(observed),
        )

    def _compute_metrics(
        self,
        fixture_results: Sequence[FixtureResult],
    ) -> dict[str, MetricValue]:
        """Build a transient report and use `compute_all_metrics`
        to compute every registered metric.

        Note: the metric formulas (P3.2.C) take an
        `EvaluationReport` argument. We construct a
        transient report with `metrics={}` and the
        `fixture_results` filled in; the formulas read
        `fixture_results` (not the metrics).
        """
        from agent.eval.metrics import compute_all_metrics

        # We use a fixed deterministic started_at / ended_at
        # for the transient report to keep the runner's
        # side effect on the report empty. The transient
        # report is not exposed.
        transient_started = datetime(1970, 1, 1, tzinfo=timezone.utc)
        transient = EvaluationReport(
            schema_version=REPORT_SCHEMA_VERSION,
            dataset_id=self.dataset_id,
            dataset_version=self.dataset_version,
            planner_version=self.planner_version,
            execution_profile=self.execution_profile,
            started_at=transient_started,
            ended_at=transient_started,
            metrics={},
            threshold_decisions=(),
            fixture_results=tuple(fixture_results),
            reproducibility_digest="",
            run_id="transient",
        )

        metric_floats = compute_all_metrics(
            transient, registry=self.metric_registry
        )

        # Wrap in MetricValue using spec metadata.
        wrapped: dict[str, MetricValue] = {}
        for metric_id, value in metric_floats.items():
            spec, _ = self.metric_registry.get(metric_id)
            wrapped[metric_id] = MetricValue(
                metric_id=metric_id,
                value=value,
                unit=spec.unit,
                direction=spec.direction,
            )
        return wrapped


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ensure_mapping(value: Any) -> dict[str, Any]:
    """Convert a `Mapping`-like value to a plain dict.

    The fixture's `expected_outcome` is a Mapping; we
    normalise to a dict for the report.
    """
    if not isinstance(value, Mapping):
        raise EvaluationRunnerError(
            f"fixture expected_outcome MUST be a Mapping; "
            f"got {type(value).__name__}"
        )
    return dict(value)


def _error_message_class(exc: BaseException) -> str:
    """Stable, sanitised label of an exception for the
    `errored` fixture result.

    Per D21, the error message itself MUST NOT appear
    in the report (it could leak sensitive content).
    Only the class name and a sanitised message-class
    label are recorded.
    """
    return _EXC_SAFE_CLASSES.get(
        type(exc).__name__, "other_error"
    )


# Sanitised exception label whitelist. Anything not in
# this map becomes "other_error" — the raw exception
# message is NEVER recorded in the report.
_EXC_SAFE_CLASSES: dict[str, str] = {
    "TimeoutError": "timeout",
    "ConnectionError": "connection_error",
    "PermissionError": "permission_error",
    "FileNotFoundError": "file_not_found",
    "ValueError": "value_error",
    "TypeError": "type_error",
    "KeyError": "key_error",
    "RuntimeError": "runtime_error",
    "AssertionError": "assertion_error",
    "NotImplementedError": "not_implemented",
    "KeyboardInterrupt": "interrupted",
    "SystemExit": "system_exit",
}


__all__ = [
    # Determinism classes.
    "DETERMINISM_CLASS_D0",
    "DETERMINISM_CLASS_D1",
    "DETERMINISM_CLASS_D2",
    "ALL_DETERMINISM_CLASSES",
    # Digest.
    "DigestInputs",
    "classify_determinism",
    # Fingerprint helpers.
    "compute_metric_registry_fingerprint",
    "compute_threshold_registry_fingerprint",
    "compute_sanitisation_taxonomy_hash",
    "compute_fixture_identities_hash",
    "fixture_identity_to_dict",
    # Runner.
    "FixtureExecutor",
    "EvaluationRunner",
    "EvaluationRunnerError",
]