"""
P3.1.E.A — Confirmation boundary unit tests.

Covers D14 contract invariants:
  * Trigger rule (risk + side_effect + profile).
  * Determinism (same inputs → same decision).
  * Out-of-band token (R1 — NOT in plan payload).
  * Structured outcome (R2 — NOT bool).
  * Reason codes preserved for audit (R3).

Detailed integration tests for executor-side wiring live in
`test_confirmation_integration.py` (P3.1.E.B).
"""

from __future__ import annotations

import pytest

from agent.execution.confirmation import (
    ConfirmationDecision,
    ConfirmationGate,
    ConfirmationPolicy,
    ConfirmationReason,
    ExecutionContext,
)


# ---------------------------------------------------------------------
# Trigger rule tests
# ---------------------------------------------------------------------


class TestTriggerRule:
    """D14 trigger contract: confirmation required when
    risk ∈ {high, irreversible} OR side_effect profile-gated."""

    def test_high_risk_requires_confirmation(self) -> None:
        """Risk 'high' unconditionally requires confirmation."""
        gate = ConfirmationGate()
        ctx = ExecutionContext(profile="default")
        decision = gate.check(
            action_id="send_email",
            risk="high",
            side_effect="none",
            context=ctx,
        )
        assert decision.required is True
        assert decision.reason == ConfirmationReason.CONFIRMATION_REQUIRED
        assert decision.reject_code == "confirmation_required"
        assert decision.action_id == "send_email"

    def test_irreversible_risk_requires_confirmation(self) -> None:
        """Risk 'irreversible' unconditionally requires confirmation."""
        gate = ConfirmationGate()
        ctx = ExecutionContext(profile="default")
        decision = gate.check(
            action_id="delete_database",
            risk="irreversible",
            side_effect="none",
            context=ctx,
        )
        assert decision.required is True
        assert decision.reason == ConfirmationReason.CONFIRMATION_REQUIRED

    def test_low_risk_passes(self) -> None:
        """Risk 'low' + no side_effect → no confirmation needed."""
        gate = ConfirmationGate()
        ctx = ExecutionContext(profile="default")
        decision = gate.check(
            action_id="read_window",
            risk="low",
            side_effect="none",
            context=ctx,
        )
        assert decision.required is False
        assert decision.reason == ConfirmationReason.NOT_REQUIRED
        assert decision.reject_code == ""
        assert decision.is_allowed is True

    def test_moderate_risk_no_side_effect_passes(self) -> None:
        """Risk 'moderate' (NOT in trigger set) + no side_effect → pass."""
        gate = ConfirmationGate()
        ctx = ExecutionContext(profile="default")
        decision = gate.check(
            action_id="annotate_screenshot",
            risk="moderate",
            side_effect="none",
            context=ctx,
        )
        assert decision.required is False
        assert decision.reason == ConfirmationReason.NOT_REQUIRED

    def test_gui_mutation_requires_confirmation_when_profile_opted_in(
        self,
    ) -> None:
        """side_effect 'gui_mutation' + profile override → requires."""
        policy = ConfirmationPolicy(
            profile_overrides={
                "strict": {"gui_mutation": True},
            },
        )
        gate = ConfirmationGate(policy)
        ctx = ExecutionContext(profile="strict")
        decision = gate.check(
            action_id="click_button",
            risk="low",
            side_effect="gui_mutation",
            context=ctx,
        )
        assert decision.required is True
        assert decision.reason == ConfirmationReason.CONFIRMATION_REQUIRED

    def test_gui_mutation_passes_when_profile_did_not_opt_in(self) -> None:
        """side_effect 'gui_mutation' WITHOUT profile override → pass."""
        # Default policy: no profile opt-in for side_effect.
        gate = ConfirmationGate()
        ctx = ExecutionContext(profile="default")
        decision = gate.check(
            action_id="click_button",
            risk="low",
            side_effect="gui_mutation",
            context=ctx,
        )
        # Default profile does NOT require gui_mutation confirmation.
        assert decision.required is False
        assert decision.reason == ConfirmationReason.NOT_REQUIRED

    def test_system_mutation_requires_confirmation_when_profile_opted_in(
        self,
    ) -> None:
        """side_effect 'system_mutation' + profile override → requires."""
        policy = ConfirmationPolicy(
            profile_overrides={
                "strict": {"system_mutation": True},
            },
        )
        gate = ConfirmationGate(policy)
        ctx = ExecutionContext(profile="strict")
        decision = gate.check(
            action_id="install_driver",
            risk="moderate",
            side_effect="system_mutation",
            context=ctx,
        )
        assert decision.required is True

    def test_irreversible_overrides_profile_pass(self) -> None:
        """Risk 'irreversible' triggers regardless of profile."""
        # Even with no profile override, irreversible fires.
        gate = ConfirmationGate()
        ctx = ExecutionContext(profile="permissive")
        decision = gate.check(
            action_id="format_disk",
            risk="irreversible",
            side_effect="none",
            context=ctx,
        )
        assert decision.required is True


