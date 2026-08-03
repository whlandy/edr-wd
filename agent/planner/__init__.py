"""
__init__.py — Public surface of agent/planner (P3.1).

Layering:

    target/protocol_models  — wire models + validator
    target/action_catalog  — canonical action table
    target/action_dispatcher — dispatch + receipts
    agent/execution         — AtomicExecutor + state machine
    agent/trace             — events, evidence, projections
    agent/planner           — THIS package (P3.1)

Public re-exports (P3.1 Commit A + B + C):

  * `enabled_actions_for` / `planner_tool_list` —
    planner-side catalog view (Commit A).
  * `PLAN_JSON_SCHEMA` / `SCHEMA_VERSION` /
    `LLMPlanRequest` / `LLMPlanResponse` / `export_schema` /
    `known_versions` — wire schema + request/response
    (Commit B).
  * `parse_plan` / `ParseResult` / `ValidationError` /
    `STABLE_VALIDATION_CODES` — 5-stage plan validation
    (Commit C).

Subsequent P3.1 commits (D = persist, E = confirmation,
F = post_step, G = prompt) add to this surface.
"""

from __future__ import annotations

from .catalog_view import (
    PlannerToolEntry,
    enabled_actions_for,
    planner_tool_list,
)
from .parse import (
    COORDINATE_FALLBACK_NOT_ALLOWED,
    CODE_ACTION_CODE_MISMATCH,
    CODE_BACKEND_DISABLED,
    CODE_DEPENDENCY_CYCLE,
    CODE_PLAN_SCHEMA_INVALID,
    CODE_TARGET_REF_SNAPSHOT_MISMATCH,
    CODE_UNKNOWN_SCHEMA_VERSION,
    ParseResult,
    STABLE_VALIDATION_CODES,
    ValidationError,
    parse_plan,
)
from .schema import (
    LLMPlanRequest,
    LLMPlanResponse,
    PLAN_JSON_SCHEMA,
    SCHEMA_VERSION,
    export_schema,
    known_versions,
)


__all__ = [
    # catalog view (A)
    "PlannerToolEntry",
    "enabled_actions_for",
    "planner_tool_list",
    # schema (B)
    "PLAN_JSON_SCHEMA",
    "SCHEMA_VERSION",
    "LLMPlanRequest",
    "LLMPlanResponse",
    "export_schema",
    "known_versions",
    # parse (C)
    "ParseResult",
    "ValidationError",
    "parse_plan",
    "STABLE_VALIDATION_CODES",
    "CODE_PLAN_SCHEMA_INVALID",
    "CODE_BACKEND_DISABLED",
    "CODE_ACTION_CODE_MISMATCH",
    "CODE_TARGET_REF_SNAPSHOT_MISMATCH",
    "CODE_DEPENDENCY_CYCLE",
    "COORDINATE_FALLBACK_NOT_ALLOWED",
    "CODE_UNKNOWN_SCHEMA_VERSION",
]