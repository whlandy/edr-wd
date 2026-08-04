"""
P3.1.E.B — Confirmation boundary integration with AtomicExecutor.

End-to-end tests covering D14 contract at the executor boundary:

  * Executor blocks dispatch when confirmation is required and
    no token is supplied.
  * Executor dispatches normally when a valid token is supplied.
  * Plan payload CANNOT bypass confirmation (LLM cannot smuggle
    a "confirmed" field through the plan).
  * Token must match action_id (R1: out-of-band scoping).
  * Confirmation check is skipped when no gate is wired (backward
    compat with P1.2 callers).
  * Confirmation check is skipped when gate is wired but action
    is low-risk (no false positives).
  * error.code is exactly "confirmation_required" (D14 contract).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

import pytest

from agent.execution.confirmation import (
    ConfirmationGate,
    ConfirmationPolicy,
    ConfirmationReason,
    ExecutionContext,
)
from agent.execution.executor import AtomicExecutor
from action_dispatcher import ActionReceipt


# ---------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------


@dataclass(frozen=True)
class _FakeReceipt:
    """Stand-in for ActionReceipt."""

    action_id: str
    code: str = "ok"
    ok: bool = True
    action_code: str | None = None
    data: Mapping[str, Any] = field(default_factory=dict)
    error: Mapping[str, Any] | None = None


class _FakeObservationProvider:
    """Trivial observation provider for the executor state machine."""

    def refresh(self) -> str:
        return "snap-1"

    def get_snapshot(self, _snapshot_id: str) -> Mapping[str, Any]:
        return {"windows": []}

    def latest_snapshot_id(self) -> str:
        return "snap-1"


def _make_dispatch(
    *,
    fail_on_action_id: str | None = None,
    recorded_calls: list[dict[str, Any]] | None = None,
) -> Callable[..., ActionReceipt]:
    """Build a fake dispatch callable.

    If `fail_on_action_id` is set, raising a ValueError when
    that action_id is dispatched (simulates unknown_action_id).
    If `recorded_calls` is set, every dispatch call is appended
    there (so tests can assert the dispatcher was/was not called).
    """

    def _dispatch(
        *,
        action_id: str,
        action_code: str | None = None,
        args: Mapping[str, Any] | None = None,
        target_ref: Mapping[str, Any] | None = None,
        request_id: str = "",
    ) -> ActionReceipt:
        if recorded_calls is not None:
            recorded_calls.append({
                "action_id": action_id,
                "args": dict(args or {}),
            })
        if fail_on_action_id and action_id == fail_on_action_id:
            raise ValueError(f"unknown_action_id: {action_id}")
        return _FakeReceipt(action_id=action_id)

    return _dispatch


def _build_test_case(
    *,
    case_id: str = "case_x",
    steps: tuple[dict[str, Any], ...] = (),
) -> Any:
    """Build a minimal TestCase for the executor."""
    from protocol_models import AtomicTestStep, TestCase

    atomic_steps = tuple(
        AtomicTestStep(
            step_id=s["step_id"],
            action_id=s["action_id"],
            action_code=s.get("action_code"),
            args=s.get("args", {}),
            target_ref=s.get("target_ref"),
            depends_on=s.get("depends_on", ()),
            transition=s.get("transition"),
            expectations=s.get("expectations", ()),
            on_error=s.get("on_error", "abort"),
            evidence=s.get("evidence"),
            required=s.get("required", True),
        )
        for s in steps
    )
    return TestCase(
        case_id=case_id,
        title=case_id,
        description="confirmation integration test",
        steps=atomic_steps,
    )


def _make_executor(
    *,
    gate: ConfirmationGate | None = None,
    risk_lookup: Callable[[str], tuple[str, str]] | None = None,
    dispatch: Callable[..., ActionReceipt] | None = None,
) -> AtomicExecutor:
    """Build an AtomicExecutor with a confirmation gate wired in."""
    return AtomicExecutor(
        dispatch=dispatch or _make_dispatch(),
        observation_provider=_FakeObservationProvider(),
        confirmation_gate=gate,
        risk_lookup=risk_lookup,
    )


# ---------------------------------------------------------------------
# Executor + gate integration
# ---------------------------------------------------------------------


class TestExecutorWithConfirmation:
    """D14: confirmation gate at the executor boundary."""

    def test_executor_blocks_without_token(self) -> None:
        """High-risk action + no token → step is blocked,
        dispatcher is NOT called."""
        recorded: list[dict[str, Any]] = []
        gate = ConfirmationGate()
        risk_lookup = lambda _aid: ("high", "none")
        executor = _make_executor(
            gate=gate,
            risk_lookup=risk_lookup,
            dispatch=_make_dispatch(recorded_calls=recorded),
        )
        case = _build_test_case(steps=(
            {"step_id": "s1", "action_id": "send_email"},
        ))

        result = executor.run_case(
            case,
            execution_context=ExecutionContext(profile="default"),
        )

        # Step blocked.
        assert len(result.step_results) == 1
        sr = result.step_results[0]
        assert sr.status.value == "blocked"
        # D14 reject code.
        assert sr.error is not None
        assert sr.error["code"] == "confirmation_required"
        assert sr.error["action_id"] == "send_email"
        assert sr.error["reason"] == "confirmation_required"
        assert sr.error["risk"] == "high"
        # Dispatcher was NOT called.
        assert recorded == []

    def test_executor_dispatches_with_valid_token(self) -> None:
        """High-risk action + matching token → step dispatches."""
        recorded: list[dict[str, Any]] = []
        gate = ConfirmationGate()
        risk_lookup = lambda _aid: ("high", "none")
        executor = _make_executor(
            gate=gate,
            risk_lookup=risk_lookup,
            dispatch=_make_dispatch(recorded_calls=recorded),
        )
        case = _build_test_case(steps=(
            {"step_id": "s1", "action_id": "send_email"},
        ))

        result = executor.run_case(
            case,
            execution_context=ExecutionContext(
                profile="default",
                confirmation_tokens=frozenset({"send_email"}),
            ),
        )

        # Step passed (or completed via dispatcher).
        sr = result.step_results[0]
        assert sr.status.value in {"passed", "skipped"}
        # Dispatcher WAS called.
        assert len(recorded) == 1
        assert recorded[0]["action_id"] == "send_email"

    def test_executor_no_gate_works(self) -> None:
        """Backward compat: no gate wired → dispatch as normal,
        regardless of risk."""
        recorded: list[dict[str, Any]] = []
        executor = _make_executor(
            gate=None,
            risk_lookup=lambda _aid: ("high", "none"),
            dispatch=_make_dispatch(recorded_calls=recorded),
        )
        case = _build_test_case(steps=(
            {"step_id": "s1", "action_id": "send_email"},
        ))

        result = executor.run_case(case)

        # Dispatcher was called — no gate, no block.
        assert len(recorded) == 1
        sr = result.step_results[0]
        assert sr.status.value in {"passed", "skipped"}

    def test_executor_skips_gate_for_low_risk(self) -> None:
        """Low-risk action with gate wired → no confirmation check,
        no false positive block."""
        recorded: list[dict[str, Any]] = []
        gate = ConfirmationGate()
        risk_lookup = lambda _aid: ("low", "none")
        executor = _make_executor(
            gate=gate,
            risk_lookup=risk_lookup,
            dispatch=_make_dispatch(recorded_calls=recorded),
        )
        case = _build_test_case(steps=(
            {"step_id": "s1", "action_id": "read_window"},
        ))

        result = executor.run_case(
            case,
            execution_context=ExecutionContext(profile="default"),
        )

        assert len(recorded) == 1
        sr = result.step_results[0]
        assert sr.status.value in {"passed", "skipped"}
        # No confirmation_required error.
        assert sr.error is None or sr.error.get("code") != "confirmation_required"


# ---------------------------------------------------------------------
# Plan cannot bypass confirmation (R1)
# ---------------------------------------------------------------------


class TestPlanCannotBypass:
    """R1: plan payload cannot smuggle a confirmation claim.

    The plan's ActionStep has no `confirmation` / `confirmed` /
    `requires_token` field. The LLM cannot write these. Even if
    the LLM tried, the executor ignores them.
    """

    def test_plan_payload_has_no_confirmation_field(self) -> None:
        """ActionStep fields do NOT include any confirmation claim."""
        from protocol_models import ActionStep
        field_names = {f.name for f in ActionStep.__dataclass_fields__.values()}
        for forbidden in (
            "confirmation",
            "confirmed",
            "confirmation_token",
            "user_approved",
            "skip_confirmation",
        ):
            assert forbidden not in field_names, (
                f"ActionStep must NOT carry {forbidden!r}"
            )

    def test_executor_ignores_sneaked_confirmation_args(self) -> None:
        """Even if the LLM smuggles `confirmation=true` into args,
        the executor's gate decision is unchanged."""
        recorded: list[dict[str, Any]] = []
        gate = ConfirmationGate()
        risk_lookup = lambda _aid: ("high", "none")
        executor = _make_executor(
            gate=gate,
            risk_lookup=risk_lookup,
            dispatch=_make_dispatch(recorded_calls=recorded),
        )
        # The plan smuggles confirmation=true in args.
        case = _build_test_case(steps=(
            {
                "step_id": "s1",
                "action_id": "send_email",
                "args": {"confirmation": True, "to": "alice"},
            },
        ))

        result = executor.run_case(
            case,
            # NO token supplied.
            execution_context=ExecutionContext(profile="default"),
        )

        # Blocked regardless of args.
        sr = result.step_results[0]
        assert sr.status.value == "blocked"
        assert sr.error["code"] == "confirmation_required"
        assert recorded == []

    def test_executor_does_not_dispatch_when_required(self) -> None:
        """D14: a required decision MUST NOT trigger dispatch."""
        recorded: list[dict[str, Any]] = []
        gate = ConfirmationGate()
        risk_lookup = lambda _aid: ("high", "none")
        executor = _make_executor(
            gate=gate,
            risk_lookup=risk_lookup,
            dispatch=_make_dispatch(recorded_calls=recorded),
        )
        case = _build_test_case(steps=(
            {"step_id": "s1", "action_id": "send_email"},
        ))

        result = executor.run_case(
            case,
            execution_context=ExecutionContext(profile="default"),
        )

        # Dispatcher is NEVER called when gate requires.
        assert recorded == []
        # Step is blocked.
        assert result.step_results[0].status.value == "blocked"