# ---------------------------------------------------------------------
# Token validation tests (R1 — out-of-band)
# ---------------------------------------------------------------------


class TestTokenValidation:
    """R1: confirmation token is out-of-band, NOT in plan payload."""

    def test_already_confirmed_with_matching_token(self) -> None:
        """Valid token for action → gate allows."""
        gate = ConfirmationGate()
        ctx = ExecutionContext(
            profile="default",
            confirmation_tokens=frozenset({"send_email"}),
        )
        decision = gate.check(
            action_id="send_email",
            risk="high",
            side_effect="none",
            context=ctx,
        )
        assert decision.required is False
        assert decision.reason == ConfirmationReason.ALREADY_CONFIRMED

    def test_missing_token_for_high_risk_blocks(self) -> None:
        """High-risk action + no token → blocks."""
        gate = ConfirmationGate()
        ctx = ExecutionContext(profile="default")  # no tokens
        decision = gate.check(
            action_id="send_email",
            risk="high",
            side_effect="none",
            context=ctx,
        )
        assert decision.required is True
        assert decision.reason == ConfirmationReason.CONFIRMATION_REQUIRED

    def test_token_must_match_action_id(self) -> None:
        """Token for `action_a` does NOT grant confirmation for
        `action_b`. Prevents the LLM from carrying one
        confirmation across actions."""
        gate = ConfirmationGate()
        ctx = ExecutionContext(
            profile="default",
            confirmation_tokens=frozenset({"action_a"}),
        )
        # High-risk action_b, but only action_a is confirmed.
        decision = gate.check(
            action_id="action_b",
            risk="high",
            side_effect="none",
            context=ctx,
        )
        assert decision.required is True
        assert decision.reason == ConfirmationReason.CONFIRMATION_REQUIRED

    def test_irrelevant_token_for_low_risk_passes(self) -> None:
        """Token for an unrelated action + low-risk action → pass
        (no confirmation needed anyway)."""
        gate = ConfirmationGate()
        ctx = ExecutionContext(
            profile="default",
            confirmation_tokens=frozenset({"some_other_action"}),
        )
        decision = gate.check(
            action_id="read_window",
            risk="low",
            side_effect="none",
            context=ctx,
        )
        assert decision.required is False
        assert decision.reason == ConfirmationReason.NOT_REQUIRED

    def test_token_unused_when_not_required(self) -> None:
        """Token supplied for an action that doesn't need
        confirmation → decision is `not_required` (token unused)."""
        gate = ConfirmationGate()
        ctx = ExecutionContext(
            profile="default",
            confirmation_tokens=frozenset({"read_window"}),
        )
        decision = gate.check(
            action_id="read_window",
            risk="low",
            side_effect="none",
            context=ctx,
        )
        # Token present but not needed → not_required.
        assert decision.reason == ConfirmationReason.NOT_REQUIRED


# ---------------------------------------------------------------------
# Determinism (D14 core invariant)
# ---------------------------------------------------------------------


