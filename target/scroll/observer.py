"""
observer.py — Observer / diff-engine layer (PR1, enforceability item 6).

`verify()` / `moved` ownership lives here, NOT inside the composite action.
The Observer builds on the *existing* observation machinery
(`target/observations/`) rather than inventing a new snapshot format:

  * `snapshot()` wraps `build_snapshot()` (`target/observations/snapshot.py`)
    and returns the existing `ObservationSnapshot` (`tree_digest`, `targets`).
  * `content_changed(before, after, *, strategy, tolerance)` is the verify
    predicate the composite layer consumes. For `TREE_DIGEST` (the only
    strategy shipped in PR1) it is `before.tree_digest != after.tree_digest`.
  * `diff(...)` returns a `Diff` record (the finer-grained compare output) so
    PR3's `PageVerifier` can get locality when it needs it.

`FINGERPRINT` is implemented here too (a set of per-target `Target.fingerprint`
values) because it is trivial, deterministicable, and gives the locality PR3
wants — but it carries the documented locality caveat (fingerprint is identity-
only, excludes rect; row-recycling virtual lists won't move it). `SCREENSHOT`
is declared but raises `NotImplementedError` because screenshot evidence does
not exist in the tree yet (per design item 6: "a screenshot-digest diff strategy
is a *proposed* extension ... not present today"). No dead branches are shipped.

`churn` handling is intentionally absent in PR1: for a single-scalar
`TREE_DIGEST` you cannot distinguish a spinner reflow from real content
movement, so churn defaults to `False`. PR3's `PageVerifier`/`tolerance` is the
churn disambiguation knob (documented limitation, design item 6 open risk).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional, Union

try:
    from ..observations.models import ObservationSnapshot, Target
    from ..observations.snapshot import build_snapshot
except ImportError:
    from observations.models import ObservationSnapshot, Target
    from observations.snapshot import build_snapshot


# A single digest (TREE_DIGEST/SCREENSHOT) or a set of per-target
# fingerprints (FINGERPRINT). Frozen-friendly: passthrough as-is.
DigestRef = Union[str, "frozenset[str]"]


class DiffStrategy(Enum):
    """Enum over *how* we compare two snapshots, not a new schema."""

    TREE_DIGEST = "tree_digest"      # ObservationSnapshot.tree_digest equality
    FINGERPRINT = "fingerprint"      # set of per-target Target.fingerprint
    SCREENSHOT = "screenshot"        # proposed; requires P1.4 screenshot evidence


@dataclass(frozen=True)
class Diff:
    """Observer output; comparison inputs are strategy-dependent.

    Fields:
        changed: bool               — a content change was detected
        strategy: DiffStrategy      — the strategy that produced this verdict
        before: DigestRef           — TREE_DIGEST/SCREENSHOT: scalar digest;
                                      FINGERPRINT: set of fingerprints
        after: DigestRef            — same shape as `before`
        churn: bool = False         — True ⇒ change attributed to transients,
                                      not content (always False in PR1)
    """

    changed: bool
    strategy: DiffStrategy
    before: DigestRef
    after: DigestRef
    churn: bool = False


# Signature of the snapshot source. Production wires this to a backend
# dump_tree→target-dicts collector; tests pass a fixed list or a stub.
TargetsFn = Callable[[], list[dict]]


class Observer:
    """Observer / diff-engine. Thin, decoupled from MCP transport."""

    def __init__(
        self,
        *,
        targets_fn: Optional[TargetsFn] = None,
        backend: str = "windows_pywinauto",
        host: str = "local",
    ) -> None:
        """
        Args:
            targets_fn: optional callable returning the pre-collected target
                dicts in the backend's stable traversal order. If provided,
                `snapshot()` builds a live `ObservationSnapshot` from it. If
                omitted, `snapshot()` raises `ValueError` (callers that only
                need `content_changed` on two hand-built snapshots do not need
                a live source).
            backend / host: passed through to `build_snapshot`.
        """
        self._targets_fn = targets_fn
        self._backend = backend
        self._host = host

    # ---- snapshot ---------------------------------------------------------

    def snapshot(self) -> ObservationSnapshot:
        """Build a live `ObservationSnapshot` from the configured target
        source. Deterministic: two calls with no UI change produce equal
        `tree_digest` (byte-stable, per `fingerprint.py`)."""
        if self._targets_fn is None:
            raise ValueError(
                "Observer.snapshot() requires a targets_fn; pass one to the "
                "constructor or compare pre-built snapshots via content_changed()."
            )
        targets = self._targets_fn()
        if not targets:
            raise ValueError("Observer.snapshot(): targets_fn returned no targets")
        import datetime
        captured_at = datetime.datetime.now(
            datetime.timezone.utc
        ).isoformat()
        return build_snapshot(
            targets=targets,
            backend=self._backend,
            host=self._host,
            captured_at=captured_at,
        )

    # ---- diff / verify -----------------------------------------------------

    def diff(
        self,
        before: ObservationSnapshot,
        after: ObservationSnapshot,
        *,
        strategy: DiffStrategy = DiffStrategy.TREE_DIGEST,
    ) -> Diff:
        """Compare two snapshots under the explicitly-selected `strategy`.

        The strategy is never auto-detected; the caller passes it and it must
        be valid for the given inputs (a non-`ObservationSnapshotIn` value
        raises). Returns a `Diff` verdict independent of the action that
        dispatched the input.
        """
        if not isinstance(before, ObservationSnapshot) or not isinstance(
            after, ObservationSnapshot
        ):
            raise TypeError("diff() requires ObservationSnapshot instances")
        if strategy is DiffStrategy.TREE_DIGEST:
            return Diff(
                changed=before.tree_digest != after.tree_digest,
                strategy=strategy,
                before=before.tree_digest,
                after=after.tree_digest,
            )
        if strategy is DiffStrategy.FINGERPRINT:
            bset = frozenset(t.fingerprint for t in before.targets)
            aset = frozenset(t.fingerprint for t in after.targets)
            return Diff(
                changed=bset != aset,
                strategy=strategy,
                before=bset,
                after=aset,
            )
        if strategy is DiffStrategy.SCREENSHOT:
            raise NotImplementedError(
                "SCREENSHOT diff strategy requires screenshot evidence (P1.4); "
                "not available"
            )
        raise ValueError(f"unsupported diff strategy {strategy!r}")

    def content_changed(
        self,
        before: ObservationSnapshot,
        after: ObservationSnapshot,
        *,
        strategy: DiffStrategy = DiffStrategy.TREE_DIGEST,
        tolerance: Optional[float] = None,
    ) -> bool:
        """Return True iff the chosen diff strategy reports a change.

        `tolerance: None` ⇒ "any change counts" (item-1 default). In PR1 a
        positive epsilon is accepted for signature-compatibility but cannot
        filter a scalar `tree_digest` — it is ignored for `TREE_DIGEST` and
        documented (the churn/tolerance knob is finalized in PR3). `churn`
        never propagates to `True` here (churn is always False in PR1).
        """
        d = self.diff(before, after, strategy=strategy)
        if d.churn:
            return False
        return d.changed


__all__ = [
    "DiffStrategy",
    "Diff",
    "DigestRef",
    "Observer",
    "TargetsFn",
]