# ---------------------------------------------------------------------
# Token scoping (R1 + D14)
# ---------------------------------------------------------------------


class TestTokenScoping:
    """Token for action_a does NOT grant confirmation for action_b."""

    def test_token_for_other_action_does_not_grant_confirmation(
        self,
    ) -> None:
        """action_b is high-risk; token for action_a doesn't help."""
        recorded: list[dict[str, Any]] = []
        gate = ConfirmationGate()
        risk_lookup = lambda aid: ("high", "none") if aid == "action_b" else ("low", "none")
        executor = _make_executor(
            gate=gate,
            risk_lookup=risk_lookup,
            dispatch=_make_dispatch(recorded_calls=recorded),
        )
        case = _build_test_case(steps=(
            {"step_id": "s1", "action_id": "action_b"},
        ))

        result = executor.run_case(
            case,
            execution_context=ExecutionContext(
                profile="default",
                confirmation_tokens=frozenset({"action_a"}),
            ),
        )

        # action_b blocked.
        assert result.step_results[0].status.value == "blocked"
        assert result.step_results[0].error["code"] == "confirmation_required"
        assert recorded == []


# ---------------------------------------------------------------------
# Side-effect gating per profile
# ---------------------------------------------------------------------


class TestSideEffectProfileGating:
    """Side-effect triggers only when the profile opts in."""

    def test_strict_profile_blocks_gui_mutation(self) -> None:
        """Strict profile + gui_mutation action → blocked."""
        policy = ConfirmationPolicy(
            profile_overrides={"strict": {"gui_mutation": True}},
        )
        gate = ConfirmationGate(policy)
        risk_lookup = lambda _aid: ("low", "gui_mutation")
        recorded: list[dict[str, Any]] = []
        executor = _make_executor(
            gate=gate,
            risk_lookup=risk_lookup,
            dispatch=_make_dispatch(recorded_calls=recorded),
        )
        case = _build_test_case(steps=(
            {"step_id": "s1", "action_id": "click_button"},
        ))

        result = executor.run_case(
            case,
            execution_context=ExecutionContext(profile="strict"),
        )

        assert result.step_results[0].status.value == "blocked"
        assert result.step_results[0].error["code"] == "confirmation_required"
        assert result.step_results[0].error["side_effect"] == "gui_mutation"
        assert recorded == []

    def test_default_profile_passes_gui_mutation(self) -> None:
        """Default profile + gui_mutation action → passes."""
        policy = ConfirmationPolicy(
            profile_overrides={"strict": {"gui_mutation": True}},
        )
        gate = ConfirmationGate(policy)
        risk_lookup = lambda _aid: ("low", "gui_mutation")
        recorded: list[dict[str, Any]] = []
        executor = _make_executor(
            gate=gate,
            risk_lookup=risk_lookup,
            dispatch=_make_dispatch(recorded_calls=recorded),
        )
        case = _build_test_case(steps=(
            {"step_id": "s1", "action_id": "click_button"},
        ))

        result = executor.run_case(
            case,
            execution_context=ExecutionContext(profile="default"),
        )

        # Default profile does NOT opt-in → no block.
        assert len(recorded) == 1
        sr = result.step_results[0]
        assert sr.error is None or sr.error.get("code") != "confirmation_required"


