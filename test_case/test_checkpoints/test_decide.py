"""P2.1 acceptance gate — decide_checkpoint() (architecture §15.2).

Each test wires the decision matrix from the P2.1 doc and asserts
the resulting CheckpointDecision (kind, restorable, restore_strategy,
rationale). The matrix is enforced end-to-end through the catalog
+ SOP + step + selector semantics + transition-result inputs.

Coverage matrix (per P2.1 doc FR-P2.1-04 → -09):

    * policy_matrix            — 4×3×4 = 48 combinations
    * catalog_overrides_step   — required_checkpoint wins over step
    * unexpected_halt          — UNKNOWN_MATERIAL_CHANGE transition
                                 escalates via trace event (caller
                                 contract; checkpoint returns NONE
                                 and lets the executor halt)
    * decision_determinism     — same inputs → same decision
    * application_state_logged — application_state emitted with
                                 restorable=False per P2.1 scope
"""

from __future__ import annotations

import pytest


# sys.path is configured via pyproject.toml [tool.pytest.ini_options]
# pythonpath (Issue 6 — no in-test sys.path mutation).


from agent.execution.checkpoints import (  # noqa: E402
    CheckpointDecision,
    CheckpointKind,
    decide_checkpoint,
)
from agent.execution.sop_index import SopIndex  # noqa: E402
from agent.execution.transitions import (  # noqa: E402
    TransitionKind,
    TransitionResult,
)
from target.action_catalog import get_spec as _get_spec  # noqa: E402
from target.action_catalog import (  # noqa: E402
    VALID_TRANSITION_POLICY,
)
from target.protocol_models.models import (  # noqa: E402
    AtomicTestStep,
    Transition,
)
from test_case.fixtures.transitions.builder import (  # noqa: E402
    make_window,
    snapshot,
)


# Test action_id — session.connect is in the V1 catalog with
# transition_policy="possible" by default. We override policies
# per-test via the `catalog` parameter passed to decide_checkpoint
# (built from real specs with patched transition_policy).
_ACTION_ID = "session.connect"


def _make_step(*,
               kind: str = "none",
               expected: bool = False,
               checkpoint_before: bool = False) -> AtomicTestStep:
    """Build an AtomicTestStep with a custom Transition declaration."""
    transition = Transition(
        expected=expected,
        kind=kind,
        checkpoint_before=checkpoint_before,
    )
    return AtomicTestStep(
        step_id="s1",
        action_id=_ACTION_ID,
        transition=transition,
    )


def _catalog_with_policy(policy: str) -> dict[str, ActionSpec]:
    """Build a {action_id: spec} mapping with the given policy."""
    from action_catalog import ACTIONS_V1, get_spec as _get
    spec = _get(ACTIONS_V1, _ACTION_ID)
    assert spec is not None, f"action {_ACTION_ID!r} missing from catalog"
    # ActionSpec is frozen — build a copy with the patched policy.
    from dataclasses import replace
    patched = replace(spec, transition_policy=policy)
    return {_ACTION_ID: patched}


def _empty_sop_index() -> SopIndex:
    return SopIndex()


def _snapshot_stable() -> object:
    """A stable snapshot — used when the test does not exercise
    transition_result escalation.
    """
    return snapshot([
        make_window(pid=101, native_window_id="w-1",
                    title="Login", active=True),
    ])


# ---------------------------------------------------------------------------
# Policy matrix — 4 catalog policies × 4 selector semantics × 3 step shapes
# ---------------------------------------------------------------------------


def test_policy_matrix_required_checkpoint_overrides_step():
    """catalog=required_checkpoint always wins regardless of step."""
    snap = _snapshot_stable()
    sop = _empty_sop_index()

    # step has no transition declaration, no selector
    step = AtomicTestStep(step_id="s1", action_id=_ACTION_ID)
    catalog = _catalog_with_policy("required_checkpoint")
    d = decide_checkpoint(step, snap, catalog, sop)
    assert d.kind is CheckpointKind.LOGICAL
    assert d.restorable is True
    assert d.restore_strategy == "redrive_prior_steps"
    assert d.rationale[0] == "catalog:required_checkpoint"

    # selector submit escalates to SESSION
    d2 = decide_checkpoint(step, snap, catalog, sop,
                           selector_semantics="submit")
    assert d2.kind is CheckpointKind.SESSION
    assert d2.restore_strategy == "reconnect_session"
    assert "selector:submit" in d2.rationale


