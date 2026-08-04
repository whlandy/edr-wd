"""
P3.2.D — D21 sanitisation kinds taxonomy unit tests.

Covers D21 invariants:

  * Expectation kinds = {threshold, stage_reached, match_target}.
  * Observed kinds = {value, mismatch, stage_reached,
    stage_missed, aborted, errored}.
  * StructuredExpectation validates kind + Mapping payload.
  * SanitisedObserved validates kind + Mapping payload.
  * Raw string payload rejected (per D21 anti-raw-text).
  * Builders and kind checkers work as expected.

Layer boundary checks:

  * sanitisation.py does not import thresholds policy.
  * sanitisation.py does not import runner mechanics.
  * sanitisation.py does not modify reports.py.
"""

from __future__ import annotations

from typing import Any

import pytest

from agent.eval.sanitisation import (
    ALL_EXPECTATION_KINDS,
    ALL_OBSERVED_KINDS,
    EXPECTATION_KIND_MATCH_TARGET,
    EXPECTATION_KIND_STAGE_REACHED,
    EXPECTATION_KIND_THRESHOLD,
    OBSERVED_KIND_ABORTED,
    OBSERVED_KIND_ERRORED,
    OBSERVED_KIND_MISMATCH,
    OBSERVED_KIND_STAGE_MISSED,
    OBSERVED_KIND_STAGE_REACHED,
    OBSERVED_KIND_VALUE,
    SanitisationKindError,
    SanitisedObserved,
    StructuredExpectation,
    check_expectation_kind,
    check_observed_kind,
    make_expectation,
    make_observed,
    taxonomy_summary,
)


# ---------------------------------------------------------------------------
# D21 taxonomy
# ---------------------------------------------------------------------------


class TestExpectationKinds:
    def test_three_expectation_kinds(self) -> None:
        assert EXPECTATION_KIND_THRESHOLD in ALL_EXPECTATION_KINDS
        assert EXPECTATION_KIND_STAGE_REACHED in ALL_EXPECTATION_KINDS
        assert EXPECTATION_KIND_MATCH_TARGET in ALL_EXPECTATION_KINDS
        assert len(ALL_EXPECTATION_KINDS) == 3

    def test_kind_checker_accepts_valid(self) -> None:
        check_expectation_kind(EXPECTATION_KIND_THRESHOLD)
        check_expectation_kind(EXPECTATION_KIND_STAGE_REACHED)
        check_expectation_kind(EXPECTATION_KIND_MATCH_TARGET)

    def test_kind_checker_rejects_invalid(self) -> None:
        with pytest.raises(SanitisationKindError, match="expectation kind"):
            check_expectation_kind("not_a_kind")


class TestObservedKinds:
    def test_six_observed_kinds(self) -> None:
        assert OBSERVED_KIND_VALUE in ALL_OBSERVED_KINDS
        assert OBSERVED_KIND_MISMATCH in ALL_OBSERVED_KINDS
        assert OBSERVED_KIND_STAGE_REACHED in ALL_OBSERVED_KINDS
        assert OBSERVED_KIND_STAGE_MISSED in ALL_OBSERVED_KINDS
        assert OBSERVED_KIND_ABORTED in ALL_OBSERVED_KINDS
        assert OBSERVED_KIND_ERRORED in ALL_OBSERVED_KINDS
        assert len(ALL_OBSERVED_KINDS) == 6

    def test_kind_checker_accepts_valid(self) -> None:
        for kind in ALL_OBSERVED_KINDS:
            check_observed_kind(kind)

    def test_kind_checker_rejects_invalid(self) -> None:
        with pytest.raises(SanitisationKindError, match="observed kind"):
            check_observed_kind("not_a_kind")


class TestTaxonomySummary:
    def test_summary_counts(self) -> None:
        s = taxonomy_summary()
        assert s == {
            "expectation_kinds": 3,
            "observed_kinds": 6,
        }