# ---------------------------------------------------------------------
# Determinism at the executor boundary
# ---------------------------------------------------------------------


class TestExecutorDeterminism:
    """D14: same context + same plan → same outcome, every time."""

    def test_same_inputs_same_blocked_decision(self) -> None:
        """Two runs with the same plan + context produce the same
        blocked-step outcome."""
        gate = ConfirmationGate()
        risk_lookup = lambda _aid: ("high", "none")
        case = _build_test_case(steps=(
            {"step_id": "s1", "action_id": "send_email"},
        ))
        ctx = ExecutionContext(profile="default")

        results = [
            _make_executor(
                gate=gate,
                risk_lookup=risk_lookup,
            ).run_case(case, execution_context=ctx)
            for _ in range(3)
        ]

        # All 3 runs blocked with the same code.
        for r in results:
            sr = r.step_results[0]
            assert sr.status.value == "blocked"
            assert sr.error["code"] == "confirmation_required"
            assert sr.error["action_id"] == "send_email"


# ---------------------------------------------------------------------
# Error payload structure (D14 reject contract)
# ---------------------------------------------------------------------


class TestErrorPayloadContract:
    """D14: reject payload carries code + action_id."""

    def test_reject_payload_has_required_fields(self) -> None:
        """error.code == 'confirmation_required', action_id present."""
        gate = ConfirmationGate()
        risk_lookup = lambda _aid: ("high", "none")
        executor = _make_executor(
            gate=gate,
            risk_lookup=risk_lookup,
        )
        case = _build_test_case(steps=(
            {"step_id": "s1", "action_id": "send_email"},
        ))

        result = executor.run_case(
            case,
            execution_context=ExecutionContext(profile="default"),
        )

        err = result.step_results[0].error
        assert err is not None
        # Required D14 fields.
        assert err["code"] == "confirmation_required"
        assert err["action_id"] == "send_email"
        # Audit fields (R3).
        assert err["reason"] == "confirmation_required"
        assert err["risk"] == "high"
        assert "side_effect" in err
        assert "profile" in err

    def test_reject_payload_does_not_contain_token(self) -> None:
        """The error payload MUST NOT echo back the supplied
        confirmation token (no token leakage in logs/reports)."""
        gate = ConfirmationGate()
        risk_lookup = lambda _aid: ("high", "none")
        executor = _make_executor(
            gate=gate,
            risk_lookup=risk_lookup,
        )
        case = _build_test_case(steps=(
            {"step_id": "s1", "action_id": "send_email"},
        ))

        result = executor.run_case(
            case,
            execution_context=ExecutionContext(
                profile="default",
                # A wrong token for a different action.
                confirmation_tokens=frozenset({"some_other_action"}),
            ),
        )

        err = result.step_results[0].error
        # No token string leaks.
        assert "token" not in err
        assert "confirmation_tokens" not in err


# ---------------------------------------------------------------------
# Required step abort propagation (interaction with FR-P1.2-?? abort)
# ---------------------------------------------------------------------


class TestAbortPropagation:
    """A blocked confirmation step aborts the case if required."""

    def test_required_blocked_step_aborts_case(self) -> None:
        """High-risk required step + no token → case aborts."""
        gate = ConfirmationGate()
        risk_lookup = lambda _aid: ("high", "none")
        executor = _make_executor(
            gate=gate,
            risk_lookup=risk_lookup,
        )
        case = _build_test_case(steps=(
            {"step_id": "s1", "action_id": "send_email", "required": True},
            {"step_id": "s2", "action_id": "read_window"},
        ))

        result = executor.run_case(
            case,
            execution_context=ExecutionContext(profile="default"),
        )

        # First step blocked; second step skipped (abort propagation).
        assert result.step_results[0].status.value == "blocked"
        assert result.step_results[1].status.value == "skipped"
        # Case outcome is blocked (required step blocked).
        assert result.outcome.value == "blocked"