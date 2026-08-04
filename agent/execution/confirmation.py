"""
confirmation.py — P3.1.E.A confirmation boundary at executor.

Implements P3.1 design gate contract **D14** (Confirmation policy
contract):

    * Gate lives at the executor boundary, NOT in the planner.
    * Gate is deterministic — same `(action, profile, policy)`
      triple MUST produce the same decision every time, regardless
      of LLM output.
    * LLM cannot bypass:
        - Emitting a different `action_id` with the same effect
          (catalog + dispatch reject with `unknown_action_id`).
        - Omitting `requires` (dispatcher rejects with
          `precondition_failed`).
        - Claiming "confirmed" in the plan payload (the plan
          payload MUST NOT carry a confirmation field; the calling
          system supplies it out-of-band — R1).
    * Confirmation required when:
        - `action.risk ∈ {high, irreversible}`, OR
        - `action.side_effect ∈ {gui_mutation, system_mutation}`
          AND profile declares a confirmation requirement.
    * Reject contract:
        `code = "confirmation_required"` + `action_id`. Plan MUST
        NOT be dispatched.

Public API (frozen contract):

    * `ConfirmationReason` — enum of reason codes (R3, verbatim).
    * `ConfirmationDecision` — structured outcome, NOT bool (R2).
    * `ExecutionContext` — out-of-band token holder (R1).
    * `ConfirmationPolicy` — pure deterministic policy.
    * `ConfirmationGate` — wire the policy into a gate callable
      from the executor boundary.

This module ships **E.A** (core contract). E.B (executor integration)
follows after the E.A review gate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, FrozenSet, Mapping


# ---------------------------------------------------------------------
# Reason codes (R3 — preserved for audit + report)
# ---------------------------------------------------------------------


class ConfirmationReason(str, Enum):
    """Why the decision came out this way.

    These codes are the audit + report surface. They MUST be
    preserved verbatim (do not collapse to `allowed` / `denied`).
    """

    NOT_REQUIRED = "not_required"
    """Confirmation is not needed for this action — gate allows."""

    CONFIRMATION_REQUIRED = "confirmation_required"
    """Action requires confirmation; token missing. Gate blocks.
    Pairs with D14 reject code `confirmation_required`."""

    ALREADY_CONFIRMED = "already_confirmed"
    """Action requires confirmation; valid token supplied. Gate
    allows."""

    INVALID_TOKEN = "invalid_token"
    """Action requires confirmation; token supplied but does not
    match the action's required token. Gate blocks."""


# ---------------------------------------------------------------------
# Outcome (R2 — structured, NOT bool)
# ---------------------------------------------------------------------


@dataclass(frozen=True)
class ConfirmationDecision:
    """Structured outcome of a confirmation evaluation.

    R2: this MUST NOT collapse to a bool. The fields here flow
    into the audit log and the rejection payload reported by the
    executor.
    """

    required: bool
    """True if the gate BLOCKS execution. False if the gate
    ALLOWS execution."""

    reason: ConfirmationReason
    """Why the decision came out this way (R3)."""

    action_id: str
    """The action_id this decision applies to."""

    risk: str = ""
    """Risk class reported by the catalog (D14 trigger input)."""

    side_effect: str = ""
    """Side-effect class reported by the catalog (D14 trigger)."""

    profile: str = ""
    """The profile under which the policy evaluated."""

    @property
    def is_allowed(self) -> bool:
        """Convenience: gate allows execution."""
        return not self.required

    @property
    def reject_code(self) -> str:
        """D14 reject code returned to the caller when required."""
        # D14 contract: code = "confirmation_required" + action_id.
        if not self.required:
            return ""
        return "confirmation_required"


# ---------------------------------------------------------------------
# Out-of-band token holder (R1 — NOT in plan payload)
# ---------------------------------------------------------------------


@dataclass(frozen=True)
class ExecutionContext:
    """Per-run execution context. Carries confirmation tokens out-of-band.

    R1: confirmation tokens MUST live here, NOT in the plan payload.
    The planner emits a plan; the calling system supplies tokens
    when invoking the executor. The LLM cannot write into this
    context.

    Tokens are scoped per action_id. A token for `action_a` does NOT
    grant confirmation for `action_b`. This prevents the LLM from
    carrying one confirmation across actions.
    """

    profile: str = "default"
    confirmation_tokens: FrozenSet[str] = frozenset()

    def has_token_for(self, action_id: str) -> bool:
        """True iff this context carries a valid token for action_id."""
        return action_id in self.confirmation_tokens


# ---------------------------------------------------------------------
# Policy (pure deterministic)
# ---------------------------------------------------------------------


# Action risk classes that ALWAYS require confirmation, regardless of
# profile. Per D14 trigger contract.
_RISK_ALWAYS_CONFIRM: FrozenSet[str] = frozenset({"high", "irreversible"})

