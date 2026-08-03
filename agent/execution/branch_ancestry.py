"""branch_ancestry.py — Branch lineage queries (P2.2 — Commit F).

Round 2 review (#4: branch ancestry) requires the executor
to carry branch lineage (parent_branch_id) on every event and
to support queries:

    * :func:`root` — find the root branch by walking parents.
    * :func:`depth` — depth in the lineage tree (root = 0).
    * :func:`lineage` — root -> leaf order tuple.
    * :func:`is_ancestor` — is ``a`` an ancestor of ``b``?

The :class:`BranchRegistry` is an in-memory store of
:class:`Branch` records that supports these queries. It is
designed to be **append-only** — once a branch is registered,
its ``parent_branch_id`` is immutable. Cycles are detected
and rejected (raises :class:`BranchCycleError`).

Boundary:

    * This module does **not** import :class:`TraceStore`.
    * This module does **not** import :class:`RecoveryExecutor`.
    * Branch creation is the caller's responsibility; this
      module only records and queries lineage.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from agent.execution.recovery import Branch


class BranchCycleError(ValueError):
    """Raised when registering a branch would create a cycle."""


class BranchNotFoundError(KeyError):
    """Raised when querying a branch that is not registered."""


# ---------------------------------------------------------------------------
# BranchRegistry
# ---------------------------------------------------------------------------


@dataclass
class BranchRegistry:
    """Append-only registry of :class:`Branch` records.

    Use::

        registry = BranchRegistry()
        registry.register(Branch(branch_id="BR-001", ...))
        registry.register(Branch(branch_id="BR-002", parent_branch_id="BR-001", ...))

        registry.depth("BR-002")  # 1
        registry.root("BR-002")   # "BR-001"
        registry.lineage("BR-002") # ("BR-001", "BR-002")
    """

    _branches: dict[str, Branch] = field(default_factory=dict, init=False, repr=False)

    def register(self, branch: Branch) -> None:
        """Add ``branch`` to the registry.

        Raises:
            BranchCycleError: if registering would create a
                cycle in the lineage graph.
            ValueError: if ``parent_branch_id`` is set but
                the parent branch is not registered.
        """
        if branch.parent_branch_id is not None:
            if branch.parent_branch_id not in self._branches:
                raise ValueError(
                    f"parent branch {branch.parent_branch_id!r} not registered"
                )
            # Cycle check: ensure branch_id doesn't appear in
            # the parent lineage.
            try:
                parent_lineage = self.lineage(branch.parent_branch_id)
            except BranchNotFoundError:
                raise ValueError(
                    f"parent branch {branch.parent_branch_id!r} not registered"
                )
            if branch.branch_id in parent_lineage:
                raise BranchCycleError(
                    f"registering {branch.branch_id!r} would create a cycle"
                )
        self._branches[branch.branch_id] = branch

    def get(self, branch_id: str) -> Branch:
        """Return the :class:`Branch` for ``branch_id``.

        Raises:
            BranchNotFoundError: if ``branch_id`` is not registered.
        """
        try:
            return self._branches[branch_id]
        except KeyError as e:
            raise BranchNotFoundError(branch_id) from e

    def has(self, branch_id: str) -> bool:
        return branch_id in self._branches

    # -----------------------------------------------------------------
    # Queries
    # -----------------------------------------------------------------

    def root(self, branch_id: str) -> str:
        """Return the root branch id for the lineage containing ``branch_id``.

        Walks ``parent_branch_id`` until it reaches a branch
        with no parent.
        """
        seen: set[str] = set()
        current = branch_id
        while True:
            if current in seen:
                # Defensive: should be impossible due to
                # cycle check on register, but handle gracefully.
                raise BranchCycleError(
                    f"lineage cycle detected at {current!r}"
                )
            seen.add(current)
            branch = self.get(current)
            if branch.parent_branch_id is None:
                return branch.branch_id
            current = branch.parent_branch_id

    def depth(self, branch_id: str) -> int:
        """Return the depth of ``branch_id`` in the lineage tree.

        Root branches have depth ``0``.
        """
        seen: set[str] = set()
        current = branch_id
        depth = 0
        while True:
            if current in seen:
                raise BranchCycleError(
                    f"lineage cycle detected at {current!r}"
                )
            seen.add(current)
            branch = self.get(current)
            if branch.parent_branch_id is None:
                return depth
            current = branch.parent_branch_id
            depth += 1

    def lineage(self, branch_id: str) -> tuple[str, ...]:
        """Return the lineage of ``branch_id`` from root to leaf.

        Example: if BR-002 was forked from BR-001, then
        ``lineage("BR-002") == ("BR-001", "BR-002")``.
        """
        ancestors = self._ancestors_inclusive(branch_id)
        # ancestors are returned leaf-first; reverse to root-first.
        return tuple(reversed(ancestors))

    def is_ancestor(self, ancestor_id: str, descendant_id: str) -> bool:
        """Return ``True`` if ``ancestor_id`` is an ancestor of ``descendant_id``.

        A branch is its own ancestor for this check's purposes
        (``is_ancestor("BR-001", "BR-001") == True``); use
        ``lineage()`` if you need the inclusive check to exclude
        the branch itself.
        """
        lineage = self._ancestors_inclusive(descendant_id)
        return ancestor_id in lineage

    def children(self, branch_id: str) -> tuple[str, ...]:
        """Return the direct children of ``branch_id`` (branches
        whose ``parent_branch_id`` is ``branch_id``).

        Returns ``()`` if ``branch_id`` has no children.
        """
        return tuple(
            b.branch_id
            for b in self._branches.values()
            if b.parent_branch_id == branch_id
        )

    # -----------------------------------------------------------------
    # Internal helpers
    # -----------------------------------------------------------------

    def _ancestors_inclusive(self, branch_id: str) -> tuple[str, ...]:
        """Walk parents from ``branch_id`` to root, inclusive.

        Returns leaf-first order (``(branch_id, ..., root)``).
        Raises :class:`BranchCycleError` if a cycle is detected.
        """
        seen: set[str] = set()
        chain: list[str] = []
        current = branch_id
        while True:
            if current in seen:
                raise BranchCycleError(
                    f"lineage cycle detected at {current!r}"
                )
            seen.add(current)
            chain.append(current)
            branch = self.get(current)
            if branch.parent_branch_id is None:
                return tuple(chain)
            current = branch.parent_branch_id


__all__ = [
    "BranchRegistry",
    "BranchCycleError",
    "BranchNotFoundError",
]