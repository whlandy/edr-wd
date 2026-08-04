"""
datasets.py — P3.2.A dataset contract and fixture registry.

Implements P3.2 design gate contract **D17** (Dataset identity
and versioning contract):

    * Every fixture carries an explicit identity triple
      `(dataset_id, dataset_version, fixture_id)`.
    * The identity is content-stable (renaming files does
      not change identity).
    * The identity is embedded in the report (per D18, done
      in P3.2.B).
    * Old dataset versions remain runnable for at least one
      major P3.2 release after supersession.
    * Mutation policy: changing fixture semantics requires
      a `fixture_id` change OR a `dataset_version` bump.
      Pure content changes (typos, formatting) MAY keep the
      same identity.
    * A `fixture_id` MUST NOT be reused with different
      semantics across dataset versions. Once a `fixture_id`
      semantically meant X, it MUST never mean Y, even in a
      later dataset version.

Public API:

    * FixtureIdentity — frozen dataclass, the triple.
    * Fixture — frozen dataclass, identity + content +
      expected outcome + change log.
    * FixtureMutationError — raised on policy violation.
    * DatasetRegistry — versioned registry; old versions
      remain runnable.
    * semantic_fingerprint — pure function, hashes the
      semantic payload (content + expected_outcome),
      ignoring metadata.

The registry is in-memory and pure. Persistence (loading
from disk) is P3.2.B scope (D18 + loaders); P3.2.A only
locks the contract surface.
"""

from dataclasses import dataclass, field
from hashlib import sha256
from json import dumps, loads
from typing import Any, FrozenSet, Mapping


# Sentinel for "no exception" path; keeps test assertions
# concise.
OK = "ok"


@dataclass(frozen=True)
class FixtureIdentity:
    """Explicit identity triple per D17.

    The identity MUST be content-stable: renaming a fixture
    file MUST NOT change the identity. This is enforced by
    carrying the triple as data, not by deriving from a path.
    """

    dataset_id: str
    dataset_version: str
    fixture_id: str

    def __post_init__(self) -> None:
        if not self.dataset_id:
            raise ValueError("dataset_id MUST be non-empty")
        if not self.dataset_version:
            raise ValueError("dataset_version MUST be non-empty")
        if not self.fixture_id:
            raise ValueError("fixture_id MUST be non-empty")


@dataclass(frozen=True)
class Fixture:
    """A single dataset fixture.

    `content` is the input the runner receives (a planner
    request, a snapshot reference, a fixture-local config).

    `expected_outcome` is the structured expectation the
    runner compares against (metric values, plan ids,
    stage-reached markers).

    `change_log` records every semantic shift that triggered
    a `fixture_id` change OR a `dataset_version` bump. Pure
    content edits (typos, formatting) MUST NOT appear here.
    """

    identity: FixtureIdentity
    content: Mapping[str, Any]
    expected_outcome: Mapping[str, Any]
    change_log: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        # The change_log MUST be a tuple for frozen-ness; we
        # accept any iterable and normalise.
        if not isinstance(self.change_log, tuple):
            object.__setattr__(self, "change_log", tuple(self.change_log))


