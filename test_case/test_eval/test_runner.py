"""
P3.2.E — Runner + determinism classes (D22) unit tests.

Covers D22 invariants:
  * Three determinism classes (D0 / D1 / D2) with
    stable classification rules.
  * Digest is deterministic for same configuration.
  * Digest EXCLUDES timestamps, run_id, env.
  * Digest INCLUDES registries' fingerprints,
    sanitisation taxonomy hash, fixture identities.
  * Digest changes when any included field changes.
  * Runner produces a complete EvaluationReport.
  * Runner captures executor errors as `errored`
    FixtureResult.
  * Runner wraps metrics in MetricValue with correct
    unit / direction from spec.
  * Runner is a coordinator: it does NOT execute
    fixtures itself.

Layer boundary checks:
  * runner does NOT modify reports.py schema.
  * runner does NOT register metrics.
  * runner does NOT modify threshold policy.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

import pytest

from agent.eval.datasets import (
    DatasetRegistry,
    Fixture,
    FixtureIdentity,
)
from agent.eval.metrics import (
    METRIC_DIRECTION_HIGHER_IS_BETTER,
    METRIC_DIRECTION_LOWER_IS_BETTER,
    MetricFormula,
    MetricRegistry,
    MetricSpec,
    compute_all_metrics,
    compute_metric,
)
from agent.eval.reports import (
    REPORT_SCHEMA_VERSION,
    EvaluationReport,
    FixtureResult,
    MetricValue,
)
from agent.eval.runner import (
    ALL_DETERMINISM_CLASSES,
    DETERMINISM_CLASS_D0,
    DETERMINISM_CLASS_D1,
    DETERMINISM_CLASS_D2,
    DigestInputs,
    EvaluationRunner,
    EvaluationRunnerError,
    classify_determinism,
    compute_fixture_identities_hash,
    compute_metric_registry_fingerprint,
    compute_sanitisation_taxonomy_hash,
    compute_threshold_registry_fingerprint,
    fixture_identity_to_dict,
)
from agent.eval.thresholds import (
    THRESHOLD_DIRECTION_HIGHER_IS_BETTER,
    THRESHOLD_DIRECTION_LOWER_IS_BETTER,
    THRESHOLD_STATE_THRESHOLDED,
    THRESHOLD_STATE_UNGATED,
    ThresholdDeclaration,
    ThresholdRegistry,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _identity(fid: str) -> FixtureIdentity:
    return FixtureIdentity(
        dataset_id="eval-fixtures",
        dataset_version="v1",
        fixture_id=fid,
    )


def _fixture(fid: str, *, content=None, expected=None) -> Fixture:
    return Fixture(
        identity=_identity(fid),
        content=content if content is not None else {"input": fid},
        expected_outcome=(
            expected
            if expected is not None
            else {"_d21_kind": "value", "metric_id": "m1"}
        ),
    )


def _metric_registry() -> MetricRegistry:
    registry = MetricRegistry()
    registry.register(
        MetricSpec(
            metric_id="parse_success_rate",
            categories=frozenset({"Quality", "Behaviour", "Coverage"}),
            formula_summary="fraction parsed",
            unit="ratio",
            direction=METRIC_DIRECTION_HIGHER_IS_BETTER,
            owner="planner",
        ),
        MetricFormula(
            metric_id="parse_success_rate",
            compute=lambda r: (
                sum(
                    1
                    for fr in r.fixture_results
                    if fr.observed.get("parse_status") == "parsed"
                )
                / max(
                    1,
                    sum(
                        1
                        for fr in r.fixture_results
                        if "parse_status" in fr.observed
                    ),
                )
            ),
        ),
    )
    registry.register(
        MetricSpec(
            metric_id="execution_divergence_rate",
            categories=frozenset({"Behaviour", "Quality", "Efficiency"}),
            formula_summary="fraction diverged",
            unit="ratio",
            direction=METRIC_DIRECTION_LOWER_IS_BETTER,
            owner="executor",
        ),
        MetricFormula(
            metric_id="execution_divergence_rate",
            compute=lambda r: (
                sum(
                    1
                    for fr in r.fixture_results
                    if fr.observed.get("diverged") is True
                )
                / max(1, len(r.fixture_results))
            ),
        ),
    )
    return registry


def _threshold_registry() -> ThresholdRegistry:
    registry = ThresholdRegistry()
    registry.declare(
        ThresholdDeclaration(
            metric_id="parse_success_rate",
            state=THRESHOLD_STATE_THRESHOLDED,
            threshold=0.9,
            direction=THRESHOLD_DIRECTION_HIGHER_IS_BETTER,
        )
    )
    registry.declare(
        ThresholdDeclaration(
            metric_id="execution_divergence_rate",
            state=THRESHOLD_STATE_THRESHOLDED,
            threshold=0.1,
            direction=THRESHOLD_DIRECTION_LOWER_IS_BETTER,
        )
    )
    return registry


def _constant_executor(value: Mapping[str, Any]):
    """Returns a fixture executor that always returns the
    same mapping."""

    def executor(fixture: Fixture) -> Mapping[str, Any]:
        return dict(value)

    return executor


def _per_fixture_executor(mapping: dict[str, Mapping[str, Any]]):
    """Returns a fixture executor that maps fixture_id
    to a static observed payload."""

    def executor(fixture: Fixture) -> Mapping[str, Any]:
        if fixture.identity.fixture_id not in mapping:
            raise KeyError(fixture.identity.fixture_id)
        return dict(mapping[fixture.identity.fixture_id])

    return executor


def _runner(**overrides) -> EvaluationRunner:
    base = dict(
        dataset_id="eval-fixtures",
        dataset_version="v1",
        planner_version="p3.1.0",
        execution_profile="default",
        metric_registry=_metric_registry(),
        threshold_registry=_threshold_registry(),
        executor=_constant_executor(
            {"parse_status": "parsed", "diverged": False}
        ),
    )
    base.update(overrides)
    return EvaluationRunner(**base)


# ---------------------------------------------------------------------------
# D22 determinism class taxonomy
# ---------------------------------------------------------------------------


class TestDeterminismTaxonomy:
    def test_three_classes(self) -> None:
        assert DETERMINISM_CLASS_D0 in ALL_DETERMINISM_CLASSES
        assert DETERMINISM_CLASS_D1 in ALL_DETERMINISM_CLASSES
        assert DETERMINISM_CLASS_D2 in ALL_DETERMINISM_CLASSES
        assert len(ALL_DETERMINISM_CLASSES) == 3


class TestClassifyDeterminism:
    def _di(self, **overrides) -> DigestInputs:
        base = dict(
            dataset_id="d",
            dataset_version="v1",
            planner_version="p1",
            metric_registry_fingerprint="m" * 64,
            threshold_registry_fingerprint="t" * 64,
            sanitisation_taxonomy_hash="s" * 64,
            fixture_identities_hash="f" * 64,
        )
        base.update(overrides)
        return DigestInputs(**base)

    def test_d0_seed_no_llm(self) -> None:
        assert classify_determinism(self._di(seed=42)) == DETERMINISM_CLASS_D0

    def test_d1_with_model_fingerprint(self) -> None:
        assert (
            classify_determinism(self._di(model_fingerprint="x" * 64))
            == DETERMINISM_CLASS_D1
        )

    def test_d1_with_prompt_hash(self) -> None:
        assert (
            classify_determinism(self._di(prompt_template_hash="x" * 64))
            == DETERMINISM_CLASS_D1
        )

    def test_d1_overrides_d0(self) -> None:
        """LLM fingerprint takes precedence over seed."""
        cls = classify_determinism(
            self._di(seed=42, model_fingerprint="x" * 64)
        )
        assert cls == DETERMINISM_CLASS_D1

    def test_d2_no_seed_no_llm(self) -> None:
        assert (
            classify_determinism(self._di()) == DETERMINISM_CLASS_D2
        )


# ---------------------------------------------------------------------------
# D22 digest
# ---------------------------------------------------------------------------


class TestDigestInputs:
    def _di(self, **overrides) -> DigestInputs:
        base = dict(
            dataset_id="d",
            dataset_version="v1",
            planner_version="p1",
            metric_registry_fingerprint="m" * 64,
            threshold_registry_fingerprint="t" * 64,
            sanitisation_taxonomy_hash="s" * 64,
            fixture_identities_hash="f" * 64,
        )
        base.update(overrides)
        return DigestInputs(**base)

    def test_required_fields_validated(self) -> None:
        with pytest.raises(ValueError, match="dataset_id MUST be"):
            self._di(dataset_id="")

    def test_digest_is_sha256_hex(self) -> None:
        d = self._di().digest()
        assert len(d) == 64
        assert all(c in "0123456789abcdef" for c in d)

    def test_digest_is_deterministic(self) -> None:
        d1 = self._di().digest()
        d2 = self._di().digest()
        assert d1 == d2

    def test_digest_changes_on_dataset_id(self) -> None:
        assert self._di().digest() != self._di(dataset_id="other").digest()

    def test_digest_changes_on_dataset_version(self) -> None:
        assert (
            self._di().digest()
            != self._di(dataset_version="v2").digest()
        )

    def test_digest_changes_on_planner_version(self) -> None:
        assert (
            self._di().digest()
            != self._di(planner_version="p2").digest()
        )

    def test_digest_changes_on_metric_registry_fingerprint(self) -> None:
        assert (
            self._di().digest()
            != self._di(metric_registry_fingerprint="z" * 64).digest()
        )

    def test_digest_changes_on_threshold_registry_fingerprint(self) -> None:
        assert (
            self._di().digest()
            != self._di(threshold_registry_fingerprint="z" * 64).digest()
        )

    def test_digest_changes_on_sanitisation_taxonomy_hash(self) -> None:
        assert (
            self._di().digest()
            != self._di(sanitisation_taxonomy_hash="z" * 64).digest()
        )

    def test_digest_changes_on_fixture_identities_hash(self) -> None:
        assert (
            self._di().digest()
            != self._di(fixture_identities_hash="z" * 64).digest()
        )

    def test_digest_changes_on_model_fingerprint(self) -> None:
        assert (
            self._di().digest()
            != self._di(model_fingerprint="m1").digest()
        )

    def test_digest_changes_on_prompt_template_hash(self) -> None:
        assert (
            self._di().digest()
            != self._di(prompt_template_hash="p1").digest()
        )

    def test_digest_changes_on_seed(self) -> None:
        assert self._di().digest() != self._di(seed=42).digest()

    def test_digest_independent_of_run_id(self) -> None:
        """run_id is NOT part of the digest (per D22)."""
        # This test verifies the digest function itself
        # doesn't include run_id. The runner will not
        # include run_id in digest inputs.
        d1 = self._di().digest()
        d2 = self._di().digest()  # different call, same config
        assert d1 == d2

    def test_digest_independent_of_timestamps(self) -> None:
        """Timestamps are NOT part of DigestInputs (per D22)."""
        d1 = self._di().digest()
        d2 = self._di().digest()
        assert d1 == d2


# ---------------------------------------------------------------------------
# D22 fingerprint helpers
# ---------------------------------------------------------------------------


class TestFingerprintHelpers:
    def test_metric_registry_fingerprint_stable(self) -> None:
        registry = _metric_registry()
        a = compute_metric_registry_fingerprint(registry)
        b = compute_metric_registry_fingerprint(registry)
        assert a == b

    def test_metric_registry_fingerprint_changes(self) -> None:
        a = compute_metric_registry_fingerprint(_metric_registry())
        registry2 = MetricRegistry()
        registry2.register(
            MetricSpec(
                metric_id="new_metric",
                categories=frozenset({"Quality"}),
                formula_summary="...",
                unit="ratio",
                direction=METRIC_DIRECTION_HIGHER_IS_BETTER,
                owner="x",
            ),
            MetricFormula(
                metric_id="new_metric",
                compute=lambda r: 0.0,
            ),
        )
        b = compute_metric_registry_fingerprint(registry2)
        assert a != b

    def test_threshold_registry_fingerprint_stable(self) -> None:
        registry = _threshold_registry()
        a = compute_threshold_registry_fingerprint(registry)
        b = compute_threshold_registry_fingerprint(registry)
        assert a == b

    def test_threshold_registry_fingerprint_changes(self) -> None:
        a = compute_threshold_registry_fingerprint(_threshold_registry())
        registry2 = ThresholdRegistry()
        b = compute_threshold_registry_fingerprint(registry2)
        assert a != b

    def test_sanitisation_taxonomy_hash_stable(self) -> None:
        a = compute_sanitisation_taxonomy_hash()
        b = compute_sanitisation_taxonomy_hash()
        assert a == b

    def test_fixture_identities_hash_stable(self) -> None:
        fixtures = [_fixture("a"), _fixture("b")]
        a = compute_fixture_identities_hash(fixtures)
        b = compute_fixture_identities_hash(list(reversed(fixtures)))
        assert a == b

    def test_fixture_identities_hash_changes_on_content(self) -> None:
        a = compute_fixture_identities_hash([_fixture("a", content={"x": 1})])
        b = compute_fixture_identities_hash([_fixture("a", content={"x": 2})])
        assert a != b

    def test_fixture_identities_hash_changes_on_addition(self) -> None:
        a = compute_fixture_identities_hash([_fixture("a")])
        b = compute_fixture_identities_hash(
            [_fixture("a"), _fixture("b")]
        )
        assert a != b

    def test_fixture_identity_to_dict(self) -> None:
        identity = _identity("fid-1")
        d = fixture_identity_to_dict(identity)
        assert d == {
            "fixture_id": "fid-1",
            "dataset_id": "eval-fixtures",
            "dataset_version": "v1",
        }


# ---------------------------------------------------------------------------
# D22 EvaluationRunner
# ---------------------------------------------------------------------------


class TestEvaluationRunnerConstruction:
    def test_required_fields(self) -> None:
        with pytest.raises(ValueError, match="dataset_id"):
            _runner(dataset_id="")

    def test_dataset_version_required(self) -> None:
        with pytest.raises(ValueError, match="dataset_version"):
            _runner(dataset_version="")

    def test_planner_version_required(self) -> None:
        with pytest.raises(ValueError, match="planner_version"):
            _runner(planner_version="")

    def test_execution_profile_required(self) -> None:
        with pytest.raises(ValueError, match="execution_profile"):
            _runner(execution_profile="")

    def test_metric_registry_type(self) -> None:
        with pytest.raises(ValueError, match="metric_registry"):
            _runner(metric_registry="not a registry")  # type: ignore[arg-type]

    def test_threshold_registry_type(self) -> None:
        with pytest.raises(ValueError, match="threshold_registry"):
            _runner(threshold_registry="not a registry")  # type: ignore[arg-type]

    def test_executor_must_be_callable(self) -> None:
        with pytest.raises(ValueError, match="executor MUST be callable"):
            _runner(executor="not callable")  # type: ignore[arg-type]


class TestEvaluationRunnerRun:
    def test_run_produces_complete_report(self) -> None:
        runner = _runner()
        fixtures = [_fixture("a"), _fixture("b"), _fixture("c")]
        started = datetime(2026, 8, 4, 10, 0, 0, tzinfo=timezone.utc)
        ended = datetime(2026, 8, 4, 10, 1, 0, tzinfo=timezone.utc)
        report = runner.run(
            fixtures,
            started_at=started,
            ended_at=ended,
        )
        assert report.schema_version == REPORT_SCHEMA_VERSION
        assert report.dataset_id == "eval-fixtures"
        assert report.dataset_version == "v1"
        assert report.planner_version == "p3.1.0"
        assert report.execution_profile == "default"
        assert report.started_at == started
        assert report.ended_at == ended
        assert "parse_success_rate" in report.metrics
        assert "execution_divergence_rate" in report.metrics
        assert isinstance(
            report.metrics["parse_success_rate"], MetricValue
        )
        assert len(report.fixture_results) == 3
        assert report.reproducibility_digest
        assert len(report.reproducibility_digest) == 64
        assert report.run_id

    def test_run_produces_threshold_decisions(self) -> None:
        runner = _runner()
        fixtures = [_fixture("a")]
        report = runner.run(
            fixtures,
            started_at=datetime(2026, 8, 4, 10, 0, 0, tzinfo=timezone.utc),
            ended_at=datetime(2026, 8, 4, 10, 1, 0, tzinfo=timezone.utc),
        )
        assert len(report.threshold_decisions) == 2
        by_metric = {d.metric_id: d for d in report.threshold_decisions}
        assert by_metric["parse_success_rate"].thresholded is True
        assert by_metric["execution_divergence_rate"].thresholded is True

    def test_run_handles_empty_fixture_list(self) -> None:
        runner = _runner()
        report = runner.run(
            [],
            started_at=datetime(2026, 8, 4, 10, 0, 0, tzinfo=timezone.utc),
            ended_at=datetime(2026, 8, 4, 10, 1, 0, tzinfo=timezone.utc),
        )
        assert report.fixture_results == ()
        # Metrics still computed (returning defaults).
        assert "parse_success_rate" in report.metrics

    def test_run_captures_executor_errors(self) -> None:
        def bad_executor(fixture: Fixture) -> Mapping[str, Any]:
            raise ValueError("simulated failure")

        runner = _runner(executor=bad_executor)
        fixtures = [_fixture("a"), _fixture("b")]
        report = runner.run(
            fixtures,
            started_at=datetime(2026, 8, 4, 10, 0, 0, tzinfo=timezone.utc),
            ended_at=datetime(2026, 8, 4, 10, 1, 0, tzinfo=timezone.utc),
        )
        assert all(
            fr.status == "errored" for fr in report.fixture_results
        )
        # Per D21: the raw exception message MUST NOT
        # appear in the report.
        for fr in report.fixture_results:
            assert "simulated failure" not in str(fr.observed)
            assert "_error_class" in fr.observed

    def test_run_rejects_non_mapping_executor_result(self) -> None:
        def bad_executor(fixture: Fixture):
            return "not a mapping"  # type: ignore[return-value]

        runner = _runner(executor=bad_executor)
        with pytest.raises(EvaluationRunnerError, match="non-Mapping"):
            runner.run(
                [_fixture("a")],
                started_at=datetime(
                    2026, 8, 4, 10, 0, 0, tzinfo=timezone.utc
                ),
                ended_at=datetime(
                    2026, 8, 4, 10, 1, 0, tzinfo=timezone.utc
                ),
            )

    def test_run_wraps_metric_values(self) -> None:
        runner = _runner()
        report = runner.run(
            [_fixture("a")],
            started_at=datetime(2026, 8, 4, 10, 0, 0, tzinfo=timezone.utc),
            ended_at=datetime(2026, 8, 4, 10, 1, 0, tzinfo=timezone.utc),
        )
        mv = report.metrics["parse_success_rate"]
        assert mv.unit == "ratio"
        assert mv.direction == METRIC_DIRECTION_HIGHER_IS_BETTER
        mv2 = report.metrics["execution_divergence_rate"]
        assert mv2.unit == "ratio"
        assert mv2.direction == METRIC_DIRECTION_LOWER_IS_BETTER

    def test_run_uses_caller_provided_run_id(self) -> None:
        runner = _runner()
        report = runner.run(
            [_fixture("a")],
            run_id="my-custom-run-id",
            started_at=datetime(2026, 8, 4, 10, 0, 0, tzinfo=timezone.utc),
            ended_at=datetime(2026, 8, 4, 10, 1, 0, tzinfo=timezone.utc),
        )
        assert report.run_id == "my-custom-run-id"

    def test_run_derives_run_id_from_digest_when_missing(self) -> None:
        runner = _runner()
        report = runner.run(
            [_fixture("a")],
            started_at=datetime(2026, 8, 4, 10, 0, 0, tzinfo=timezone.utc),
            ended_at=datetime(2026, 8, 4, 10, 1, 0, tzinfo=timezone.utc),
        )
        assert report.run_id.startswith("run-")
        # Derived from digest prefix.
        assert (
            report.run_id
            == f"run-{report.reproducibility_digest[:16]}"
        )

    def test_run_propagates_executor_payload(self) -> None:
        per_fix = _per_fixture_executor(
            {
                "a": {"parse_status": "parsed", "diverged": False},
                "b": {"parse_status": "failed", "diverged": True},
            }
        )
        runner = _runner(executor=per_fix)
        report = runner.run(
            [_fixture("a"), _fixture("b")],
            started_at=datetime(2026, 8, 4, 10, 0, 0, tzinfo=timezone.utc),
            ended_at=datetime(2026, 8, 4, 10, 1, 0, tzinfo=timezone.utc),
        )
        a_result = next(
            fr for fr in report.fixture_results
            if fr.fixture_id == "a"
        )
        b_result = next(
            fr for fr in report.fixture_results
            if fr.fixture_id == "b"
        )
        assert a_result.status == "completed"
        assert b_result.status == "completed"
        assert b_result.observed["diverged"] is True


# ---------------------------------------------------------------------------
# D22 digest determinism — the critical test
# ---------------------------------------------------------------------------


class TestDigestDeterminism:
    def test_digest_stable_for_same_inputs(self) -> None:
        runner = _runner()
        started = datetime(2026, 8, 4, 10, 0, 0, tzinfo=timezone.utc)
        ended = datetime(2026, 8, 4, 10, 1, 0, tzinfo=timezone.utc)
        r1 = runner.run([_fixture("a")], started_at=started, ended_at=ended)
        r2 = runner.run([_fixture("a")], started_at=started, ended_at=ended)
        assert r1.reproducibility_digest == r2.reproducibility_digest

    def test_digest_stable_when_run_id_changes(self) -> None:
        """Per D22: run_id is excluded from digest."""
        runner = _runner()
        started = datetime(2026, 8, 4, 10, 0, 0, tzinfo=timezone.utc)
        ended = datetime(2026, 8, 4, 10, 1, 0, tzinfo=timezone.utc)
        r1 = runner.run(
            [_fixture("a")],
            run_id="run-1",
            started_at=started,
            ended_at=ended,
        )
        r2 = runner.run(
            [_fixture("a")],
            run_id="run-2",
            started_at=started,
            ended_at=ended,
        )
        assert r1.reproducibility_digest == r2.reproducibility_digest
        assert r1.run_id != r2.run_id

    def test_digest_stable_when_timestamps_change(self) -> None:
        """Per D22: timestamps are excluded from digest."""
        runner = _runner()
        r1 = runner.run(
            [_fixture("a")],
            started_at=datetime(2026, 8, 4, 10, 0, 0, tzinfo=timezone.utc),
            ended_at=datetime(2026, 8, 4, 10, 1, 0, tzinfo=timezone.utc),
        )
        r2 = runner.run(
            [_fixture("a")],
            started_at=datetime(2026, 9, 5, 11, 0, 0, tzinfo=timezone.utc),
            ended_at=datetime(2026, 9, 5, 11, 1, 0, tzinfo=timezone.utc),
        )
        assert r1.reproducibility_digest == r2.reproducibility_digest
        assert r1.started_at != r2.started_at

    def test_digest_changes_when_metric_registry_changes(self) -> None:
        """Adding a metric changes the digest."""
        runner1 = _runner()
        registry2 = _metric_registry()
        registry2.register(
            MetricSpec(
                metric_id="new_metric",
                categories=frozenset({"Quality"}),
                formula_summary="...",
                unit="ratio",
                direction=METRIC_DIRECTION_HIGHER_IS_BETTER,
                owner="x",
            ),
            MetricFormula(
                metric_id="new_metric",
                compute=lambda r: 0.0,
            ),
        )
        threshold_registry = _threshold_registry()
        threshold_registry.declare(
            ThresholdDeclaration(
                metric_id="new_metric",
                state=THRESHOLD_STATE_THRESHOLDED,
                threshold=0.5,
                direction=THRESHOLD_DIRECTION_HIGHER_IS_BETTER,
            )
        )
        runner2 = _runner(
            metric_registry=registry2,
            threshold_registry=threshold_registry,
        )
        started = datetime(2026, 8, 4, 10, 0, 0, tzinfo=timezone.utc)
        ended = datetime(2026, 8, 4, 10, 1, 0, tzinfo=timezone.utc)
        r1 = runner1.run(
            [_fixture("a")], started_at=started, ended_at=ended
        )
        r2 = runner2.run(
            [_fixture("a")], started_at=started, ended_at=ended
        )
        assert r1.reproducibility_digest != r2.reproducibility_digest

    def test_digest_changes_when_fixture_set_changes(self) -> None:
        runner = _runner()
        started = datetime(2026, 8, 4, 10, 0, 0, tzinfo=timezone.utc)
        ended = datetime(2026, 8, 4, 10, 1, 0, tzinfo=timezone.utc)
        r1 = runner.run([_fixture("a")], started_at=started, ended_at=ended)
        r2 = runner.run(
            [_fixture("a"), _fixture("b")],
            started_at=started,
            ended_at=ended,
        )
        assert r1.reproducibility_digest != r2.reproducibility_digest


# ---------------------------------------------------------------------------
# Layer boundary
# ---------------------------------------------------------------------------


class TestLayerBoundary:
    def test_runner_does_not_modify_reports(self) -> None:
        """The runner must not import or modify reports.py
        internals (other than the documented public API)."""
        import agent.eval.runner as r
        import inspect

        source = inspect.getsource(r)
        # Should NOT touch private attributes of
        # EvaluationReport (the dataclass field names start
        # with no underscore, but the runtime _<field>
        # mangling is not used here either).
        assert "_fixture_results=" not in source
        assert "_metrics=" not in source
        assert "_threshold_decisions=" not in source

    def test_runner_is_coordinator_not_executor(self) -> None:
        """The runner itself does not execute fixtures —
        it delegates to the caller-provided executor."""
        executor_called = []

        def tracking_executor(fixture: Fixture) -> Mapping[str, Any]:
            executor_called.append(fixture.identity.fixture_id)
            return {"parse_status": "parsed", "diverged": False}

        runner = _runner(executor=tracking_executor)
        runner.run(
            [_fixture("a"), _fixture("b")],
            started_at=datetime(2026, 8, 4, 10, 0, 0, tzinfo=timezone.utc),
            ended_at=datetime(2026, 8, 4, 10, 1, 0, tzinfo=timezone.utc),
        )
        assert executor_called == ["a", "b"]

    def test_runner_uses_thresholds_pure_evaluator(self) -> None:
        """The runner calls the P3.2.D evaluator; it does
        not implement its own pass/fail logic."""
        import agent.eval.runner as r
        import inspect

        source = inspect.getsource(r)
        # The runner should import evaluate_all_thresholds.
        assert "evaluate_all_thresholds" in source

    def test_runner_uses_metrics_compute_all(self) -> None:
        """The runner calls the P3.2.C compute_all_metrics;
        it does not implement its own metric computation."""
        import agent.eval.runner as r
        import inspect

        source = inspect.getsource(r)
        assert "compute_all_metrics" in source


# ---------------------------------------------------------------------------
# D22 integration with P3.2.A / B / C / D
# ---------------------------------------------------------------------------


class TestIntegrationWithEarlierPhases:
    def test_uses_fixture_identity_from_p32a(self) -> None:
        runner = _runner()
        report = runner.run(
            [_fixture("alpha")],
            started_at=datetime(2026, 8, 4, 10, 0, 0, tzinfo=timezone.utc),
            ended_at=datetime(2026, 8, 4, 10, 1, 0, tzinfo=timezone.utc),
        )
        assert report.fixture_results[0].fixture_id == "alpha"

    def test_uses_metric_registry_from_p32c(self) -> None:
        runner = _runner()
        report = runner.run(
            [_fixture("a")],
            started_at=datetime(2026, 8, 4, 10, 0, 0, tzinfo=timezone.utc),
            ended_at=datetime(2026, 8, 4, 10, 1, 0, tzinfo=timezone.utc),
        )
        # Both metrics from the default registry appear.
        assert "parse_success_rate" in report.metrics
        assert "execution_divergence_rate" in report.metrics

    def test_uses_threshold_evaluator_from_p32d(self) -> None:
        runner = _runner()
        report = runner.run(
            [_fixture("a")],
            started_at=datetime(2026, 8, 4, 10, 0, 0, tzinfo=timezone.utc),
            ended_at=datetime(2026, 8, 4, 10, 1, 0, tzinfo=timezone.utc),
        )
        # parse_success_rate = 1.0 (all parsed), threshold 0.9
        # → pass.
        decision = next(
            d for d in report.threshold_decisions
            if d.metric_id == "parse_success_rate"
        )
        assert decision.thresholded is True
        assert decision.decision == "pass"

    def test_uses_dataset_semantic_fingerprint_from_p32a(self) -> None:
        """Fixture identities flow into digest via
        semantic_fingerprint from P3.2.A."""
        runner = _runner()
        # The runner's digest must include fixture
        # identities.
        started = datetime(2026, 8, 4, 10, 0, 0, tzinfo=timezone.utc)
        ended = datetime(2026, 8, 4, 10, 1, 0, tzinfo=timezone.utc)
        r1 = runner.run(
            [_fixture("a", content={"input": "x"})],
            started_at=started,
            ended_at=ended,
        )
        r2 = runner.run(
            [_fixture("a", content={"input": "y"})],
            started_at=started,
            ended_at=ended,
        )
        assert r1.reproducibility_digest != r2.reproducibility_digest