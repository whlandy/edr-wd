"""transitions.py — P2.1 transition classification (architecture §15.1).

Given a pair of `ObservationSnapshot`s captured before/after an action,
:classify_transition` returns a :class:`TransitionResult` describing
what changed between them. Coordinates alone never establish a
transition (P2.1 doc line 39): only structural signals contribute.

Signals observed (architecture §15.1):
    1. Top-level window set                (process + native_window_id)
    2. Owner PID / process name
    3. Native window ID
    4. Active window
    5. Modal role heuristic               (control_type in {Dialog, Pane})
    6. Navigation title delta             (any Target.title under same window)
    7. Tree root digest                   (Target.fingerprint under same window)
    8. Stable target survival             (fingerprint equality across snapshots)
    9. Normalized tree digest             (ObservationSnapshot.tree_digest)

NOTE — relation to protocol_models (Issue 4, code review):

    `target.protocol_models.enums` exposes ``VALID_TRANSITION_KINDS`` as a
    ``frozenset[str]`` (the wire schema). The string values in that
    set match this enum's ``.value`` attributes exactly. P2.1
    introduces ``TransitionKind`` as the only Python enum
    representation so classifier code can use exhaustive matching
    (``match``/``is``) without string comparisons.

    Keep the two in sync:

    * Add a new transition kind → add it to both
      ``VALID_TRANSITION_KINDS`` and this enum.
    * Rename a kind → rename in both places; bump a ``__rev__``
      constant if the wire schema is involved.

    A protocol-side enum was avoided here to keep P2.1 from
    dragging wire-schema changes into the runtime classifier.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, Mapping

from target.observations.models import ObservationSnapshot, Target


# ---------------------------------------------------------------------------
# TransitionKind — V1 enumeration aligned with target.protocol_models
# ---------------------------------------------------------------------------


class TransitionKind(str, Enum):
    """Classifier output — what kind of transition occurred."""

    NONE = "none"
    CONTROL_STATE_CHANGE = "control_state_change"
    PAGE_NAVIGATION = "page_navigation"
    MODAL_OPEN = "modal_open"
    MODAL_CLOSE = "modal_close"
    WINDOW_OPEN = "window_open"
    WINDOW_CLOSE = "window_close"
    WINDOW_OWNER_CHANGE = "window_owner_change"
    APPLICATION_RESTART = "application_restart"
    UNKNOWN_MATERIAL_CHANGE = "unknown_material_change"


VALID_TRANSITION_KINDS: frozenset[TransitionKind] = frozenset(TransitionKind)


# ---------------------------------------------------------------------------
# TransitionResult — classifier output envelope
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TransitionResult:
    """What changed between two snapshots.

    `signals` is a free-form debug aid: a dict of named observations
    that contributed to the decision (e.g. ``new_windows``, ``pid_set``).
    `confidence` is one of ``"high" | "medium" | "low"`` — low signals
    force the executor to halt (P2.1 FR-P2.1-04).
    """

    kind: TransitionKind
    signals: Mapping[str, Any] = field(default_factory=dict)
    confidence: str = "high"

    _ALLOWED_CONFIDENCE: frozenset[str] = frozenset({"high", "medium", "low"})

    def __post_init__(self) -> None:
        if self.confidence not in self._ALLOWED_CONFIDENCE:
            raise ValueError(
                f"confidence must be one of {sorted(self._ALLOWED_CONFIDENCE)}, "
                f"got {self.confidence!r}"
            )

    @property
    def is_unexpected(self) -> bool:
        """True if the result is UNKNOWN_MATERIAL_CHANGE (FR-P2.1-04)."""
        return self.kind is TransitionKind.UNKNOWN_MATERIAL_CHANGE


# ---------------------------------------------------------------------------
# Modal heuristic
# ---------------------------------------------------------------------------


# Heuristic modal control_types (architecture §15.1). Both UIA
# ("Pane", "Dialog") and macOS Accessibility ("AXDialog") names are
# accepted. Misses are intentional: better to under-detect than to
# false-positive a navigation event as modal_open.
_MODAL_CONTROL_TYPES: frozenset[str] = frozenset({
    "Pane",
    "Dialog",
    "AXDialog",
    "AXSheet",
    "Sheet",
    "AXAlert",
})


def _is_modal(target: Any) -> bool:
    """Return True if the target looks like a modal surface.

    Detection heuristic (architecture §15.1):

        1. control_type in the modal set (most reliable), OR
        2. kind == "window" with control_type == "Pane" (some UIA
           dialogs report as Pane when Window role is stripped), OR
        3. title contains "Dialog" / "Confirm" / "确认" (very
           conservative — keeps false-positive rate low).

    Controls inside a window are never modal even if their
    control_type matches; we only check window-kind targets.
    """
    if getattr(target, "kind", None) != "window":
        return False
    ctype = getattr(target, "control_type", None) or ""
    if ctype in _MODAL_CONTROL_TYPES:
        return True
    title = getattr(target, "title", "") or ""
    # Case-insensitive contains — no false positives with these tokens
    # in known SOPs (validated against sops/*.md headings).
    lowered = title.lower()
    if any(token in lowered for token in ("dialog", "confirm", "确认")):
        return True
    return False


# ---------------------------------------------------------------------------
# Helpers — extract signal sets from a snapshot
# ---------------------------------------------------------------------------


# Structural fields that survive content changes (title, text) but
# die with restart (different automation_id / native_window_id /
# control_type). Used for topology signature — distinguishes
# "same window object" from "fresh window after restart" without
# confusing page-navigation content drift for a restart signal.
_STRUCTURAL_FIELDS: tuple[str, ...] = (
    "kind",
    "control_type",
    "automation_id",
    "native_window_id",
)


def _pid_set(snapshot: ObservationSnapshot) -> set[int]:
    return {t.pid for t in snapshot.targets if t.pid is not None}


def _window_set(snapshot: ObservationSnapshot) -> set[tuple[int, str]]:
    """Return the (pid, native_window_id) pairs that exist in `snapshot`.

    Only window-kind targets contribute; controls inherit the window's
    pair and are excluded to avoid double-counting.
    """
    return {
        (t.pid, t.native_window_id)
        for t in snapshot.targets
        if t.pid is not None and getattr(t, "kind", None) == "window"
    }


def _topology_signature(target) -> tuple:
    """Topological identity of a window target — survives content
    changes (title, text) but dies with restart.

    Built from ``(process_name, kind, control_type, automation_id,
    native_window_id)``. ``process_name`` rules out hwnd reuse across
    different applications (Blocker 3); the structural fields rule
    out confusing a page-navigation content drift for a window
    recreation signal.

    Distinct from ``Target.fingerprint``: the fingerprint includes
    title/text and is therefore sensitive to UI content changes
    (which is correct for ``CONTROL_STATE_CHANGE`` detection in
    Blocker 1 but wrong for restart detection).
    """
    return (
        getattr(target, "process_name", None),
        *(getattr(target, f, "") or "" for f in _STRUCTURAL_FIELDS),
        # Round 3 review (required #1) — fallback discriminator
        # note: ``Target`` model has no ``native_index`` field, so
        # this slot currently resolves to ``-1`` for every target.
        # We keep the slot anyway because:
        #
        # 1. ``assign_target_ids`` does include native_index in its
        #    sort key — a future Target schema that exposes it will
        #    light this up automatically with no classifier change.
        # 2. The slot documents the intended disambiguation layer
        #    so future maintainers know to extend it when the
        #    schema gains a positional field.
        #
        # For the current schema, real disambiguation comes from
        # ``native_window_id`` (required non-empty by the Target
        # model) and ``automation_id`` (optional). Backend bugs
        # that assign the same native_window_id to two distinct
        # windows are out of scope for the classifier.
        getattr(target, "native_index", -1),
    )


def _topology_set(snapshot: ObservationSnapshot) -> set[tuple]:
    """Set of topology signatures across window-kind targets in
    `snapshot`. Used for restart detection (Blocker 3).
    """
    return {
        _topology_signature(t)
        for t in snapshot.targets
        if getattr(t, "kind", None) == "window"
    }


def _topology_to_pid(snapshot: ObservationSnapshot) -> dict[tuple, int]:
    """Map a topology signature to its pid. First occurrence wins."""
    out: dict[tuple, int] = {}
    for t in snapshot.targets:
        if getattr(t, "kind", None) == "window" and t.pid is not None:
            out.setdefault(_topology_signature(t), t.pid)
    return out


def _structural_changes(
    before: ObservationSnapshot,
    after: ObservationSnapshot,
) -> tuple[bool, bool, bool]:
    """Return (fingerprint_changed, title_changed, text_changed).

    Set-based comparison of the fingerprint / title / text sets
    across all targets. Per-target matching via topology_signature
    is intentionally NOT done here — the architectural resolver
    (architecture §8.1) is responsible for matching the same
    underlying control across snapshots via fingerprint. P2.1 only
    needs to detect that SOME content drifted, not which specific
    target drifted.

    Blocker 1 addressed: tree_digest alone is unreliable
    (sensitive to target ordering and transient fields). This
    function requires at least one of fingerprint, title, or text
    to have actually drifted before the caller promotes a
    transition signal.
    """
    before_fps = {t.fingerprint for t in before.targets if t.fingerprint}
    after_fps = {t.fingerprint for t in after.targets if t.fingerprint}
    before_titles = {t.title for t in before.targets if t.title}
    after_titles = {t.title for t in after.targets if t.title}
    before_texts = {t.text for t in before.targets if t.text}
    after_texts = {t.text for t in after.targets if t.text}
    return (
        before_fps != after_fps,
        before_titles != after_titles,
        before_texts != after_texts,
    )


def _active_pid(snapshot: ObservationSnapshot) -> int | None:
    aw = snapshot.active_window
    return aw.pid if (aw is not None and aw.pid is not None) else None


def _active_window_id(snapshot: ObservationSnapshot) -> str | None:
    aw = snapshot.active_window
    return aw.native_window_id if aw is not None else None


def _active_process(snapshot: ObservationSnapshot) -> str | None:
    aw = snapshot.active_window
    return aw.process_name if aw is not None else None


def _title_changed(before: ObservationSnapshot, after: ObservationSnapshot) -> bool:
    """Return True if any window-kind target's title changed.

    Controls are ignored — only window titles count for navigation.
    """
    before_titles = {
        (t.pid, t.native_window_id): t.title
        for t in before.targets
        if t.pid is not None and getattr(t, "kind", None) == "window"
    }
    after_titles = {
        (t.pid, t.native_window_id): t.title
        for t in after.targets
        if t.pid is not None and getattr(t, "kind", None) == "window"
    }
    # Only compare window pairs that exist in both — windows that opened
    # or closed are handled separately by WINDOW_OPEN / WINDOW_CLOSE.
    common = before_titles.keys() & after_titles.keys()
    return any(before_titles[k] != after_titles[k] for k in common)


# ---------------------------------------------------------------------------
# classify_transition — public entry point
# ---------------------------------------------------------------------------


def classify_transition(
    before: ObservationSnapshot,
    after: ObservationSnapshot,
) -> TransitionResult:
    """Classify what changed between `before` and `after`.

    Priority order (P2.1 review decision; hardened after review):

        1. APPLICATION_RESTART       — signature set emptied
                                      (process_name, fingerprint)
                                      (Blocker 3: robust against
                                      native_window_id reuse across
                                      processes)
        2. MODAL_OPEN / CLOSE        — modal heuristic delta
                                      (preferred over WINDOW_OPEN;
                                      modals ARE windows)
        3. WINDOW_OPEN / CLOSE       — non-modal window-set delta
        4. WINDOW_OWNER_CHANGE       — same signature, different pid
                                      (Blocker 3: pid change alone
                                      is NOT sufficient; need
                                      surviving process_name +
                                      fingerprint to disambiguate
                                      from restart)
        5. PAGE_NAVIGATION           — tree_digest + title delta +
                                      structural evidence on a
                                      surviving target
        6. CONTROL_STATE_CHANGE      — tree_digest delta + structural
                                      evidence (fingerprint/text/title
                                      changed on a surviving target)
                                      (Blocker 1: tree_digest alone
                                      is unreliable)
        7. NONE                      — everything equal, OR digest
                                      drift with no structural signal
        8. UNKNOWN_MATERIAL_CHANGE   — fallback when no rule matches

    Coordinates alone never promote a signal.
    """
    before_pids = _pid_set(before)
    after_pids = _pid_set(after)
    before_windows = _window_set(before)
    after_windows = _window_set(after)
    before_topology = _topology_set(before)
    after_topology = _topology_set(after)

    signals: dict[str, Any] = {
        "before_pids": sorted(before_pids),
        "after_pids": sorted(after_pids),
        "before_windows": sorted(before_windows),
        "after_windows": sorted(after_windows),
        "before_topology": sorted(before_topology),
        "after_topology": sorted(after_topology),
    }

    # ---- Compute non-modal window set for WINDOW_OPEN/CLOSE
    #      checks. Modal windows (control_type in the modal set or
    #      modal-titled) are excluded so a modal opening does not
    #      double-count as WINDOW_OPEN.
    _nonmodal = lambda t: getattr(t, "kind", None) == "window" and not _is_modal(t)
    new_windows = {
        (t.pid, t.native_window_id) for t in after.targets
        if t.pid is not None and _nonmodal(t)
    } - {
        (t.pid, t.native_window_id) for t in before.targets
        if t.pid is not None and _nonmodal(t)
    }
    closed_windows = {
        (t.pid, t.native_window_id) for t in before.targets
        if t.pid is not None and _nonmodal(t)
    } - {
        (t.pid, t.native_window_id) for t in after.targets
        if t.pid is not None and _nonmodal(t)
    }

    # ---- 1. APPLICATION_RESTART --------------------------------
    # Topology-based: every previously seen topology signature is
    # gone AND new signatures are present. Topology excludes
    # content fields (title, text) so a normal page navigation
    # does NOT trigger this rule, while a real application
    # restart (which rebuilds the automation_id tree or assigns a
    # different hwnd) does. Robust against native_window_id
    # reuse across processes (Blocker 3): if the OS recycles
    # hwnd 100 in a new process with a different process_name,
    # the topology differs.
    if (
        before_topology
        and not (before_topology & after_topology)
        and after_topology
    ):
        signals["restart"] = "all_previous_topology_gone"
        return TransitionResult(
            kind=TransitionKind.APPLICATION_RESTART,
            signals=signals,
            confidence="medium",
        )
    # Also detect restart via pid-only path for completeness:
    # all pids gone + new pids appeared (when topologies are
    # empty/unset, e.g. synthetic test data without
    # process_name / control_type).
    elif (
        before_pids
        and not (before_pids & after_pids)
        and after_pids
        and not before_topology
        and not after_topology
    ):
        signals["restart"] = "all_previous_pids_gone_no_topology"
        return TransitionResult(
            kind=TransitionKind.APPLICATION_RESTART,
            signals=signals,
            confidence="low",
        )

    # ---- 2. MODAL_OPEN / MODAL_CLOSE ----------------------------
    # Checked BEFORE WINDOW_OPEN / CLOSE because modal surfaces
    # ARE windows in the V1 model (kind="window", control_type
    # in {Dialog, Pane, ...}); a modal appearing would otherwise
    # trigger WINDOW_OPEN. The modal classification is more
    # specific and useful for downstream checkpoint decisions.
    before_modals = [t for t in before.targets if _is_modal(t)]
    after_modals = [t for t in after.targets if _is_modal(t)]
    if not before_modals and after_modals:
        signals["modals"] = [t.title for t in after_modals]
        return TransitionResult(
            kind=TransitionKind.MODAL_OPEN,
            signals=signals,
            confidence="medium",
        )
    if before_modals and not after_modals:
        signals["modals"] = [t.title for t in before_modals]
        return TransitionResult(
            kind=TransitionKind.MODAL_CLOSE,
            signals=signals,
            confidence="medium",
        )

    # ---- 3. WINDOW_OPEN / WINDOW_CLOSE --------------------------
    if new_windows and not closed_windows:
        signals["new_windows"] = sorted(new_windows)
        return TransitionResult(
            kind=TransitionKind.WINDOW_OPEN,
            signals=signals,
            confidence="high",
        )
    if closed_windows and not new_windows:
        signals["closed_windows"] = sorted(closed_windows)
        return TransitionResult(
            kind=TransitionKind.WINDOW_CLOSE,
            signals=signals,
            confidence="high",
        )
    # Mixed delta is ambiguous — could be swap, owner change, or
    # restart — fall through to other checks before declaring UNKNOWN.
    if new_windows and closed_windows:
        signals["new_windows"] = sorted(new_windows)
        signals["closed_windows"] = sorted(closed_windows)

    # ---- 4. WINDOW_OWNER_CHANGE ---------------------------------
    # Same topology signature in both snapshots but different
    # pid. Topology excludes content fields (so page nav does
    # not look like owner change) and includes process_name +
    # structural fields (so hwnd reuse across different
    # applications does not falsely trigger — Blocker 3).
    before_topo_to_pid = _topology_to_pid(before)
    after_topo_to_pid = _topology_to_pid(after)
    surviving_topology = set(before_topo_to_pid) & set(after_topo_to_pid)
    for sig in sorted(surviving_topology):
        if before_topo_to_pid[sig] != after_topo_to_pid[sig]:
            signals["topology"] = list(sig)
            signals["from_pid"] = before_topo_to_pid[sig]
            signals["to_pid"] = after_topo_to_pid[sig]
            return TransitionResult(
                kind=TransitionKind.WINDOW_OWNER_CHANGE,
                signals=signals,
                confidence="medium",
            )

    # ---- 5 & 6. PAGE_NAVIGATION / CONTROL_STATE_CHANGE ----------
    # Both require:
    #   (a) tree_digest changed, AND
    #   (b) at least one structural property (fingerprint, title,
    #       text) changed on a SURVIVING target.
    # Blockers addressed:
    #   - Blocker 1: digest alone is sensitive to target ordering
    #     and transient fields; we require structural evidence on
    #     a surviving target before promoting the signal.
    #   - If only digest changes (no structural evidence), we
    #     return NONE — the digest is treated as transient drift.
    fingerprint_changed, title_changed, text_changed = _structural_changes(
        before, after
    )
    structural_evidence = (
        fingerprint_changed or title_changed or text_changed
    )
    if before.tree_digest != after.tree_digest and structural_evidence:
        signals["tree_digest_before"] = before.tree_digest
        signals["tree_digest_after"] = after.tree_digest
        signals["fingerprint_changed"] = fingerprint_changed
        signals["title_changed"] = title_changed
        signals["text_changed"] = text_changed
        if title_changed:
            return TransitionResult(
                kind=TransitionKind.PAGE_NAVIGATION,
                signals=signals,
                confidence="high",
            )
        # fingerprint OR text changed but title did not.
        return TransitionResult(
            kind=TransitionKind.CONTROL_STATE_CHANGE,
            signals=signals,
            confidence="medium",
        )

    # ---- 7. NONE ------------------------------------------------
    # Either everything is identical (full signals echoed for
    # callers / debug) or the digest drifted with no structural
    # evidence (Blocker 1 acceptance: do not promote an unstable
    # digest to a transition).
    if (before.tree_digest == after.tree_digest
            and before_windows == after_windows
            and before_pids == after_pids
            and _active_pid(before) == _active_pid(after)
            and _active_window_id(before) == _active_window_id(after)
            and _active_process(before) == _active_process(after)):
        # Echo full signals so callers always see the same shape.
        return TransitionResult(
            kind=TransitionKind.NONE,
            signals={**signals, "reason": "snapshots_identical"},
            confidence="high",
        )
    if before.tree_digest != after.tree_digest and not structural_evidence:
        # Digest drifted but no real change — likely ordering or
        # transient-field noise. Treat as NONE with a flag for
        # callers who want to surface the digest drift separately.
        signals["digest_only"] = True
        return TransitionResult(
            kind=TransitionKind.NONE,
            signals=signals,
            confidence="low",
        )

    # ---- 8. UNKNOWN_MATERIAL_CHANGE -----------------------------
    return TransitionResult(
        kind=TransitionKind.UNKNOWN_MATERIAL_CHANGE,
        signals=signals,
        confidence="low",
    )


__all__ = [
    "TransitionKind",
    "VALID_TRANSITION_KINDS",
    "TransitionResult",
    "classify_transition",
]