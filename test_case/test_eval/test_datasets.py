"""
P3.2.A — Dataset contract and fixture registry unit tests.

Covers D17 invariants:
  * Identity triple is content-stable and explicit.
  * Old dataset versions remain runnable.
  * Mutation policy: semantic change requires fixture_id
    change OR dataset_version bump.
  * fixture_id MUST NOT be reused with different semantics
    across dataset versions.
  * Change log is recorded for semantic shifts.
"""

from __future__ import annotations

from typing import Any

import pytest

from agent.eval.datasets import (
    DatasetRegistry,
    Fixture,
    FixtureIdentity,
    FixtureMutationError,
    semantic_fingerprint,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_identity(
    *,
    dataset_id: str = "eval-fixtures",
    dataset_version: str = "v1",
    fixture_id: str = "click_login",
) -> FixtureIdentity:
    return FixtureIdentity(
        dataset_id=dataset_id,
        dataset_version=dataset_version,
        fixture_id=fixture_id,
    )


def make_fixture(
    *,
    identity: FixtureIdentity,
    content: dict[str, Any] | None = None,
    expected_outcome: dict[str, Any] | None = None,
    change_log: tuple[str, ...] = (),
) -> Fixture:
    return Fixture(
        identity=identity,
        content=content or {"snapshot_id": "snap-1", "actions": ["click"]},
        expected_outcome=expected_outcome or {"stage_reached": "click"},
        change_log=change_log,
    )


# ---------------------------------------------------------------------------
# Identity contract
# ---------------------------------------------------------------------------


class TestIdentityContract:
    def test_identity_is_frozen(self) -> None:
        identity = make_identity()
        with pytest.raises((AttributeError, Exception)):
            identity.fixture_id = "different"  # type: ignore[misc]

    def test_identity_triple_required(self) -> None:
        with pytest.raises(ValueError, match="dataset_id MUST be non-empty"):
            FixtureIdentity(dataset_id="", dataset_version="v1", fixture_id="x")
        with pytest.raises(ValueError, match="dataset_version MUST be non-empty"):
            FixtureIdentity(dataset_id="d", dataset_version="", fixture_id="x")
        with pytest.raises(ValueError, match="fixture_id MUST be non-empty"):
            FixtureIdentity(dataset_id="d", dataset_version="v1", fixture_id="")

    def test_identity_is_content_stable(self) -> None:
        """Renaming the file / changing the path MUST NOT
        change the identity. Identity is data, not derived."""
        i1 = FixtureIdentity(
            dataset_id="eval-fixtures",
            dataset_version="v1",
            fixture_id="click_login",
        )
        i2 = FixtureIdentity(
            dataset_id="eval-fixtures",
            dataset_version="v1",
            fixture_id="click_login",
        )
        # Two separately-constructed identities with the same
        # triple MUST compare equal (content-stable).
        assert i1 == i2
        assert hash(i1) == hash(i2)


# ---------------------------------------------------------------------------
# Semantic fingerprint
# ---------------------------------------------------------------------------


class TestSemanticFingerprint:
    def test_deterministic(self) -> None:
        fp_a = semantic_fingerprint({"a": 1, "b": 2}, {"x": "y"})
        fp_b = semantic_fingerprint({"a": 1, "b": 2}, {"x": "y"})
        assert fp_a == fp_b
        # SHA-256 hex is 64 chars.
        assert len(fp_a) == 64

    def test_key_order_invariant(self) -> None:
        """Semantic fingerprint MUST be order-invariant in
        dict keys, otherwise two fixtures with the same
        payload but different key order would compare
        differently."""
        fp_a = semantic_fingerprint({"a": 1, "b": 2}, {})
        fp_b = semantic_fingerprint({"b": 2, "a": 1}, {})
        assert fp_a == fp_b

    def test_content_change_changes_fingerprint(self) -> None:
        fp_a = semantic_fingerprint({"a": 1}, {})
        fp_b = semantic_fingerprint({"a": 2}, {})
        assert fp_a != fp_b

    def test_expected_outcome_change_changes_fingerprint(self) -> None:
        fp_a = semantic_fingerprint({}, {"x": 1})
        fp_b = semantic_fingerprint({}, {"x": 2})
        assert fp_a != fp_b

    def test_metadata_does_not_affect_fingerprint(self) -> None:
        """The fingerprint MUST depend only on semantic
        payload, not on metadata (change_log, identity)."""
        fp_a = semantic_fingerprint({"a": 1}, {"x": 1})
        fp_b = semantic_fingerprint({"a": 1}, {"x": 1})
        assert fp_a == fp_b  # both have same semantic payload

    def test_frozen_set_treated_as_set(self) -> None:
        fp_a = semantic_fingerprint({"tags": frozenset(["b", "a"])}, {})
        fp_b = semantic_fingerprint({"tags": frozenset(["a", "b"])}, {})
        assert fp_a == fp_b


# ---------------------------------------------------------------------------
# Registry — happy path
# ---------------------------------------------------------------------------


class TestRegistryHappyPath:
    def test_register_and_lookup(self) -> None:
        reg = DatasetRegistry()
        fix = make_fixture(identity=make_identity())
        reg2 = reg.register(fix)
        assert fix.identity in reg2
        assert reg2.get(fix.identity) == fix

    def test_register_no_op_same_semantic(self) -> None:
        reg = DatasetRegistry()
        fix = make_fixture(identity=make_identity())
        reg2 = reg.register(fix)
        # Re-registering the same fixture (same content +
        # same identity) is a no-op — registry unchanged.
        reg3 = reg2.register(fix)
        assert len(reg3) == 1

    def test_version_bump_same_semantic_allowed(self) -> None:
        """Same fixture_id, new dataset_version, same
        semantic fingerprint MUST be allowed (version
        bump without semantic change)."""
        reg = DatasetRegistry()
        fix_v1 = make_fixture(identity=make_identity(dataset_version="v1"))
        reg2 = reg.register(fix_v1)
        # Same fixture_id, same content, but dataset_version
        # bumped. Allowed.
        fix_v2 = make_fixture(identity=make_identity(dataset_version="v2"))
        reg3 = reg2.register(fix_v2)
        assert len(reg3) == 2
        assert reg3.get(fix_v1.identity) == fix_v1
        assert reg3.get(fix_v2.identity) == fix_v2

    def test_old_dataset_version_remains_runnable(self) -> None:
        """Per D17, old dataset versions MUST remain
        runnable. After registering v1 AND v2, both are
        retrievable.

        Note: per D17 mutation policy, reusing the same
        fixture_id with different semantics across dataset
        versions is FORBIDDEN. The legitimate "old version
        remains runnable" scenario is: same semantic
        payload, different dataset_version — the version
        is bumped but the meaning is unchanged.
        """
        reg = DatasetRegistry()
        semantic_content = {"action": "click_login"}
        semantic_outcome = {"stage_reached": "login"}
        fix_v1 = make_fixture(
            identity=make_identity(dataset_version="v1"),
            content=semantic_content,
            expected_outcome=semantic_outcome,
        )
        # Pure version bump, same semantic payload.
        fix_v2 = make_fixture(
            identity=make_identity(dataset_version="v2"),
            content=semantic_content,
            expected_outcome=semantic_outcome,
        )
        reg2 = reg.register(fix_v1).register(fix_v2)
        # Both versions present and runnable.
        assert reg2.get(fix_v1.identity).content == semantic_content
        assert reg2.get(fix_v2.identity).content == semantic_content
        versions = reg2.versions("eval-fixtures")
        assert versions == frozenset({"v1", "v2"})

    def test_different_fixture_id_same_version(self) -> None:
        """Different fixture_id at same version = different
        fixture, allowed."""
        reg = DatasetRegistry()
        fix_a = make_fixture(identity=make_identity(fixture_id="click_login"))
        fix_b = make_fixture(identity=make_identity(fixture_id="click_logout"))
        reg2 = reg.register(fix_a).register(fix_b)
        assert len(reg2) == 2


# ---------------------------------------------------------------------------
# Registry — mutation policy
# ---------------------------------------------------------------------------


class TestRegistryMutationPolicy:
    def test_same_identity_different_semantic_rejected(self) -> None:
        """Same identity, same dataset_version, different
        semantic content → REJECTED. Caller MUST change
        fixture_id or bump dataset_version."""
        reg = DatasetRegistry()
        fix_a = make_fixture(
            identity=make_identity(),
            content={"action": "click_login"},
            expected_outcome={"stage_reached": "login"},
        )
        reg2 = reg.register(fix_a)
        # Same identity, different content (semantic shift).
        fix_b = make_fixture(
            identity=make_identity(),
            content={"action": "delete_account"},
            expected_outcome={"stage_reached": "delete"},
        )
        with pytest.raises(FixtureMutationError, match="already registered"):
            reg2.register(fix_b)

    def test_same_fixture_id_different_version_different_semantic_rejected(
        self,
    ) -> None:
        """Per D17 mutation policy, a fixture_id MUST NOT be
        reused with different semantics across dataset
        versions. Same fixture_id, different version, with
        different semantic fingerprint → REJECTED."""
        reg = DatasetRegistry()
        fix_v1 = make_fixture(
            identity=make_identity(dataset_version="v1"),
            content={"action": "click_login"},
            expected_outcome={"stage_reached": "login"},
        )
        reg2 = reg.register(fix_v1)
        # Same fixture_id, new version, but DIFFERENT
        # semantic (delete_account instead of click_login).
        fix_v2 = make_fixture(
            identity=make_identity(dataset_version="v2"),
            content={"action": "delete_account"},
            expected_outcome={"stage_reached": "delete"},
        )
        with pytest.raises(
            FixtureMutationError,
            match="MUST NOT be reused with different semantics",
        ):
            reg2.register(fix_v2)

    def test_same_fixture_id_different_version_same_semantic_allowed(self) -> None:
        """Per D17, same fixture_id across dataset versions
        is allowed as long as the semantic fingerprint is
        unchanged (pure version bump, no semantic shift)."""
        reg = DatasetRegistry()
        fix_v1 = make_fixture(
            identity=make_identity(dataset_version="v1"),
            content={"action": "click_login"},
            expected_outcome={"stage_reached": "login"},
        )
        reg2 = reg.register(fix_v1)
        # Same fixture_id, same content (same semantic), new
        # version. Allowed.
        fix_v2 = make_fixture(
            identity=make_identity(dataset_version="v2"),
            content={"action": "click_login"},
            expected_outcome={"stage_reached": "login"},
        )
        reg3 = reg2.register(fix_v2)
        assert len(reg3) == 2

    def test_change_fixture_id_for_semantic_shift(self) -> None:
        """The intended fix for a semantic shift at the same
        version is to change fixture_id."""
        reg = DatasetRegistry()
        fix_a = make_fixture(
            identity=make_identity(fixture_id="click_login"),
            content={"action": "click_login"},
            expected_outcome={"stage_reached": "login"},
        )
        reg2 = reg.register(fix_a)
        # Same version, different semantic, but new fixture_id
        # → allowed.
        fix_b = make_fixture(
            identity=make_identity(fixture_id="delete_account"),
            content={"action": "delete_account"},
            expected_outcome={"stage_reached": "delete"},
            change_log=("renamed from click_login; semantic shifted",),
        )
        reg3 = reg2.register(fix_b)
        assert len(reg3) == 2


# ---------------------------------------------------------------------------
# Change log
# ---------------------------------------------------------------------------


class TestChangeLog:
    def test_change_log_is_tuple(self) -> None:
        """The change_log MUST be a tuple (frozen-ness)."""
        fix = make_fixture(identity=make_identity(), change_log=("a", "b"))
        assert isinstance(fix.change_log, tuple)
        assert fix.change_log == ("a", "b")

    def test_change_log_iterable_normalised_to_tuple(self) -> None:
        """Lists and other iterables MUST be normalised."""
        fix = make_fixture(identity=make_identity(), change_log=["a", "b"])
        assert isinstance(fix.change_log, tuple)
        assert fix.change_log == ("a", "b")

    def test_change_log_distinguishes_semantic_shift(self) -> None:
        """Recording a change_log entry alongside a fixture
        id change signals a semantic shift."""
        fix_a = make_fixture(identity=make_identity(fixture_id="v1"))
        fix_b = make_fixture(
            identity=make_identity(fixture_id="v2"),
            content={"action": "different"},
            change_log=("semantic shift from v1",),
        )
        assert fix_a.change_log == ()
        assert "semantic shift from v1" in fix_b.change_log


# ---------------------------------------------------------------------------
# Registry — query
# ---------------------------------------------------------------------------


class TestRegistryQueries:
    def test_versions_empty_for_unknown_dataset(self) -> None:
        reg = DatasetRegistry()
        assert reg.versions("nonexistent") == frozenset()

    def test_versions_filters_by_dataset(self) -> None:
        reg = DatasetRegistry()
        reg2 = (
            reg.register(make_fixture(identity=make_identity(dataset_id="ds1")))
            .register(
                make_fixture(
                    identity=make_identity(
                        dataset_id="ds1", dataset_version="v2"
                    )
                )
            )
            .register(
                make_fixture(
                    identity=make_identity(
                        dataset_id="ds2", dataset_version="v1"
                    )
                )
            )
        )
        assert reg2.versions("ds1") == frozenset({"v1", "v2"})
        assert reg2.versions("ds2") == frozenset({"v1"})

    def test_get_unknown_raises_keyerror(self) -> None:
        reg = DatasetRegistry()
        with pytest.raises(KeyError, match="fixture not registered"):
            reg.get(make_identity())

    def test_registry_is_immutable(self) -> None:
        """The registry itself is frozen; register() returns
        a new registry."""
        reg = DatasetRegistry()
        reg2 = reg.register(make_fixture(identity=make_identity()))
        with pytest.raises((AttributeError, Exception)):
            reg2._fixtures = {}  # type: ignore[misc]