class TestDeterminism:
    """D14: same inputs → same decision. No LLM, no history."""

    def test_confirmation_deterministic(self) -> None:
        """Calling the gate twice with the same inputs yields
        structurally equal decisions."""
        gate = ConfirmationGate()
        ctx = ExecutionContext(
            profile="default",
            confirmation_tokens=frozenset({"send_email"}),
        )
        d1 = gate.check(
            action_id="send_email",
            risk="high",
            side_effect="none",
            context=ctx,
        )
        d2 = gate.check(
            action_id="send_email",
            risk="high",
            side_effect="none",
            context=ctx,
        )
        # Dataclass equality is structural.
        assert d1 == d2

    def test_policy_does_not_read_external_state(self) -> None:
        """The policy does not depend on time, randomness, or
        any external mutable state."""
        # If it did, calling it 100 times would yield variance.
        gate = ConfirmationGate()
        ctx = ExecutionContext(profile="default")
        decisions = [
            gate.check(
                action_id="send_email",
                risk="high",
                side_effect="none",
                context=ctx,
            )
            for _ in range(100)
        ]
        # All decisions identical.
        assert all(d == decisions[0] for d in decisions)

    def test_decision_is_immutable(self) -> None:
        """ConfirmationDecision is a frozen dataclass."""
        gate = ConfirmationGate()
        ctx = ExecutionContext(profile="default")
        decision = gate.check(
            action_id="send_email",
            risk="high",
            side_effect="none",
            context=ctx,
        )
        with pytest.raises(Exception):
            # Frozen dataclass rejects setattr.
            decision.required = False  # type: ignore[misc]


# ---------------------------------------------------------------------
# Structured outcome (R2 — NOT bool)
# ---------------------------------------------------------------------


class TestStructuredOutcome:
    """R2: ConfirmationDecision is structured, NOT a bool."""

    def test_decision_carries_action_id(self) -> None:
        """Decision records the action_id for audit."""
        gate = ConfirmationGate()
        ctx = ExecutionContext(profile="default")
        decision = gate.check(
            action_id="send_email",
            risk="high",
            side_effect="none",
            context=ctx,
        )
        assert decision.action_id == "send_email"

    def test_decision_carries_risk_and_side_effect(self) -> None:
        """Decision echoes risk + side_effect for audit."""
        gate = ConfirmationGate()
        ctx = ExecutionContext(profile="default")
        decision = gate.check(
            action_id="send_email",
            risk="high",
            side_effect="network_call",
            context=ctx,
        )
        assert decision.risk == "high"
        assert decision.side_effect == "network_call"

    def test_decision_carries_profile(self) -> None:
        """Decision records the profile that evaluated."""
        gate = ConfirmationGate()
        ctx = ExecutionContext(profile="strict")
        decision = gate.check(
            action_id="send_email",
            risk="low",
            side_effect="gui_mutation",
            context=ctx,
        )
        assert decision.profile == "strict"

    def test_reject_code_constant_for_required_decision(self) -> None:
        """D14 reject code is constant `confirmation_required`."""
        gate = ConfirmationGate()
        ctx = ExecutionContext(profile="default")
        for risk in ("high", "irreversible"):
            decision = gate.check(
                action_id="x",
                risk=risk,
                side_effect="none",
                context=ctx,
            )
            assert decision.reject_code == "confirmation_required"

    def test_reject_code_empty_when_allowed(self) -> None:
        """Reject code is empty string when gate allows."""
        gate = ConfirmationGate()
        ctx = ExecutionContext(profile="default")
        decision = gate.check(
            action_id="read_window",
            risk="low",
            side_effect="none",
            context=ctx,
        )
        assert decision.reject_code == ""


# ---------------------------------------------------------------------
# Reason codes preserved (R3)
# ---------------------------------------------------------------------