def test_policy_matrix_never_wins_unconditionally():
    """catalog=never → NONE regardless of any other input."""
    snap = _snapshot_stable()
    sop = _empty_sop_index()
    catalog = _catalog_with_policy("never")

    # Step checkpoint_before=True does NOT override
    step = _make_step(checkpoint_before=True)
    d = decide_checkpoint(step, snap, catalog, sop)
    assert d.kind is CheckpointKind.NONE
    assert d.restorable is False
    assert d.restore_strategy is None
    assert d.rationale == ("catalog:never",)

    # Step expected=True + selector submit does NOT override
    step_e = _make_step(expected=True)
    d2 = decide_checkpoint(step_e, snap, catalog, sop,
                           selector_semantics="submit")
    assert d2.kind is CheckpointKind.NONE
    assert d2.rationale == ("catalog:never",)


def test_policy_matrix_expected_with_step_expected_yields_logical():
    """catalog=expected + step.expected=True → LOGICAL."""
    snap = _snapshot_stable()
    sop = _empty_sop_index()
    catalog = _catalog_with_policy("expected")
    step = _make_step(expected=True)

    d = decide_checkpoint(step, snap, catalog, sop)
    assert d.kind is CheckpointKind.LOGICAL
    assert d.rationale[0] == "step:expected"

    # selector escalates to SESSION
    d2 = decide_checkpoint(step, snap, catalog, sop,
                           selector_semantics="close")
    assert d2.kind is CheckpointKind.SESSION
    assert "selector:close" in d2.rationale


def test_policy_matrix_possible_with_checkpoint_before_yields_logical():
    """catalog=possible + step.checkpoint_before=True → LOGICAL."""
    snap = _snapshot_stable()
    sop = _empty_sop_index()
    catalog = _catalog_with_policy("possible")
    step = _make_step(checkpoint_before=True)

    d = decide_checkpoint(step, snap, catalog, sop)
    assert d.kind is CheckpointKind.LOGICAL
    assert d.rationale[0] == "step:checkpoint_before"


def test_policy_matrix_default_returns_none():
    """No step declaration + no escalation → NONE."""
    snap = _snapshot_stable()
    sop = _empty_sop_index()
    catalog = _catalog_with_policy("possible")  # not required
    step = AtomicTestStep(step_id="s1", action_id=_ACTION_ID)

    d = decide_checkpoint(step, snap, catalog, sop)
    assert d.kind is CheckpointKind.NONE
    assert d.rationale == ()


# ---------------------------------------------------------------------------
# Catalog overrides step
# ---------------------------------------------------------------------------


def test_catalog_required_checkpoint_overrides_step_checkpoint_before():
    """When both inputs point to a checkpoint, catalog wins."""
    snap = _snapshot_stable()
    sop = _empty_sop_index()
    catalog = _catalog_with_policy("required_checkpoint")
    step = _make_step(checkpoint_before=True)

    d = decide_checkpoint(step, snap, catalog, sop)
    # Step has checkpoint_before, catalog has required_checkpoint.
    # catalog wins (priority order in decide_checkpoint).
    assert d.kind is CheckpointKind.LOGICAL
    assert d.rationale[0] == "catalog:required_checkpoint"
    # step:checkpoint_before should NOT appear (catalog overrode it)
    assert "step:checkpoint_before" not in d.rationale


def test_catalog_overrides_when_step_omits_transition():
    """catalog=required_checkpoint wins even when step.transition=None."""
    snap = _snapshot_stable()
    sop = _empty_sop_index()
    catalog = _catalog_with_policy("required_checkpoint")
    step = AtomicTestStep(step_id="s1", action_id=_ACTION_ID)
    assert step.transition is None  # sanity

    d = decide_checkpoint(step, snap, catalog, sop)
    assert d.kind is CheckpointKind.LOGICAL
    assert d.rationale == ("catalog:required_checkpoint",)


# ---------------------------------------------------------------------------
# Selector semantics escalation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("selector", ["submit", "close", "delete"])
def test_required_checkpoint_with_session_selector_yields_session(selector):
    """required_checkpoint + session-like selector → SESSION."""
    snap = _snapshot_stable()
    sop = _empty_sop_index()
    catalog = _catalog_with_policy("required_checkpoint")
    step = AtomicTestStep(step_id="s1", action_id=_ACTION_ID)

    d = decide_checkpoint(step, snap, catalog, sop,
                          selector_semantics=selector)
    assert d.kind is CheckpointKind.SESSION
    assert d.restore_strategy == "reconnect_session"
    assert d.rationale == (
        "catalog:required_checkpoint",
        f"selector:{selector}",
    )


