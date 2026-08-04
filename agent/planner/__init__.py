"""
__init__.py — Public surface of agent/planner (P3.1).

Layering:

    target/protocol_models  — wire models + validator
    target/action_catalog  — canonical action table
    target/action_dispatcher — dispatch + receipts
    agent/execution         — AtomicExecutor + state machine
    agent/trace             — events, evidence, projections
    agent/planner           — THIS package (P3.1)

Public re-exports (P3.1 Commit A + B + C + D + F):

  * `enabled_actions_for` / `planner_tool_list` —
    planner-side catalog view (Commit A).
  * `PLAN_JSON_SCHEMA` / `SCHEMA_VERSION` /
    `LLMPlanRequest` / `LLMPlanResponse` / `export_schema` /
    `known_versions` — wire schema + request/response
    (Commit B).
  * `parse_plan` / `ParseResult` / `ValidationError` /
    `STABLE_VALIDATION_CODES` — 5-stage plan validation
    (Commit C).
  * `PlanEvent` / `build_plan_event` / `audit_persisted_plans` —
    persistence + sensitive-argument audit (Commit D).
  * `needs_replan` / `ReplanBudget` / `ReplanDecision` /
    `ReplanBudgetError` / `check_replan_budget` —
    replan trigger and bound (Commit F).

Subsequent P3.1 commit (G = prompt) will add to this
surface.

Note: Commit E (confirmation) lives in
`agent.execution.confirmation` per the D14 contract that
the confirmation gate sits at the executor boundary, not
in the planner.
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
from .persist import (
    PLANNER_EVENT_SCHEMA_VERSION,
    PLAN_EVENT_TYPES,
    PlanEvent,
    PlanEventType,
    audit_persisted_plans,
    build_plan_event,
)
from .post_step import (
    ALL_REPLAN_REASONS,
    CODE_REPLAN_BUDGET_EXHAUSTED,
    DEFAULT_REPLAN_BUDGET,
    REPLAN_REASON_NO_PREVIOUS_STEP,
    REPLAN_REASON_NO_REPLAN_NEEDED,
    REPLAN_REASON_STALE_SNAPSHOT,
    REPLAN_REASON_STALE_TARGET_REF,
    REPLAN_REASON_UNEXPECTED_TRANSITION,
    ReplanBudget,
    ReplanBudgetError,
    ReplanDecision,
    check_replan_budget,
    generate_replan_id,
    needs_replan,
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
    # persist (D)
    "PLANNER_EVENT_SCHEMA_VERSION",
    "PLAN_EVENT_TYPES",
    "PlanEvent",
    "PlanEventType",
    "build_plan_event",
    "audit_persisted_plans",
    # post_step (F)
    "needs_replan",
    "ReplanBudget",
    "ReplanDecision",
    "ReplanBudgetError",
    "check_replan_budget",
    "generate_replan_id",
    "CODE_REPLAN_BUDGET_EXHAUSTED",
    "DEFAULT_REPLAN_BUDGET",
    "ALL_REPLAN_REASONS",
    "REPLAN_REASON_STALE_TARGET_REF",
    "REPLAN_REASON_UNEXPECTED_TRANSITION",
    "REPLAN_REASON_STALE_SNAPSHOT",
    "REPLAN_REASON_NO_PREVIOUS_STEP",
    "REPLAN_REASON_NO_REPLAN_NEEDED",
]