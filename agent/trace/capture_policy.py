"""
capture_policy.py — Capture policy table (architecture §14.2,
FR-P1.4-02 + -03).

The capture policy answers one question per step:

    Given the step's transition hint, evidence declaration, and
    runtime outcome, which screenshot roles (if any) must be
    persisted?

The §14.2 table is encoded as plain data; the resolver returns a
list of roles. Tests pin the matrix (one row per situation).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping


VALID_CAPTURE_ROLES = ("before", "after", "failure", "baseline")


@dataclass(frozen=True)
class CaptureContext:
    """Inputs to the capture-policy resolver."""
    is_case_start: bool = False
    is_observation_only: bool = False
    transition_expected: bool = False  # step.transition.expected
    is_checkpointed: bool = False     # step has a checkpoint policy
    is_irreversible: bool = False      # step.action is irreversible
    outcome_failed: bool = False
    outcome_blocked: bool = False
    expectation_requires_visual: bool = False


@dataclass(frozen=True)
class CapturePlan:
    """The roles to capture for one step."""
    roles: tuple[str, ...] = ()
    mandatory: bool = False  # if True, the capture cannot be weakened


# ---------------------------------------------------------------------------
# §14.2 table.
# ---------------------------------------------------------------------------
#
# case start                          -> baseline
# observation-only (no expectation)   -> nothing on success, failure on fail
# observation-only (visual_evidence)   -> after on success, failure on fail
# stable same-page mutation            -> after
# expected navigation/modal/window     -> before + after
# checkpointed action                 -> before + after
# irreversible action                 -> before + after (authorization
#                                        reference is policy metadata,
#                                        not image gating)
# any failed/blocked                  -> immediate failure
#
# Mandatory: any failure-image and baseline-image captures are
# mandatory; tests cannot weaken them (FR-P1.4-03).


def resolve_capture_plan(ctx: CaptureContext) -> CapturePlan:
    roles: list[str] = []
    mandatory = False

    if ctx.is_case_start:
        # baseline at case start.
        roles.append("baseline")
        mandatory = True  # mandatory baseline.

    if ctx.is_observation_only:
        if ctx.expectation_requires_visual:
            if not (ctx.outcome_failed or ctx.outcome_blocked):
                roles.append("after")
        # Failure image handled below.
    elif ctx.transition_expected or ctx.is_checkpointed or ctx.is_irreversible:
        roles.extend(["before", "after"])
    else:
        # Default for stable same-page mutation.
        if not (ctx.outcome_failed or ctx.outcome_blocked):
            roles.append("after")

    # Any failed/blocked -> immediate failure image (mandatory).
    if ctx.outcome_failed or ctx.outcome_blocked:
        if "failure" not in roles:
            roles.append("failure")
        mandatory = True

    return CapturePlan(roles=tuple(roles), mandatory=mandatory)


__all__ = [
    "CaptureContext",
    "CapturePlan",
    "VALID_CAPTURE_ROLES",
    "resolve_capture_plan",
]