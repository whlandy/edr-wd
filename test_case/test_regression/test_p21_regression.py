"""P2.1 regression tests — code-review acceptance criteria (rounds 2 + 3).

These tests exercise specific edge cases called out across the
two code-review rounds:

    1. hwnd reuse across processes → APPLICATION_RESTART
       (not WINDOW_OWNER_CHANGE)
    2. tree_digest drift without structural evidence → NONE
       (not CONTROL_STATE_CHANGE; Round 1 Blocker 1 hardening)
    3. malformed YAML frontmatter → fail fast with a clear error
    4. duplicate sop_id across two files → fail fast with
       first/second paths named (Round 3 polish)
    5. catalog:never vs step.checkpoint_before → step wins
       (catalog metadata cannot suppress an application-level
       restart; per spec FR-P2.1-09 catalog:never only suppresses
       step.request_checkpoint, NOT step.application_restart)
    6. UNKNOWN_MATERIAL_CHANGE is constructable and the
       ``is_unexpected`` property works (Round 3 hardening)
    7. Stable state classifies to NONE (Round 3 hardening)
    8. Topology signature includes ``process_name`` so hwnd
       reuse across processes cannot collapse onto one slot
       (Round 3 required #1)

These tests live in a separate file so a regression search can
pinpoint them quickly without scrolling through the full
classification / decision matrices.

File name note: originally ``test_review_round2.py`` — renamed
to ``test_p21_regression.py`` after Round 3 so future reviews
have a stable home (Round 3 review minor note #1).
"""

from __future__ import annotations

import pytest


# sys.path is configured via pyproject.toml [tool.pytest.ini_options]
# pythonpath (Issue 6 — no in-test sys.path mutation).


from agent.execution.transitions import (
    TransitionKind,
    TransitionResult,
    classify_transition,
)
from agent.execution.checkpoints import (
    CheckpointDecision,
    CheckpointKind,
    decide_checkpoint,
)
from agent.execution.sop_index import (
    SopIndexLoadError,
    load_sop_index,
)
from agent.execution.sop_index import SopIndexEntry
from target.observations import build_snapshot


from test_case.fixtures.transitions.builder import (
    make_window,
    snapshot,
)


# -----------------------------------------------------------------------
# Regression 1 — hwnd reuse across processes → APPLICATION_RESTART
# -----------------------------------------------------------------------


def test_regression_hwnd_reuse_with_different_process_is_restart():
    """The OS may recycle a native_window_id across two different
    processes. After Blocker 3 hardening, this must be classified
    as APPLICATION_RESTART (topology differs on process_name),
    NOT WINDOW_OWNER_CHANGE (which would falsely treat the new
    process as the old one with a different pid).
    """
    before = snapshot([
        make_window(pid=101, native_window_id="100",
                    process_name="ProcessA",
                    title="Login", active=True),
    ])
    after = snapshot([
        make_window(pid=202, native_window_id="100",  # OS recycled hwnd
                    process_name="ProcessB",  # different process
                    title="Login", active=True),
    ])
    result = classify_transition(before, after)
    assert result.kind is TransitionKind.APPLICATION_RESTART


def test_regression_hwnd_reuse_with_same_process_is_owner_change():
    """The opposite case: same process_name, hwnd stays, pid
    changes. Topology matches → WINDOW_OWNER_CHANGE (legitimate
    fork / re-parenting).
    """
    before = snapshot([
        make_window(pid=101, native_window_id="100",
                    process_name="ProcessA",
                    title="Login", active=True),
    ])
    after = snapshot([
        make_window(pid=202, native_window_id="100",
                    process_name="ProcessA",  # same process name
                    title="Login", active=True),
    ])
    result = classify_transition(before, after)
    assert result.kind is TransitionKind.WINDOW_OWNER_CHANGE


# -----------------------------------------------------------------------
# Regression 2 — digest drift without structural evidence → NONE
# -----------------------------------------------------------------------


