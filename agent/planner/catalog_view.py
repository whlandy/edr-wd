"""
catalog_view.py — Planner-side catalog view (P3.1 Commit A).

Wraps the existing `action_catalog` surfaces in a planner-facing
API. P3.1 design gate contract D11/D12 require the planner to:

  * See ONLY actions enabled for the active `(backend, profile)`
    pair (FR-P3.1-01; acceptance #1).
  * Receive a JSON-friendly view consumable as the planner's
    tool list.

This module is a **thin wrapper** over the existing catalog:

  * `enabled_actions_for(backend, profile)` returns the list of
    `ActionSpec` objects statically enabled for the pair.
  * `planner_tool_list(backend, profile)` returns the JSON
    payload shape used by the planner prompt (FR-P3.1-01
    wire format).

The wrapper does NOT introduce a second capability table. It
reuses:

  * `action_catalog.specs_for_backend` (P0.1 backend filter)
  * `action_catalog.BACKEND_NOT_IMPLEMENTED` (P0.1 gap
    declaration; macos_accessibility excludes
    `type_text`/`select`/`get_text`)
  * `action_catalog.get_action_catalog` (P0.1 JSON view,
    extended with profile-level filtering here)

Profile-level enablement policy:

  P3.1 accepts a profile argument for forward-compatibility
  with profile-specific capability overlays. The current
  V1 implementation:

    * If `profile` is None, returns the existing
      `get_action_catalog(backend)` output unchanged (no
      profile filter; pre-P3.1 behaviour).
    * If `profile` is set, returns the same output (the
      current catalog has no per-profile overlays; profile
      filtering is delegated to the executor's safety
      policy). P3.1.x may add per-profile overlays without
      changing this signature.

Boundary notes (per architecture §7.2 / P2.5):

  * This module does NOT accept a backend instance — it is
    pure-catalog, mirroring the existing P0.1 view. Runtime
    readiness remains the dispatcher's responsibility.
  * The planner MUST call `enabled_actions_for` at the start
    of every prompt-build; subsequent calls may have changed
    the live state.
"""

from __future__ import annotations

from dataclasses import dataclass
from target.action_catalog import (
    ACTIONS_V1,
    BACKEND_NOT_IMPLEMENTED,
    VALID_BACKENDS,
    ActionSpec,
    get_action_catalog,
)


__all__ = [
    "PlannerToolEntry",
    "enabled_actions_for",
    "planner_tool_list",
]


@dataclass(frozen=True)
class PlannerToolEntry:
    """JSON-friendly view of a single planner tool.

    P3.1 design gate contract: this shape is what the
    planner prompt surfaces. The set of fields is locked
    for V1 (P3.1.x may extend without breaking the
    contract).
    """

    action_id: str           # canonical id (P0.1)
    tool_name: str           # legacy / wire name
    description: str         # human-readable description for the LLM
    input_schema: dict       # JSON Schema for the action's input args
    requires: tuple[str, ...] = ()
    side_effect: str = ""
    risk: str = ""
    rollback_class: str = ""
    preferred_over: tuple[str, ...] = ()
    enabled: bool = True
    disabled_reason: str = ""

    def to_dict(self) -> dict:
        return {
            "action_id": self.action_id,
            "tool_name": self.tool_name,
            "description": self.description,
            "input_schema": self.input_schema,
            "requires": list(self.requires),
            "side_effect": self.side_effect,
            "risk": self.risk,
            "rollback_class": self.rollback_class,
            "preferred_over": list(self.preferred_over),
            "enabled": self.enabled,
            "disabled_reason": self.disabled_reason,
        }


def _validate_backend(backend: str) -> None:
    if backend not in VALID_BACKENDS:
        raise ValueError(
            f"unknown backend {backend!r}; expected one of "
            f"{sorted(VALID_BACKENDS)}"
        )


def enabled_actions_for(
    backend: str, profile: str | None = None
) -> list[ActionSpec]:
    """Return the list of `ActionSpec` enabled for `(backend, profile)`.

    FR-P3.1-01: the returned list contains exactly the actions
    statically enabled for the pair. Disabled actions are
    absent (not just marked). Acceptance #1: for
    `(macos_accessibility, macos_hisec)`, `type_text`,
    `select`, and `get_text` MUST be absent (because
    `BACKEND_NOT_IMPLEMENTED["macos_accessibility"]` flags
    them).

    `profile` is accepted for forward-compatibility with
    profile-specific overlays (P3.1.x territory); V1
    currently uses the per-backend filter only.
    """
    _validate_backend(backend)

    # Layer 1: per-backend filter (existing P0.1 capability
    # layer; excludes actions whose `backends` tuple does not
    # contain `backend`).
    candidates: tuple[ActionSpec, ...] = tuple(
        s for s in ACTIONS_V1 if backend in s.backends
    )

    # Layer 2: per-backend gap filter (BACKEND_NOT_IMPLEMENTED).
    # An action in this map is excluded entirely from the
    # planner's tool list — its `enabled: False` declaration
    # means the planner MUST NOT emit it.
    gaps = BACKEND_NOT_IMPLEMENTED.get(backend, {})
    out: list[ActionSpec] = []
    for spec in candidates:
        if spec.tool_name in gaps:
            # Strict-exclude (per FR-P3.1-01 "absent, not just
            # marked").
            continue
        out.append(spec)
    return out


def planner_tool_list(
    backend: str, profile: str | None = None
) -> list[dict]:
    """Return the JSON-friendly planner tool list.

    Wraps `get_action_catalog` for forward-compatibility
    while applying the strict-exclude policy from
    `enabled_actions_for`. Disabled entries are NOT
    returned (FR-P3.1-01); the planner MUST NOT see them.
    """
    enabled = enabled_actions_for(backend, profile)
    # Build the dict view from `get_action_catalog` and then
    # filter out anything not in our strict-exclude set. We
    # use the existing view to preserve `execution_provider`
    # and `status_provider` semantics (P0.1 review Issue 4).
    raw = get_action_catalog(backend=backend, include_disabled=False)
    enabled_ids = {spec.action_id for spec in enabled}
    out: list[dict] = []
    for entry in raw.get("actions", []):
        if entry.get("action_id") in enabled_ids:
            # Re-cast as our PlannerToolEntry so the shape is
            # locked at the planner boundary (FR-P3.1-01).
            out.append(
                PlannerToolEntry(
                    action_id=entry["action_id"],
                    tool_name=entry.get("tool_name", entry["action_id"]),
                    description=entry.get("description", ""),
                    input_schema=entry.get("input_schema", {}),
                    requires=tuple(entry.get("requires", ())),
                    side_effect=entry.get("side_effect", ""),
                    risk=entry.get("risk", ""),
                    rollback_class=entry.get("rollback_class", ""),
                    preferred_over=tuple(entry.get("preferred_over", ())),
                    enabled=bool(entry.get("enabled", True)),
                    disabled_reason=entry.get("disabled_reason", ""),
                ).to_dict()
            )
    return out