# ---------------------------------------------------------------------------
# D21 StructuredExpectation
# ---------------------------------------------------------------------------


class TestStructuredExpectation:
    def test_valid(self) -> None:
        e = StructuredExpectation(
            kind=EXPECTATION_KIND_THRESHOLD,
            value={"metric_id": "m1", "threshold": 0.9},
        )
        assert e.kind == EXPECTATION_KIND_THRESHOLD
        assert e.value == {"metric_id": "m1", "threshold": 0.9}

    def test_invalid_kind_rejected(self) -> None:
        with pytest.raises(SanitisationKindError, match="kind MUST be one of"):
            StructuredExpectation(kind="invalid_kind", value={})

    def test_raw_string_value_rejected(self) -> None:
        """Per D21, raw strings are forbidden at the
        payload level."""
        with pytest.raises(SanitisationKindError, match="MUST be a Mapping"):
            StructuredExpectation(
                kind=EXPECTATION_KIND_THRESHOLD,  # type: ignore[arg-type]
                value="click login button",
            )

    def test_list_value_rejected(self) -> None:
        with pytest.raises(SanitisationKindError, match="MUST be a Mapping"):
            StructuredExpectation(
                kind=EXPECTATION_KIND_THRESHOLD,
                value=["a", "b"],  # type: ignore[arg-type]
            )

    def test_is_frozen(self) -> None:
        e = StructuredExpectation(
            kind=EXPECTATION_KIND_THRESHOLD, value={}
        )
        with pytest.raises(Exception):
            e.kind = "other"  # type: ignore[misc]

    def test_to_mapping(self) -> None:
        e = StructuredExpectation(
            kind=EXPECTATION_KIND_THRESHOLD,
            value={"metric_id": "m1", "threshold": 0.9},
        )
        m = e.to_mapping()
        assert m["_d21_kind"] == EXPECTATION_KIND_THRESHOLD
        assert m["_d21_value"] == {"metric_id": "m1", "threshold": 0.9}

    def test_empty_value_allowed(self) -> None:
        e = StructuredExpectation(
            kind=EXPECTATION_KIND_STAGE_REACHED, value={}
        )
        assert e.value == {}


# ---------------------------------------------------------------------------
# D21 SanitisedObserved
# ---------------------------------------------------------------------------


class TestSanitisedObserved:
    def test_valid(self) -> None:
        o = SanitisedObserved(
            kind=OBSERVED_KIND_VALUE,
            summary={"metric_id": "m1", "value": 0.95},
        )
        assert o.kind == OBSERVED_KIND_VALUE
        assert o.summary == {"metric_id": "m1", "value": 0.95}

    def test_invalid_kind_rejected(self) -> None:
        with pytest.raises(SanitisationKindError, match="kind MUST be one of"):
            SanitisedObserved(kind="invalid_kind", summary={})

    def test_raw_string_summary_rejected(self) -> None:
        with pytest.raises(SanitisationKindError, match="MUST be a Mapping"):
            SanitisedObserved(
                kind=OBSERVED_KIND_VALUE,  # type: ignore[arg-type]
                summary="0.95",
            )

    def test_is_frozen(self) -> None:
        o = SanitisedObserved(kind=OBSERVED_KIND_VALUE, summary={})
        with pytest.raises(Exception):
            o.kind = "other"  # type: ignore[misc]

    def test_to_mapping(self) -> None:
        o = SanitisedObserved(
            kind=OBSERVED_KIND_MISMATCH,
            summary={"expected": 0.9, "observed": 0.5},
        )
        m = o.to_mapping()
        assert m["_d21_kind"] == OBSERVED_KIND_MISMATCH
        assert m["_d21_summary"] == {"expected": 0.9, "observed": 0.5}

    def test_empty_summary_allowed(self) -> None:
        o = SanitisedObserved(kind=OBSERVED_KIND_ABORTED, summary={})
        assert o.summary == {}