class TestReasonCodes:
    """R3: reason codes preserved verbatim for audit + report."""

    def test_reason_code_set_is_frozen(self) -> None:
        """The set of reason codes is closed and well-known."""
        codes = {r.value for r in ConfirmationReason}
        assert codes == {
            "not_required",
            "confirmation_required",
            "already_confirmed",
            "invalid_token",
        }

    def test_decision_reason_is_one_of_known_codes(self) -> None:
        """Every decision has a known reason code."""
        gate = ConfirmationGate()
        ctx_no_token = ExecutionContext(profile="default")
        ctx_with_token = ExecutionContext(
            profile="default",
            confirmation_tokens=frozenset({"send_email"}),
        )
        decisions = [
            gate.check(
                action_id="read_window",
                risk="low",
                side_effect="none",
                context=ctx_no_token,
            ),
            gate.check(
                action_id="send_email",
                risk="high",
                side_effect="none",
                context=ctx_no_token,
            ),
            gate.check(
                action_id="send_email",
                risk="high",
                side_effect="none",
                context=ctx_with_token,
            ),
        ]
        for d in decisions:
            assert d.reason in ConfirmationReason


# ---------------------------------------------------------------------
# Out-of-band token (R1 — separate from plan payload)
# ---------------------------------------------------------------------


class TestOutOfBandToken:
    """R1: ConfirmationDecision / ExecutionContext do NOT carry
    any field named `confirmation` or similar that the LLM could
    write into via the plan payload."""

    def test_decision_has_no_confirmation_field(self) -> None:
        """Decision fields are audit-only; no 'claimed confirmation'."""
        gate = ConfirmationGate()
        ctx = ExecutionContext(profile="default")
        decision = gate.check(
            action_id="send_email",
            risk="high",
            side_effect="none",
            context=ctx,
        )
        # Public field names should NOT include any LLM-writable
        # "confirmation" claim. The decision reports the gate's
        # verdict, not what the LLM claimed.
        field_names = {f.name for f in decision.__dataclass_fields__.values()}
        assert "confirmation" not in field_names
        assert "claimed_confirmed" not in field_names

    def test_execution_context_carries_tokens_only(self) -> None:
        """ExecutionContext fields are profile + tokens, nothing
        the LLM could populate via plan payload."""
        ctx = ExecutionContext(profile="strict")
        field_names = {f.name for f in ctx.__dataclass_fields__.values()}
        # profile + confirmation_tokens. No "claim", "user_input",
        # "prompt", etc.
        assert field_names == {"profile", "confirmation_tokens"}

    def test_default_policy_does_not_require_side_effect(self) -> None:
        """Default policy (no profile_overrides) does NOT
        auto-require side_effect triggers. Opt-in only."""
        gate = ConfirmationGate()  # default policy
        ctx = ExecutionContext(profile="default")
        decision = gate.check(
            action_id="click_button",
            risk="low",
            side_effect="gui_mutation",
            context=ctx,
        )
        # No profile opt-in for gui_mutation → no confirmation.
        assert decision.required is False
        assert decision.reason == ConfirmationReason.NOT_REQUIRED


# ---------------------------------------------------------------------
# Profile boundary tests
# ---------------------------------------------------------------------


class TestProfileBoundary:
    """Different profiles yield different decisions for the same action."""

    def test_strict_profile_requires_side_effect(self) -> None:
        """A 'strict' profile with side_effect override gates gui_mutation."""
        policy = ConfirmationPolicy(
            profile_overrides={
                "strict": {"gui_mutation": True},
            },
        )
        gate = ConfirmationGate(policy)
        strict_ctx = ExecutionContext(profile="strict")
        permissive_ctx = ExecutionContext(profile="permissive")

        strict_d = gate.check(
            action_id="click",
            risk="low",
            side_effect="gui_mutation",
            context=strict_ctx,
        )
        permissive_d = gate.check(
            action_id="click",
            risk="low",
            side_effect="gui_mutation",
            context=permissive_ctx,
        )
        assert strict_d.required is True
        assert permissive_d.required is False

    def test_unknown_profile_falls_back_to_default(self) -> None:
        """An unknown profile gets the default trigger behavior
        (risk-based only)."""
        policy = ConfirmationPolicy(
            profile_overrides={
                "strict": {"gui_mutation": True},
            },
        )
        gate = ConfirmationGate(policy)
        ctx = ExecutionContext(profile="unknown_profile")
        decision = gate.check(
            action_id="click",
            risk="low",
            side_effect="gui_mutation",
            context=ctx,
        )
        # Unknown profile: no override → no confirmation.
        assert decision.required is False