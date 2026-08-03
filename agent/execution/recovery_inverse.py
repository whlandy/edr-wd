"""recovery_inverse.py — P2.2 SOP inverse-action registry (Commit A).

Each SOP that wants to be ``restorable=True`` at
``application_state`` level must register an inverse action set:

    * which action ids to run, in order, to bring the system
      back to the SOP's pre-state
    * what the SOP expects to observe afterwards (validated
      against ``expected_after_sop_id``)
    * a wall-clock timeout for the inverse sequence

P2.2 keeps this registry in-memory (a process-local dict). P2.4
production hardening may move to a config file; for now the
unit tests build a registry per-test and the executor wires one
per run.

Design doc: ``docs/requirements/P2-recovery-planner.md`` §4.1
(``agent.execution.recovery_inverse`` module layout).

Boundary:

    * This module is **data only** — it has no logic for executing
      the inverse actions. The executor (``agent.execution.executor``)
      consumes an ``SOPInverseAction`` and dispatches its
      ``inverse_action_ids`` in order.
    * The planner (``agent.execution.recovery`` Commit B) consumes
      the registry to look up which actions to plan for.
"""

from __future__ import annotations

from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# SOPInverseAction
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SOPInverseAction:
    """Inverse-action declaration for one SOP.

    Fields:

        * ``sop_id`` — the SOP being made restorable (matches
          ``SopIndexEntry.sop_id`` from P2.1).
        * ``expected_after_sop_id`` — the SOP whose presence in
          the post-restore snapshot validates the inverse ran
          correctly. ``None`` means "no SOP validator" (the
          executor will treat restore as successful if no error
          surfaced from the inverse actions themselves).
        * ``inverse_action_ids`` — ordered list of catalog action
          ids to dispatch in sequence. The order matters: SOPs
          that depend on a clean window must list window-reset
          actions before content-navigation actions.
        * ``timeout_s`` — wall-clock ceiling for the whole
          sequence; default 30 s is generous for V1 backends.
        * ``tags`` — free-form tags used by the planner for
          trace-side filtering (e.g., ``"windows_only"``,
          ``"requires_admin"``). The planner does not currently
          consume them but they are kept on the dataclass so
          future rule lists can filter without a schema change.
    """

    sop_id: str
    inverse_action_ids: tuple[str, ...] = field(default_factory=tuple)
    expected_after_sop_id: str | None = None
    timeout_s: float = 30.0
    tags: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        if not self.sop_id:
            raise ValueError("sop_id must be a non-empty string")
        if self.timeout_s <= 0:
            raise ValueError(
                f"timeout_s must be > 0, got {self.timeout_s}"
            )
        if any(not a for a in self.inverse_action_ids):
            raise ValueError(
                "inverse_action_ids must not contain empty strings"
            )


# ---------------------------------------------------------------------------
# InverseRegistry
# ---------------------------------------------------------------------------


class InverseRegistry:
    """Process-local SOP inverse registry.

    The registry is intentionally **simple**: a dict with
    add/get/has semantics, plus the SOP-id enforcement used
    in P2.1's ``SopIndex``. No transactions, no locking — the
    executor builds one per run, populates it, and reads it
    from a single thread.

    Construction:

        >>> reg = InverseRegistry()
        >>> reg.register(SOPInverseAction(
        ...     sop_id="edrclient.main",
        ...     inverse_action_ids=("edrclient.relaunch",),
        ...     expected_after_sop_id="edrclient.main",
        ... ))

    Lookup:

        >>> reg.get("edrclient.main")
        SOPInverseAction(sop_id='edrclient.main', ...)
        >>> reg.get("nonexistent") is None
        True
    """

    def __init__(self) -> None:
        self._by_id: dict[str, SOPInverseAction] = {}

    def register(self, inverse: SOPInverseAction) -> None:
        """Register one inverse; rejects duplicates with a
        first-vs-second path-style message (Round 2 review
        hardening carried over from P2.1 ``SopIndex``)."""
        existing = self._by_id.get(inverse.sop_id)
        if existing is not None:
            raise DuplicateInverseError(
                f"duplicate inverse sop_id={inverse.sop_id!r}: "
                f"first={existing} second={inverse}"
            )
        self._by_id[inverse.sop_id] = inverse

    def get(self, sop_id: str) -> SOPInverseAction | None:
        """Return the registered inverse, or ``None`` if absent."""
        return self._by_id.get(sop_id)

    def has(self, sop_id: str) -> bool:
        """Cheap membership check used by ``plan_recovery``."""
        return sop_id in self._by_id

    def __len__(self) -> int:
        return len(self._by_id)

    def __contains__(self, sop_id: object) -> bool:
        # ``sop_id`` is a string in practice, but accept any object
        # so ``in registry`` is safe against non-strings.
        return isinstance(sop_id, str) and sop_id in self._by_id


class DuplicateInverseError(ValueError):
    """Raised when registering two inverses for the same ``sop_id``.

    Distinct from ``agent.execution.sop_index.DuplicateSOPIdError``
    so that callers can differentiate between SOP-index duplication
    (P2.1) and inverse-registry duplication (P2.2) if they need to.
    """


__all__ = [
    "SOPInverseAction",
    "InverseRegistry",
    "DuplicateInverseError",
]