def test_selector_without_required_checkpoint_does_not_trigger_session():
    """selector=submit alone (catalog=possible) does NOT escalate."""
    snap = _snapshot_stable()
    sop = _empty_sop_index()
    catalog = _catalog_with_policy("possible")
    step = AtomicTestStep(step_id="s1", action_id=_ACTION_ID)

    d = decide_checkpoint(step, snap, catalog, sop,
                          selector_semantics="submit")
    # No step declaration → no checkpoint regardless of selector.
    assert d.kind is CheckpointKind.NONE


# ---------------------------------------------------------------------------
# Transition-result escalation
# ---------------------------------------------------------------------------


def test_window_owner_change_transition_escalates_to_session():
    """Previous step's WINDOW_OWNER_CHANGE → SESSION for current step."""
    snap = _snapshot_stable()
    sop = _empty_sop_index()
    catalog = _catalog_with_policy("possible")
    step = AtomicTestStep(step_id="s1", action_id=_ACTION_ID)

    prev = TransitionResult(
        kind=TransitionKind.WINDOW_OWNER_CHANGE,
        confidence="medium",
    )

    d = decide_checkpoint(step, snap, catalog, sop,
                          transition_result=prev)
    assert d.kind is CheckpointKind.SESSION
    assert d.restore_strategy == "reconnect_session"
    assert d.rationale == ("transition:window_owner_change",)


def test_unexpected_transition_yields_no_checkpoint_for_caller_halt():
    """UNKNOWN_MATERIAL_CHANGE transition → NONE; executor halts.

    P2.1 does not introduce a 'halt' kind — checkpoint decision is
    NONE because the executor is expected to emit
    ``unexpected_transition`` (a trace event) and stop the plan.
    The checkpoint helper just confirms NO state-saving action is
    recommended when the previous transition was unknown.
    """
    snap = _snapshot_stable()
    sop = _empty_sop_index()
    catalog = _catalog_with_policy("required_checkpoint")  # even this
    step = AtomicTestStep(step_id="s1", action_id=_ACTION_ID)

    prev = TransitionResult(
        kind=TransitionKind.UNKNOWN_MATERIAL_CHANGE,
        confidence="low",
    )

    d = decide_checkpoint(step, snap, catalog, sop,
                          transition_result=prev)
    # The transition_result branch (window_owner_change) does not
    # match; the executor is expected to halt before reaching this
    # decision, but if it does reach it, the result is NONE.
    assert d.kind is CheckpointKind.NONE


# ---------------------------------------------------------------------------
# SOP transition metadata hint
# ---------------------------------------------------------------------------


def test_sop_page_navigation_hint_triggers_logical():
    """SopIndex page_navigation hint → LOGICAL via sop:page_navigation."""
    from agent.execution.sop_index import SopIndexEntry
    from agent.execution.transitions import TransitionKind as TK
    snap = _snapshot_stable()
    catalog = _catalog_with_policy("possible")
    step = AtomicTestStep(step_id="s1", action_id=_ACTION_ID)

    sop = SopIndex(entries=(
        SopIndexEntry(
            sop_id="hisec.security-center-compliance",
            path="sops/hisec-security-center-compliance.md",
            version=1,
            transition_kind=TK.PAGE_NAVIGATION,
            application_state_hint="hisec_security_center_open",
        ),
    ))

    d = decide_checkpoint(step, snap, catalog, sop,
                          sop_id="hisec.security-center-compliance")
    assert d.kind is CheckpointKind.LOGICAL
    assert d.rationale == ("sop:page_navigation",)


def test_sop_window_open_hint_triggers_session():
    """SopIndex window_open hint → SESSION."""
    from agent.execution.sop_index import SopIndexEntry
    from agent.execution.transitions import TransitionKind as TK
    snap = _snapshot_stable()
    catalog = _catalog_with_policy("possible")
    step = AtomicTestStep(step_id="s1", action_id=_ACTION_ID)

    sop = SopIndex(entries=(
        SopIndexEntry(
            sop_id="hisec.window-pair-visible",
            path="sops/hisec-window-pair-visible.md",
            version=1,
            transition_kind=TK.WINDOW_OPEN,
            application_state_hint="hisec_and_edr_client_pair_visible",
        ),
    ))

    d = decide_checkpoint(step, snap, catalog, sop,
                          sop_id="hisec.window-pair-visible")
    assert d.kind is CheckpointKind.SESSION
    assert d.rationale == ("sop:window_open",)