# ---------------------------------------------------------------------------
# D21 builders
# ---------------------------------------------------------------------------


class TestBuilders:
    def test_make_expectation_with_value(self) -> None:
        e = make_expectation(
            EXPECTATION_KIND_THRESHOLD,
            {"metric_id": "m1", "threshold": 0.9},
        )
        assert e.kind == EXPECTATION_KIND_THRESHOLD
        assert e.value["metric_id"] == "m1"

    def test_make_expectation_default_empty(self) -> None:
        e = make_expectation(EXPECTATION_KIND_STAGE_REACHED)
        assert e.kind == EXPECTATION_KIND_STAGE_REACHED
        assert e.value == {}

    def test_make_observed_with_summary(self) -> None:
        o = make_observed(
            OBSERVED_KIND_VALUE,
            {"metric_id": "m1", "value": 0.95},
        )
        assert o.kind == OBSERVED_KIND_VALUE
        assert o.summary["metric_id"] == "m1"

    def test_make_observed_default_empty(self) -> None:
        o = make_observed(OBSERVED_KIND_ERRORED)
        assert o.kind == OBSERVED_KIND_ERRORED
        assert o.summary == {}

    def test_make_expectation_invalid_kind_raises(self) -> None:
        with pytest.raises(SanitisationKindError):
            make_expectation("not_a_kind")

    def test_make_observed_invalid_kind_raises(self) -> None:
        with pytest.raises(SanitisationKindError):
            make_observed("not_a_kind")


# ---------------------------------------------------------------------------
# Layer boundary checks
# ---------------------------------------------------------------------------


class TestLayerBoundary:
    def test_sanitisation_does_not_import_thresholds(self) -> None:
        """D21 sanitisation kinds MUST NOT depend on D20
        threshold policy."""
        import agent.eval.sanitisation as s
        import inspect

        source = inspect.getsource(s)
        # Look for actual import statements, not docstring mentions.
        assert "import agent.eval.thresholds" not in source
        assert "from agent.eval.thresholds" not in source, (
            "sanitisation.py MUST NOT import thresholds "
            "module; D21 is independent of D20 policy"
        )

    def test_sanitisation_does_not_import_reports(self) -> None:
        """D21 sanitisation kinds MUST NOT modify reports.py."""
        import agent.eval.sanitisation as s
        import inspect

        source = inspect.getsource(s)
        assert "import agent.eval.reports" not in source
        assert "from agent.eval.reports" not in source, (
            "sanitisation.py MUST NOT import reports module; "
            "D21 is a separate taxonomy layer"
        )

    def test_sanitisation_does_not_import_runner(self) -> None:
        """D21 sanitisation kinds MUST NOT depend on runner."""
        import agent.eval.sanitisation as s
        import inspect

        source = inspect.getsource(s)
        assert "import agent.eval.runner" not in source
        assert "from agent.eval.runner" not in source, (
            "sanitisation.py MUST NOT import runner; "
            "D21 is independent of runner mechanics"
        )

    def test_to_mapping_is_deterministic(self) -> None:
        e = StructuredExpectation(
            kind=EXPECTATION_KIND_THRESHOLD,
            value={"a": 1, "b": 2},
        )
        m1 = e.to_mapping()
        m2 = e.to_mapping()
        assert m1 == m2

    def test_structured_types_are_pure(self) -> None:
        """StructuredExpectation and SanitisedObserved are
        pure data; construction is deterministic and equal
        inputs produce equal outputs."""
        e1 = StructuredExpectation(
            kind=EXPECTATION_KIND_MATCH_TARGET,
            value={"target_id": "submit"},
        )
        e2 = StructuredExpectation(
            kind=EXPECTATION_KIND_MATCH_TARGET,
            value={"target_id": "submit"},
        )
        assert e1 == e2
        # Note: hash not asserted because Mapping fields
        # are unhashable. Equality is the contract.
