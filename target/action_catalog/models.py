"""
models.py — Catalog wire models (P0.1).

Two dataclasses only:

* `ActionSpec` — static metadata for one action. Immutable.
* `ActionEntry` — `ActionSpec` plus its live enablement for a specific
  backend, decided at the call site. Live enablement is **read-only
  metadata** here, not a dispatcher decision (P0.1 owns "which actions
  exist"; P1.1 owns "how to call them").

`execution_provider` distinguishes the two dispatch paths:

* `"backend"` — the action maps to a backend method. The P1.1
  dispatcher will resolve `tool_name` to a callable on the backend
  instance.
* `"server_inline"` — the action is implemented inline in
  `target/server.py` (currently only `restore_edr`). The dispatcher
  route lives in server.py, not on a backend. The catalog still owns
  the action's metadata, its tool name, its enablement, and its
  status.action_space bit.

This split means the catalog remains the single source of truth even
when the dispatch implementation lives outside the backend abstraction.

Stability rules (architecture §7.2):

* Adding an action with the same semantics → minor bump.
* Adding a new optional field to `ActionSpec` → patch bump (consumers
  must `additionalProperties: false`-tolerant).
* Removing or renaming an action_id / action_code / tool_name /
  changing the accepted semantics of an existing action → major bump.
* Removing an enum value → major bump.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping


@dataclass(frozen=True)
class ActionSpec:
    """Static metadata for one catalog entry. Immutable."""

    action_id: str
    action_code: str | None
    tool_name: str
    description: str
    category: str
    input_schema: Mapping[str, object]
    result_schema: Mapping[str, object]
    backends: tuple[str, ...]
    requires: tuple[str, ...]
    side_effect: str
    risk: str
    rollback_class: str
    default_screenshot: str
    transition_policy: str
    preferred_over: tuple[str, ...] = field(default_factory=tuple)
    execution_provider: str = "backend"


@dataclass(frozen=True)
class ActionEntry:
    """A catalog entry with its live enablement for a specific backend.

    `enabled` and `disabled_reason` are computed by the registry's
    `live_enablement_for(backend)` call. They are not stored on the
    spec itself.
    """

    spec: ActionSpec
    enabled: bool
    disabled_reason: str = ""


class CatalogConstructionError(ValueError):
    """Raised at registry construction for invariant violations."""

    def __init__(self, code: str, message: str, *, value: str | None = None):
        self.code = code
        self.value = value
        super().__init__(message)


__all__ = ["ActionSpec", "ActionEntry", "CatalogConstructionError"]