"""
schema.py — Planner JSON Schema + request/response dataclasses
(P3.1 Commit B).

P3.1 design gate contract D11:

  * `PLAN_JSON_SCHEMA` MUST be derived from the existing P0.2
    dataclasses (`ActionSequence`, `ActionStep`, `TargetRef`,
    `Expectation`, `Transition`, `EvidenceSpec`,
    `AtomicTestStep`, `TestCase`) without redefining any field
    that already exists in those models.
  * Every object-level field in the schema MUST set
    `additionalProperties: false`. Unknown fields at any level
    MUST be rejected at parse time.
  * Schema rejection MUST return:
    - `code = "plan_schema_invalid"`
    - `JSON_pointer` to the offending field (RFC 6901 format).
    - `message` describing the rejection reason.
  * The schema MUST be exported to
    `test_case/schema/plan.schema.json` for consumers to
    validate against.
  * **Schema versioning**: the schema MUST carry a
    `schema_version` field (initial value `plan_schema.v1`).
    A version bump is a major break (architecture §7.2):
    legacy consumers MUST be able to read the new schema's
    `schema_version` and reject plans whose version they don't
    recognise.

This module ships:

  * `PLAN_JSON_SCHEMA` — the JSON Schema dict (in-memory).
  * `LLMPlanRequest` — the dataclass the planner uses to
    describe what the LLM is allowed to emit.
  * `LLMPlanResponse` — the dataclass the parser produces;
    carries `plan` (the `ActionSequence`) and any
    `validation_errors` collected.
  * `SCHEMA_VERSION` — the schema version constant
    (`plan_schema.v1`).
  * `SCHEMA_PATH` — the on-disk path under `test_case/schema/`
    where the schema is exported.

The JSON Schema is hand-derived from the P0.2 dataclasses. It
is not auto-generated from the dataclasses because:

  * P0.2 models carry runtime fields (`_ALLOWED`, `from_dict`)
    that should not leak into the wire schema.
  * The planner schema adds fields the P0.2 dataclasses do
    not have (e.g. `replan_id`, `replan_count`, `hints`).
  * We want `additionalProperties: false` to be explicit at
    every nested object so the contract is grep-able.

This module ships the V1 schema. Future schema bumps must:
  * bump `SCHEMA_VERSION`,
  * add a new entry to `_KNOWN_VERSIONS`,
  * and re-export `test_case/schema/plan.schema.json`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping


SCHEMA_VERSION = "plan_schema.v1"

# The set of versions the current code knows how to read.
# Adding a new version is a minor bump (parsing accepts both);
# removing a version is a major break (architecture §7.2).
_KNOWN_VERSIONS: frozenset[str] = frozenset({SCHEMA_VERSION})


# ---------------------------------------------------------------------------
# PLAN_JSON_SCHEMA
#
# Hand-derived from P0.2 dataclasses. The wire shape is what the
# LLM emits back; the in-process dataclasses (LLMPlanRequest /
# LLMPlanResponse) are the planner-side carrier.
#
# Schema-version bump protocol:
#   1. Bump SCHEMA_VERSION above.
#   2. Add the new version to _KNOWN_VERSIONS.
#   3. Update the in-schema `"schema_version"` enum below.
#   4. Re-run `python -m agent.planner.schema export` to refresh
#      `test_case/schema/plan.schema.json`.
# ---------------------------------------------------------------------------

PLAN_JSON_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "https://edr-wd.local/schemas/plan.schema.json",
    "title": "LLMPlannerPlan",
    "description": (
        "Wire schema for the planner's structured-output plan. "
        "Schema version `plan_schema.v1`."
    ),
    "type": "object",
    "additionalProperties": False,
    "required": ["schema_version", "plan_id", "snapshot_id",
                "expected_actions"],
    "properties": {
        "schema_version": {
            "type": "string",
            "const": "plan_schema.v1",
            "description": (
                "Wire schema version. Consumers MUST reject "
                "unknown versions."
            ),
        },
        "plan_id": {
            "type": "string",
            "description": (
                "Sortable unique id (FR-P3.1-02 wire format)."
            ),
        },
        "snapshot_id": {
            "type": "string",
            "description": (
                "Snapshot id the plan was built against; "
                "FR-P3.1-06 must match the live snapshot at "
                "execution time."
            ),
        },
        "replan_id": {
            "type": ["string", "null"],
            "description": (
                "Stable unique id for this replan attempt; "
                "D13 hierarchy: run_id > step_id > replan_id "
                "> event_id."
            ),
        },
        "replan_count": {
            "type": "integer",
            "minimum": 0,
            "description": (
                "How many replan attempts preceded this plan; "
                "D13 bound is implementation-defined but "
                "tracked here for audit."
            ),
        },
        "expected_actions": {
            "type": "array",
            "minItems": 1,
            "items": {"$ref": "#/$defs/ActionStep"},
            "description": (
                "The ordered list of steps the planner emits. "
                "Each step maps 1:1 to a P0.2 `ActionStep`."
            ),
        },
        "expected_dependencies": {
            "type": "object",
            "additionalProperties": {
                "type": "array",
                "items": {"type": "string"},
                "uniqueItems": True,
            },
            "description": (
                "step_id -> [step_id...] dependency map. "
                "Used by FR-P3.1-10 cycle detection."
            ),
        },
        "transition_declarations": {
            "type": "object",
            "additionalProperties": {
                "$ref": "#/$defs/Transition",
            },
            "description": (
                "step_id -> Transition declaration. P3.1 "
                "spec: declare `transition.expected` and "
                "`on_error` per step."
            ),
        },
        "expected_expectations": {
            "type": "object",
            "additionalProperties": {
                "$ref": "#/$defs/Expectation",
            },
            "description": (
                "step_id -> Expectation declaration."
            ),
        },
        "expected_evidence": {
            "type": "object",
            "additionalProperties": {
                "$ref": "#/$defs/EvidenceSpec",
            },
            "description": (
                "step_id -> EvidenceSpec override."
            ),
        },
        "hints": {
            "type": "object",
            "additionalProperties": {"type": "string"},
            "description": (
                "Free-form hints for the planner surface; "
                "consumed but not enforced."
            ),
        },
    },
    "$defs": {
        "ActionStep": {
            "type": "object",
            "additionalProperties": False,
            "required": ["step_id", "action_id"],
            "properties": {
                "step_id": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Stable id within the plan.",
                },
                "action_id": {
                    "type": "string",
                    "description": (
                        "Must match a catalog action_id; "
                        "validated by parse_plan (D12 stage 2)."
                    ),
                },
                "action_code": {
                    "type": ["string", "null"],
                    "description": (
                        "Optional; must match catalog "
                        "action_code for action_id (D12 "
                        "stage 3)."
                    ),
                },
                "target_ref": {
                    "$ref": "#/$defs/TargetRef",
                },
                "args": {
                    "type": "object",
                    "description": (
                        "Action input args; must match the "
                        "action's `input_schema` (P0.2)."
                    ),
                },
                "timeout_seconds": {
                    "type": "integer",
                    "minimum": 1,
                },
            },
        },
        "TargetRef": {
            "type": "object",
            "additionalProperties": False,
            "required": ["snapshot_id", "target_id"],
            "properties": {
                "snapshot_id": {
                    "type": "string",
                    "minLength": 1,
                },
                "target_id": {
                    "type": "string",
                    "minLength": 1,
                },
            },
        },
        "Transition": {
            "type": "object",
            "additionalProperties": False,
            "required": ["expected"],
            "properties": {
                "expected": {
                    "type": "string",
                    "enum": [
                        "none",
                        "control_state_change",
                        "page_navigation",
                        "modal_open",
                        "modal_close",
                        "window_open",
                        "window_close",
                        "window_owner_change",
                        "application_restart",
                        "unknown_material_change",
                    ],
                    "description": (
                        "Mirrors the P2.1 transition kinds."
                    ),
                },
                "on_error": {
                    "type": "string",
                    "enum": [
                        "abort",
                        "retry",
                        "capture_and_abort",
                        "reobserve_replan",
                    ],
                    "description": (
                        "Mirrors the P1.2 on_error policies."
                    ),
                },
            },
        },
        "Expectation": {
            "type": "object",
            "additionalProperties": False,
            "required": ["type"],
            "properties": {
                "type": {
                    "type": "string",
                    "enum": [
                        "action_ok",
                        "window_open",
                        "window_closed",
                        "active_window_owner",
                        "control_exists",
                        "control_absent",
                        "control_text_equals",
                        "control_text_contains",
                        "control_value_equals",
                        "control_checked_equals",
                        "control_enabled_equals",
                        "window_text_contains",
                        "control_text_contains_time",
                        "window_text_contains_time",
                        "visual_evidence_captured",
                    ],
                    "description": (
                        "Mirrors the P1.2 / P1.4 expectation "
                        "registry (15 types)."
                    ),
                },
                "value": {
                    "description": (
                        "Type-specific expected value; "
                        "schema depends on `type`."
                    ),
                },
                "timeout_seconds": {
                    "type": "integer",
                    "minimum": 1,
                },
            },
        },
        "EvidenceSpec": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "screenshot": {
                    "type": "string",
                    "enum": [
                        "none",
                        "before",
                        "after",
                        "before_and_after",
                        "failure",
                    ],
                    "description": (
                        "Mirrors P0.2 default_screenshot enum."
                    ),
                },
            },
        },
    },
}


# ---------------------------------------------------------------------------
# LLMPlanRequest — the dataclass the planner uses to describe
# what the LLM is allowed to emit.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LLMPlanRequest:
    """Request body for a single planner call.

    Mirrors P3.1 spec §"Interface And Data Requirements":
      `{snapshot_id, target, profile, allowed_actions:
       [action_id...], schema_ref, hints}`.
    """

    snapshot_id: str
    target: str
    profile: str
    allowed_actions: tuple[str, ...]
    schema_ref: str = "test_case/schema/plan.schema.json"
    hints: Mapping[str, str] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# LLMPlanResponse — what the parser produces.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LLMPlanResponse:
    """Response body from a single planner call.

    Carries the parsed plan (as a dict matching PLAN_JSON_SCHEMA)
    plus any validation errors collected during parsing.
    """

    plan: dict
    validation_errors: tuple[dict, ...] = ()


# ---------------------------------------------------------------------------
# Export helper
# ---------------------------------------------------------------------------


# `test_case/schema/plan.schema.json` — exported by `export()`,
# not imported as a Python module, so that consumers can read it
# without depending on the agent package.
_SCHEMA_EXPORT_PATH = (
    Path(__file__).resolve().parents[2]
    / "test_case" / "schema" / "plan.schema.json"
)


def export_schema() -> Path:
    """Write `PLAN_JSON_SCHEMA` to
    `test_case/schema/plan.schema.json`. Returns the path."""
    _SCHEMA_EXPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    _SCHEMA_EXPORT_PATH.write_text(
        json.dumps(PLAN_JSON_SCHEMA, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    return _SCHEMA_EXPORT_PATH


def known_versions() -> frozenset[str]:
    """Versions the current code knows how to read.

    Consumers can use this to validate a payload's
    `schema_version` before parsing.
    """
    return _KNOWN_VERSIONS


__all__ = [
    "PLAN_JSON_SCHEMA",
    "SCHEMA_VERSION",
    "LLMPlanRequest",
    "LLMPlanResponse",
    "export_schema",
    "known_versions",
]