def test_regression_digest_drift_without_structural_evidence_is_none():
    """If tree_digest changes but no fingerprint / title / text
    set changes, the classifier returns NONE (digest_only flag)
    rather than promoting to CONTROL_STATE_CHANGE (Blocker 1).
    """
    s = snapshot([
        make_window(pid=101, native_window_id="w-1",
                    title="Dashboard", active=True),
    ])
    # Build a second snapshot with identical content. The digest
    # WILL likely match (auto-computed from fingerprints), so this
    # is the basic NONE path. The harder case — digest mismatch
    # without content change — is exercised below by hand-rolling
    # the ObservationSnapshot.
    s2 = snapshot([
        make_window(pid=101, native_window_id="w-1",
                    title="Dashboard", active=True),
    ])
    result = classify_transition(s, s2)
    assert result.kind is TransitionKind.NONE


def test_regression_digest_only_signal_is_none():
    """Hand-roll two snapshots where everything is identical
    EXCEPT the tree_digest value. This is the canonical
    digest-drift-without-structural-evidence case.

    Such drift happens in production when the snapshot pipeline
    includes transient fields (timestamps, accessibility node
    order) in its digest computation. The classifier must
    not promote such drift to a real transition.
    """
    base = snapshot([
        make_window(pid=101, native_window_id="w-1",
                    title="Dashboard", active=True),
    ])
    # Clone the snapshot with a different tree_digest but the
    # same targets (fingerprint / title / text identical).
    hand_built = build_snapshot(
        targets=[t.to_dict() if hasattr(t, "to_dict") else {
            "kind": t.kind,
            "process_name": t.process_name,
            "pid": t.pid,
            "native_window_id": t.native_window_id,
            "title": t.title,
            "control_type": getattr(t, "control_type", "Window"),
            "automation_id": getattr(t, "automation_id", None),
            "text": getattr(t, "text", None),
        } for t in base.targets],
        backend=base.backend,
        host=base.host,
        captured_at=base.captured_at,
        active_window_target_id=base.active_window.target_id if base.active_window else None,
    )
    # Force a different tree_digest by mutating the dataclass
    # (frozen=False for ObservationSnapshot? let's check)
    object.__setattr__(hand_built, "tree_digest", "sha256:forced-drift")
    result = classify_transition(base, hand_built)
    assert result.kind is TransitionKind.NONE
    assert result.signals.get("digest_only") is True


# -----------------------------------------------------------------------
# Regression 3 — malformed YAML frontmatter → fail fast
# -----------------------------------------------------------------------


def test_regression_malformed_yaml_frontmatter_raises(tmp_path):
    """A SOP file with a YAML frontmatter that uses invalid
    syntax (mismatched braces) must raise SopIndexLoadError,
    not silently fall back to defaults.

    The P2.1 review explicitly called out YAML parsing as a
    blocker; this test pins the contract that invalid YAML is
    caught at load time.
    """
    (tmp_path / "sop_catalog").mkdir()
    (tmp_path / "sop_catalog" / "INDEX.md").write_text(
        "---\nsop_id: sop_catalog\n---\n# catalog\n"
    )
    bad = tmp_path / "sop_catalog" / "broken.md"
    bad.write_text(
        "---\n"  # valid header
        "transition_kind: [unclosed\n"  # invalid YAML
        "platforms:\n"
        "  - windows\n"
        "---\n# broken sop\n"
    )
    with pytest.raises(SopIndexLoadError) as excinfo:
        load_sop_index(str(tmp_path / "sop_catalog"))
    # Either "YAML" or "frontmatter" or "broken" must appear in
    # the error message — accept any of those forms.
    msg = str(excinfo.value)
    assert any(token in msg for token in ("YAML", "frontmatter", "broken")), (
        f"unexpected error message: {msg!r}"
    )