def semantic_fingerprint(
    content: Mapping[str, Any],
    expected_outcome: Mapping[str, Any],
) -> str:
    """Hash of the semantic payload.

    The fingerprint MUST be deterministic and MUST depend
    only on the semantic payload (content + expected
    outcome). Metadata like identity, change_log, or
    timestamps MUST NOT influence the hash; otherwise two
    fixtures with the same semantic payload would compare
    as different.

    Returns a 64-character hex string (SHA-256 truncated to
    the implementation choice — see D17 implementation
    freedom).
    """

    payload = dumps(
        (_canonical(content), _canonical(expected_outcome)),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256(payload).hexdigest()


def _canonical(value: Any) -> Any:
    """Recursively canonicalise a value for hashing.

    Tuples are converted to lists (JSON cannot round-trip
    tuples); frozen sets are sorted then converted. The
    result MUST round-trip through json.dumps / json.loads
    without losing semantic information.
    """
    if isinstance(value, (list, tuple)):
        return [_canonical(v) for v in value]
    if isinstance(value, (set, frozenset)):
        return sorted(_canonical(v) for v in value)
    if isinstance(value, Mapping):
        return {k: _canonical(v) for k, v in sorted(value.items())}
    return value


class FixtureMutationError(ValueError):
    """Raised when a registration violates the mutation policy.

    The error message MUST identify the violation (which
    fixture, why it was rejected) so audit logs are
    actionable.
    """

    def __init__(self, message: str, *, identity: FixtureIdentity) -> None:
        super().__init__(message)
        self.identity = identity


@dataclass(frozen=True)
class DatasetRegistry:
    """In-memory versioned registry.

    Stores fixtures keyed by `FixtureIdentity`. Old dataset
    versions are NOT evicted; they remain runnable. The
    registry enforces the mutation policy at registration
    time.

    The registry is intentionally simple — it does NOT
    load fixtures from disk, does NOT serialise, and does
    NOT provide query interfaces beyond the cross-cutting
    ones below. Those concerns are P3.2.B scope (D18).
    """

    _fixtures: Mapping[FixtureIdentity, Fixture] = field(default_factory=dict)

    def register(self, fixture: Fixture) -> "DatasetRegistry":
        """Register a fixture, enforcing the mutation policy.

        Returns a new registry (the input is frozen). The
        caller is expected to keep the returned registry
        and discard the old one.

        Mutation policy:
          1. If `fixture.identity` is already registered
             with the same semantic fingerprint, the new
             fixture is a no-op (returns the same registry).
          2. If `fixture.identity` is already registered
             with a different semantic fingerprint and the
             same dataset_version, the mutation is rejected
             (caller MUST change fixture_id OR bump version).
          3. If `fixture.identity` is already registered
             with a different semantic fingerprint and a
             different dataset_version, the mutation is
             rejected (fixture_id reused with different
             semantics across dataset versions, forbidden
             by D17 mutation policy).
          4. Otherwise, the fixture is added.
        """
        existing = self._fixtures.get(fixture.identity)
        if existing is not None:
            # Same identity: semantic must match.
            existing_fp = semantic_fingerprint(
                existing.content, existing.expected_outcome
            )
            new_fp = semantic_fingerprint(
                fixture.content, fixture.expected_outcome
            )
            if existing_fp == new_fp:
                return self  # no-op
            # Different semantic under same identity →
            # mutation policy violation.
            raise FixtureMutationError(
                f"fixture {fixture.identity!r} already registered with "
                f"different semantics; change fixture_id or bump "
                f"dataset_version. existing_fp={existing_fp}, new_fp={new_fp}",
                identity=fixture.identity,
            )

        # Cross-version check: if (dataset_id, fixture_id)
        # appears in another dataset_version with a
        # different semantic fingerprint, the new registration
        # is rejected.
        cross_version_match = self._find_cross_version(fixture)
        if cross_version_match is not None:
            existing = cross_version_match
            existing_fp = semantic_fingerprint(
                existing.content, existing.expected_outcome
            )
            new_fp = semantic_fingerprint(
                fixture.content, fixture.expected_outcome
            )
            if existing_fp != new_fp:
                raise FixtureMutationError(
                    f"fixture_id {fixture.identity.fixture_id!r} in "
                    f"dataset_id={fixture.identity.dataset_id!r} is "
                    f"registered in dataset_version="
                    f"{existing.identity.dataset_version!r} with different "
                    f"semantics; fixture_id MUST NOT be reused with "
                    f"different semantics across dataset versions. "
                    f"existing_fp={existing_fp}, new_fp={new_fp}",
                    identity=fixture.identity,
                )

        new_map = dict(self._fixtures)
        new_map[fixture.identity] = fixture
        return DatasetRegistry(_fixtures=new_map)

    def _find_cross_version(self, fixture: Fixture) -> Fixture | None:
        """Find a fixture with the same `(dataset_id, fixture_id)`
        but a different `dataset_version`.

        Returns the most recent (by insertion order) such
        fixture, or None if there is none.
        """
        match: Fixture | None = None
        for existing_identity, existing_fixture in self._fixtures.items():
            if (
                existing_identity.dataset_id
                == fixture.identity.dataset_id
                and existing_identity.fixture_id
                == fixture.identity.fixture_id
                and existing_identity.dataset_version
                != fixture.identity.dataset_version
            ):
                match = existing_fixture
        return match

    def get(self, identity: FixtureIdentity) -> Fixture:
        """Look up a fixture by identity.

        Raises KeyError if the fixture is not registered.
        """
        if identity not in self._fixtures:
            raise KeyError(f"fixture not registered: {identity!r}")
        return self._fixtures[identity]

    def versions(self, dataset_id: str) -> FrozenSet[str]:
        """Return the set of registered `dataset_version`s
        for the given `dataset_id`.

        Empty set if no fixtures are registered.
        """
        return frozenset(
            identity.dataset_version
            for identity in self._fixtures
            if identity.dataset_id == dataset_id
        )

    def __len__(self) -> int:
        return len(self._fixtures)

    def __contains__(self, identity: object) -> bool:
        return identity in self._fixtures


__all__ = [
    "Fixture",
    "FixtureIdentity",
    "FixtureMutationError",
    "DatasetRegistry",
    "semantic_fingerprint",
]