# Side-effect classes that require confirmation only when the active
# profile declares so. Per D14 trigger contract.
_SIDE_EFFECT_PROFILE_GATED: FrozenSet[str] = frozenset({
    "gui_mutation",
    "system_mutation",
})


@dataclass(frozen=True)
class ConfirmationPolicy:
    """Pure deterministic policy.

    D14 determinism invariant: the same `(action_id, risk,
    side_effect, profile)` quadruple MUST produce the same decision
    every time. No LLM input, no prompt context, no history.

    Trigger rules (per D14):
        * risk ∈ {high, irreversible} → confirmation required.
        * side_effect ∈ {gui_mutation, system_mutation} AND
          profile declares so → confirmation required.
        * Otherwise → confirmation NOT required.

    Profile overrides:
        * `profile_overrides[profile][side_effect] = True`
          enables per-profile confirmation for that side_effect.
        * If profile has no override entry, side_effect-triggered
          confirmation is OFF by default.

    Reject codes (R3):
        * `not_required` — gate allows, no confirmation needed.
        * `confirmation_required` — gate blocks, token missing.
        * `already_confirmed` — gate allows, token valid.
        * `invalid_token` — gate blocks, token supplied but invalid.
    """

    profile_overrides: Mapping[str, Mapping[str, bool]] = field(
        default_factory=dict,
    )

    def evaluate(
        self,
        *,
        action_id: str,
        risk: str,
        side_effect: str,
        context: ExecutionContext,
    ) -> ConfirmationDecision:
        """Pure function: same inputs → same decision.

        Args:
            action_id: The action being evaluated. Echoed into
                the decision for audit.
            risk: Risk class from the catalog
                (e.g. "low", "moderate", "high", "irreversible").
            side_effect: Side-effect class from the catalog
                (e.g. "none", "gui_mutation", "system_mutation").
            context: Out-of-band execution context (carries profile
                + tokens).

        Returns:
            ConfirmationDecision with deterministic reason code.
        """
        requires = self._requires_confirmation(
            risk=risk,
            side_effect=side_effect,
            profile=context.profile,
        )

        if not requires:
            return ConfirmationDecision(
                required=False,
                reason=ConfirmationReason.NOT_REQUIRED,
                action_id=action_id,
                risk=risk,
                side_effect=side_effect,
                profile=context.profile,
            )

        # Confirmation required — check token.
        if context.has_token_for(action_id):
            return ConfirmationDecision(
                required=False,
                reason=ConfirmationReason.ALREADY_CONFIRMED,
                action_id=action_id,
                risk=risk,
                side_effect=side_effect,
                profile=context.profile,
            )

        return ConfirmationDecision(
            required=True,
            reason=ConfirmationReason.CONFIRMATION_REQUIRED,
            action_id=action_id,
            risk=risk,
            side_effect=side_effect,
            profile=context.profile,
        )

    def _requires_confirmation(
        self,
        *,
        risk: str,
        side_effect: str,
        profile: str,
    ) -> bool:
        """D14 trigger rule: pure decision function.

        Risk-based trigger fires unconditionally. Side-effect-based
        trigger fires only when the profile opts in.
        """
        if risk in _RISK_ALWAYS_CONFIRM:
            return True
        if side_effect in _SIDE_EFFECT_PROFILE_GATED:
            overrides = self.profile_overrides.get(profile, {})
            return bool(overrides.get(side_effect, False))
        return False


# ---------------------------------------------------------------------
# Gate (wire the policy into a callable for the executor)
# ---------------------------------------------------------------------


class ConfirmationGate:
    """Stateless gate callable from the executor boundary.

    The gate wraps a `ConfirmationPolicy` and exposes a single
    `check()` method. The executor calls this BEFORE dispatching
    any mutating action (per D14).

    The gate is intentionally stateless: same inputs always yield
    the same decision. State (if any, e.g. for audit) is the
    caller's responsibility.
    """

    __slots__ = ("_policy",)

    def __init__(self, policy: ConfirmationPolicy | None = None) -> None:
        # Default policy: risk-based only. Side-effect gating OFF
        # until the active profile opts in.
        self._policy = policy or ConfirmationPolicy()

    @property
    def policy(self) -> ConfirmationPolicy:
        return self._policy

    def check(
        self,
        *,
        action_id: str,
        risk: str,
        side_effect: str,
        context: ExecutionContext,
    ) -> ConfirmationDecision:
        """Evaluate and return the decision.

        Pure function. The executor MUST call this before
        dispatching any action that has a non-empty risk or
        side_effect. Low-risk read-only actions MAY skip the call
        for efficiency, but the gate is idempotent so callers may
        also call it unconditionally.
        """
        return self._policy.evaluate(
            action_id=action_id,
            risk=risk,
            side_effect=side_effect,
            context=context,
        )


# ---------------------------------------------------------------------
# Public exports
# ---------------------------------------------------------------------


__all__ = [
    "ConfirmationReason",
    "ConfirmationDecision",
    "ExecutionContext",
    "ConfirmationPolicy",
    "ConfirmationGate",
]