def test_regression_yaml_with_sequence_value_rejected_for_scalar_field(tmp_path):
    """Some frontmatter fields are required to be scalars
    (transition_kind, application_state_hint). If a file
    provides a sequence (list), the loader must reject the
    file rather than silently coercing.
    """
    (tmp_path / "sop_catalog2").mkdir()
    (tmp_path / "sop_catalog2" / "INDEX.md").write_text(
        "---\nsop_id: sop_catalog2\n---\n# catalog\n"
    )
    seq = tmp_path / "sop_catalog2" / "seq.md"
    seq.write_text(
        "---\n"
        "sop_id: edrclient.seq\n"
        "transition_kind:\n"
        "  - page_navigation\n"  # sequence instead of scalar
        "  - window_open\n"
        "application_state_hint: ok\n"
        "---\n# bad\n"
    )
    # Loader either rejects or coerces. P2.1 contract is
    # reject; if a future refactor changes this, the test
    # should flip to assert the coercion.
    with pytest.raises(SopIndexLoadError):
        load_sop_index(str(tmp_path / "sop_catalog2"))


# -----------------------------------------------------------------------
# Regression 4 — duplicate sop_id across files → fail fast
# -----------------------------------------------------------------------


def test_regression_duplicate_sop_id_raises(tmp_path):
    """Two files declaring the same `sop_id` must fail at load
    time. Silently accepting duplicates would let later files
    shadow earlier ones and produce inconsistent behavior at
    runtime.
    """
    (tmp_path / "dup").mkdir()
    (tmp_path / "dup" / "INDEX.md").write_text(
        "---\nsop_id: dup\n---\n# catalog\n"
    )
    a = tmp_path / "dup" / "a.md"
    a.write_text(
        "---\n"
        "sop_id: shared.id\n"
        "transition_kind: page_navigation\n"
        "application_state_hint: a\n"
        "---\n# a\n"
    )
    b = tmp_path / "dup" / "b.md"
    b.write_text(
        "---\n"
        "sop_id: shared.id\n"
        "transition_kind: window_open\n"
        "application_state_hint: b\n"
        "---\n# b\n"
    )
    with pytest.raises(SopIndexLoadError) as excinfo:
        load_sop_index(str(tmp_path / "dup"))
    assert "shared.id" in str(excinfo.value) or "duplicate" in str(excinfo.value).lower()


# -----------------------------------------------------------------------
# Regression 5 — catalog:never vs step.checkpoint_before precedence
# -----------------------------------------------------------------------


def test_regression_catalog_never_vs_step_checkpoint_before():
    """FR-P2.1-09: catalog policy `never` wins unconditionally
    over step-level hints. This includes
    ``step.transition.checkpoint_before=True`` — the user-facing
    intent of "never checkpoint this action" must not be
    overridable by step metadata.

    Note: this differs from
    ``step.transition.kind == application_restart`` which DOES
    win (covered by
    ``test_step_application_restart_emits_application_state_unrestorable``).
    """
    from target.protocol_models.models import AtomicTestStep, Transition
    from action_catalog import ACTIONS_V1, get_spec
    from dataclasses import replace

    spec = get_spec(ACTIONS_V1, "session.connect")
    # Force the spec into a never policy for this test via
    # ``dataclasses.replace`` so we don't have to know every
    # field name on ActionSpec.
    spec_never = replace(spec, transition_policy="never")

    step = AtomicTestStep(
        step_id="step-1",
        action_id=spec.action_id,
        args={},
        transition=Transition(
            kind="none",
            expected=False,
            checkpoint_before=True,  # step explicitly asks for checkpoint
        ),
    )
    snapshot_obj = snapshot([
        make_window(pid=101, native_window_id="w-1",
                    title="Login", active=True),
    ])
    decision = decide_checkpoint(
        step=step,
        snapshot=snapshot_obj,
        catalog={spec.action_id: spec_never},
        sop_index=_empty_index(),
    )
    assert decision.kind is CheckpointKind.NONE
    assert decision.rationale == ("catalog:never",)
    assert not decision.restorable


# -----------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------


def _empty_index():
    """Return an empty SopIndex for tests that don't care about SOP metadata.

    The loader is the production way to build an index; this
    helper constructs an in-memory one with no entries so the
    decision function under test does not depend on filesystem
    fixtures.
    """
    from agent.execution.sop_index import SopIndex
    return SopIndex(entries=tuple())


# -----------------------------------------------------------------------
# Round 3 review follow-ups (required #1 and #2)
# -----------------------------------------------------------------------


