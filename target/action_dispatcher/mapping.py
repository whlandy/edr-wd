"""
mapping.py — Action-ID ↔ backend-method mapping (P1.1, FR-P1.1-01,
FR-P1.1-10).

The 27 catalog entries live in `action_catalog.actions_v1`. Their
`tool_name` is by convention the name of the backend method that
implements the action (architecture §7.3). The dispatcher validates
this assumption at startup by calling `hasattr(backend, tool_name)`;
a missing method surfaces as `dispatch_target_missing`.

The mapping module also exposes the *invalidation policy*: actions
whose `side_effect` is one of `gui_mutation`, `session_mutation`,
or `system_mutation` invalidate observation snapshots after a
known-good backend result; pure observation actions do not.

Stability (architecture §7.2):
  * The dispatch table is derived from the catalog at runtime.
  * Adding a new side-effect category is a MINOR bump.
  * Renaming an existing category is a MAJOR bump.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from action_catalog import ACTIONS_V1, ActionSpec, backend_capability_view


# Side-effect classes that imply "observation snapshots must be
# invalidated after this action completes successfully". Pure
# observation actions are intentionally excluded.
MUTATING_SIDE_EFFECTS: frozenset[str] = frozenset({
    "gui_mutation",
    "session_mutation",
    "system_mutation",
})


def is_mutating(spec: ActionSpec) -> bool:
    """True if `spec.side_effect` implies snapshot invalidation."""
    return spec.side_effect in MUTATING_SIDE_EFFECTS


@dataclass(frozen=True)
class ActionDispatchMap:
    """Action-ID ↔ backend-method mapping (and policy).

    Built once at startup from the V1 catalog. Tests can build a
    smaller map manually for unit tests.
    """

    by_action_id: Mapping[str, ActionSpec]
    by_tool_name: Mapping[str, ActionSpec]

    @classmethod
    def from_catalog(cls) -> "ActionDispatchMap":
        by_id: dict[str, ActionSpec] = {}
        by_tool: dict[str, ActionSpec] = {}
        for spec in ACTIONS_V1:
            if spec.action_id in by_id:
                raise ValueError(
                    f"duplicate action_id {spec.action_id!r} in catalog"
                )
            if spec.tool_name in by_tool:
                raise ValueError(
                    f"duplicate tool_name {spec.tool_name!r} in catalog"
                )
            by_id[spec.action_id] = spec
            by_tool[spec.tool_name] = spec
        return cls(by_action_id=by_id, by_tool_name=by_tool)

    def get_by_action_id(self, action_id: str) -> ActionSpec | None:
        return self.by_action_id.get(action_id)

    def get_by_tool_name(self, tool_name: str) -> ActionSpec | None:
        return self.by_tool_name.get(tool_name)

    def resolve_backend_method(self, backend, action_id: str):
        """Return the bound method on `backend` for `action_id`, or
        None if the backend does not implement it (FR-P1.1-10).

        P1.1 review #1 (mapping API): the routing decision is made
        explicitly on `spec.execution_provider`, NOT via implicit
        `hasattr` semantics. The order is:

          1. unknown action_id                -> None
          2. execution_provider != "backend"  -> None
             (server_inline actions live in target/server.py)
          3. tool_name missing on backend      -> None
             (dispatch_target_missing surface)

        Future dispatch providers (e.g. P1.x's pointer / rect
        primitives) should be added as new execution_provider
        values, not as `hasattr`-driven fallbacks.
        """
        spec = self.get_by_action_id(action_id)
        if spec is None:
            return None
        if spec.execution_provider != "backend":
            # Server-inline actions: target/server.py handles the
            # dispatch. The dispatcher correctly reports
            # dispatch_target_missing so the caller falls back to
            # the legacy tool path.
            return None
        method = getattr(backend, spec.tool_name, None)
        if method is None or not callable(method):
            return None
        return method


def check_backend_coverage(backend_kind: str) -> dict[str, bool]:
    """Return a {action_id: supported} map for the given backend.

    Coverage is computed from the catalog's static
    BACKEND_NOT_IMPLEMENTED table (P0.1 architecture §8.1). A
    backend that is not present in the table at all simply has
    every entry as False.

    Used by tests and diagnostics to assert "every catalog action
    resolves". Server-inline actions (`execution_provider ==
    "server_inline"`) are still flagged True here when the
    backend claims them; `resolve_backend_method` returns None
    for them regardless.
    """
    coverage: dict[str, bool] = {}
    cap = backend_capability_view()
    for spec in ACTIONS_V1:
        # If the catalog says the action is supported on this
        # backend AND the spec's tool_name is registered as
        # enabled for this backend, treat it as covered.
        if backend_kind in spec.backends:
            coverage[spec.action_id] = bool(cap.get(spec.action_id, False))
        else:
            coverage[spec.action_id] = False
    return coverage


__all__ = [
    "MUTATING_SIDE_EFFECTS",
    "is_mutating",
    "ActionDispatchMap",
    "check_backend_coverage",
]