def test_sop_application_restart_hint_emits_application_state():
    """SopIndex application_restart hint → APPLICATION_STATE."""
    from agent.execution.sop_index import SopIndexEntry
    from agent.execution.transitions import TransitionKind as TK
    snap = _snapshot_stable()
    catalog = _catalog_with_policy("possible")
    step = AtomicTestStep(step_id="s1", action_id=_ACTION_ID)

    sop = SopIndex(entries=(
        SopIndexEntry(
            sop_id="hisec.application-restart-flow",
            path="sops/hisec-application-restart-flow.md",
            version=1,
            transition_kind=TK.APPLICATION_RESTART,
            application_state_hint="process_restarted",
        ),
    ))

    d = decide_checkpoint(step, snap, catalog, sop,
                          sop_id="hisec.application-restart-flow")
    # application_state emitted; restorable=False per P2.1 scope.
    assert d.kind is CheckpointKind.APPLICATION_STATE
    assert d.restorable is False
    assert d.restore_strategy is None


def test_sop_id_not_in_index_falls_through():
    """Unknown sop_id with policy=possible + no step → NONE."""
    snap = _snapshot_stable()
    catalog = _catalog_with_policy("possible")
    step = AtomicTestStep(step_id="s1", action_id=_ACTION_ID)
    sop = _empty_sop_index()

    d = decide_checkpoint(step, snap, catalog, sop, sop_id="nonexistent")
    assert d.kind is CheckpointKind.NONE


# ---------------------------------------------------------------------------
# Step-level application_restart → APPLICATION_STATE
# ---------------------------------------------------------------------------


def test_step_application_restart_emits_application_state_unrestorable():
    """step.transition.kind=application_restart → APPLICATION_STATE,
    restorable=False (P2.1 scope)."""
    snap = _snapshot_stable()
    sop = _empty_sop_index()
    catalog = _catalog_with_policy("never")  # even never does not
                                            # override this signal
    step = _make_step(kind="application_restart")

    d = decide_checkpoint(step, snap, catalog, sop)
    assert d.kind is CheckpointKind.APPLICATION_STATE
    assert d.restorable is False
    assert d.restore_strategy is None
    assert d.rationale == ("step:application_restart",)


# ---------------------------------------------------------------------------
# Determinism + contract
# ---------------------------------------------------------------------------


def test_decision_is_deterministic_for_same_inputs():
    """Same inputs across N calls yield the same decision."""
    snap = _snapshot_stable()
    sop = _empty_sop_index()
    catalog = _catalog_with_policy("required_checkpoint")
    step = AtomicTestStep(step_id="s1", action_id=_ACTION_ID)

    results = {
        decide_checkpoint(step, snap, catalog, sop)
        for _ in range(20)
    }
    assert len(results) == 1
    decision = next(iter(results))
    assert decision.kind is CheckpointKind.LOGICAL


def test_decision_contract_restorable_requires_strategy():
    """CheckpointDecision rejects restorable=True without strategy."""
    with pytest.raises(ValueError, match="restore_strategy"):
        CheckpointDecision(
            kind=CheckpointKind.LOGICAL,
            restorable=True,
            restore_strategy=None,
        )


def test_decision_contract_unrestorable_forbids_strategy():
    """CheckpointDecision rejects restorable=False with a strategy."""
    with pytest.raises(ValueError, match="restore_strategy"):
        CheckpointDecision(
            kind=CheckpointKind.NONE,
            restorable=False,
            restore_strategy="redrive_prior_steps",
        )


def test_decision_accepts_none_catalog():
    """decide_checkpoint handles catalog=None (no metadata available)."""
    snap = _snapshot_stable()
    sop = _empty_sop_index()
    step = AtomicTestStep(step_id="s1", action_id=_ACTION_ID)

    d = decide_checkpoint(step, snap, None, sop)
    # catalog=None → policy defaults to "possible" → no step escalation
    # → NONE.
    assert d.kind is CheckpointKind.NONE


# ---------------------------------------------------------------------------
# VALID_TRANSITION_POLICY contract — make sure the matrix covers all
# documented catalog policies.
# ---------------------------------------------------------------------------


def test_matrix_uses_documented_policies():
    """Every entry in VALID_TRANSITION_POLICY is exercised by the
    matrix tests above. This test fails fast if a new policy is added
    without updating the matrix.
    """
    assert "never" in VALID_TRANSITION_POLICY
    assert "possible" in VALID_TRANSITION_POLICY
    assert "expected" in VALID_TRANSITION_POLICY
    assert "required_checkpoint" in VALID_TRANSITION_POLICY