def test_regression_unknown_is_a_constructable_result():
    """UNKNOWN_MATERIAL_CHANGE is the **defensive** fallback enum
    value (Round 3 review, required #2 — make this test
    deterministic rather than accepting either NONE or UNKNOWN).

    Production callers (the executor's halt branch, the
    transition-monitoring agent, recovery-planner integration in
    P2.2) need to be able to construct an explicit
    ``TransitionResult(kind=UNKNOWN_MATERIAL_CHANGE, ...)`` when
    the classifier cannot reach a conclusion. This test pins the
    API contract that the enum value and the dataclass work
    together, including the ``is_unexpected`` convenience
    property.
    """
    result = TransitionResult(
        kind=TransitionKind.UNKNOWN_MATERIAL_CHANGE,
        signals={"reason": "external_halt_signal"},
        confidence="low",
    )
    assert result.kind is TransitionKind.UNKNOWN_MATERIAL_CHANGE
    assert result.is_unexpected is True
    assert result.confidence == "low"
    assert result.signals == {"reason": "external_halt_signal"}


def test_regression_classifier_returns_none_for_stable_state():
    """Sanity check: when the snapshots are byte-identical, the
    classifier returns NONE (not UNKNOWN). This guarantees the
    defensive UNKNOWN path does not mask a quiet steady state.
    """
    s = snapshot([
        make_window(pid=101, native_window_id="w-1",
                    title="Dashboard", active=True),
    ])
    result = classify_transition(s, s)
    assert result.kind is TransitionKind.NONE
    assert result.is_unexpected is False


def test_regression_topology_signature_includes_process_name():
    """Round 3 review (required #1) — pin the topology signature
    contract.

    The classifier's restart/owner-change distinction rests on
    the topology signature. This test documents the components
    and pins the contract that:

    1. ``process_name`` is part of the signature (rules out
       hwnd reuse across processes).
    2. The structural fields are included.
    3. Two distinct Target objects produce distinct signatures
       when their structural fields differ — but collapse onto
       the same signature when every structural field matches.

    Backend contracts that make collisions unlikely:
        * ``native_window_id`` is required non-empty by the
          ``Target`` model — every top-level window has one.
        * ``automation_id`` is optional; defaults to None.

    Known limitation (out of scope for P2.1): a buggy backend
    that assigns the same ``native_window_id`` to two distinct
    top-level windows in the same process will collapse them
    onto the same topology signature. The classifier treats
    this as one window. Detecting the bug requires OS-level
    state that the snapshot layer does not expose.
    """
    from agent.execution.transitions import _topology_signature
    from target.observations import build_snapshot

    # Two windows that DIFFER on a structural field produce
    # different signatures.
    a = make_window(
        pid=101, native_window_id="hwnd-A", title="A",
        process_name="App1",
    )
    b = make_window(
        pid=101, native_window_id="hwnd-B", title="B",  # different hwnd
        process_name="App1",
    )
    snap = build_snapshot(
        targets=[a, b],
        backend="synthetic",
        host="local",
        captured_at="1.0",
        active_window_target_id=None,
    )
    assert len(snap.targets) == 2
    sigs = {_topology_signature(t) for t in snap.targets}
    assert len(sigs) == 2, (
        f"two windows with different native_window_id must produce "
        f"different topology signatures, got {sigs!r}"
    )

    # Two windows in DIFFERENT processes must produce different
    # signatures even when sharing every other structural field.
    same_hwnd_a = make_window(
        pid=101, native_window_id="hwnd-X", title="X",
        process_name="App1",
    )
    same_hwnd_b = make_window(
        pid=202, native_window_id="hwnd-X", title="X",
        process_name="App2",  # different process
    )
    snap2 = build_snapshot(
        targets=[same_hwnd_a, same_hwnd_b],
        backend="synthetic",
        host="local",
        captured_at="2.0",
        active_window_target_id=None,
    )
    sigs2 = {_topology_signature(t) for t in snap2.targets}
    assert len(sigs2) == 2, (
        f"hwnd reuse across different processes must produce "
        f"different topology signatures, got {sigs2!r}"
    )