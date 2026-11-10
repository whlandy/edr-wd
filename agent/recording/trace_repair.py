"""Evidence-constrained trace-repair candidates (design 12 §4).

The repair layer sits between a failed compilation and the rerecord layer:
from the raw original and its compile issues it derives candidates whose
every change is backed by facts the original already contains. It never
touches the original, never edits args/verifiers/actionIds, never invents
observations, and never fabricates frames — a candidate is a derived
document with a parent hash, a named rule, and a minimal diff.

Only compile issues from the trace-defect class (§2) are repairable here:
weak-identity steps that failed only because their window instance died
before the inventory could walk it, while provably sequential sibling
instances of the same window recorded the same menu layout completely.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from typing import Any, Mapping

from agent.locator.menu_layout import layout as _menu_layout, matches_ancestry
from agent.recording.models import GoldenTrace, ReplaySelector, canonical_json
from target.recording.models import RawCaptureEvent, RawRecording

#: The only repairable trace-defect codes (design 12 §2 row 1). Coverage
#: counts, recorder-panel counts, product failures and environment blocks
#: are outside repair scope by design and are refused explicitly.
REPAIRABLE_CODES = frozenset({
    "compile_weak_identity_unresolved",
    "compile_weak_identity_volatile_label",
})

#: Upper bound on candidates per recovery job (design 12 §3 budget).
CANDIDATE_LIMIT = 3

_STRUCTURAL_RULE_ID = "structural_identity_from_sequential_sibling_frames"


class RepairError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class RepairCandidate:
    """A derived trace candidate plus the provenance that justifies it."""
    rule_id: str
    golden: GoldenTrace
    parent_sha256: str
    source_evidence: Mapping[str, Any]
    diff_summary: Mapping[str, Any]
    semantics_changed: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "ruleId": self.rule_id,
            "parentSha256": self.parent_sha256,
            "sourceEvidence": dict(self.source_evidence),
            "diffSummary": dict(self.diff_summary),
            "semanticsChanged": self.semantics_changed,
            "golden": self.golden.to_dict(),
        }


@dataclass(frozen=True)
class RepairRefusal:
    reason: str
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {"reason": self.reason, "detail": self.detail}


@dataclass(frozen=True)
class RepairProposal:
    candidates: tuple[RepairCandidate, ...]
    refusals: tuple[RepairRefusal, ...]


def _recording_sha256(recording: RawRecording) -> str:
    return "sha256:" + hashlib.sha256(
        canonical_json(recording.to_dict()).encode()).hexdigest()


def _lifecycle_bounds(recording: RawRecording) -> dict[str, tuple[int | None, int | None]]:
    """instanceId -> (first fact ms, first end-of-proven-life ms).

    Mirrors the compiler's interval semantics: `start` is the first observed
    fact (a lower bound, not a birth); the interval ends at the FIRST
    destroyed/lost fact — physical death or unknown continuity, neither
    provable beyond. An instance with no end fact stays open.
    """
    bounds: dict[str, tuple[int | None, int | None]] = {}
    for fact in (recording.window_timeline or {}).get("events", ()):
        ri = fact.get("instanceId")
        ms = fact.get("monotonicMs")
        if not isinstance(ri, str) or not ri or not isinstance(ms, int):
            continue
        start, end = bounds.get(ri, (ms, None))
        start = min(start, ms)
        if fact.get("kind") in {"destroyed", "lost"} and (end is None or ms < end):
            end = ms
        bounds[ri] = (start, end)
    return bounds


def _sequentially_reopened(
    earlier_ri: str,
    later_ri: str,
    bounds: Mapping[str, tuple[int | None, int | None]],
) -> bool:
    """True when the timeline proves the later instance came into existence
    at or after the earlier one's proven interval ended — they never coexisted."""
    earlier = bounds.get(earlier_ri)
    later = bounds.get(later_ri)
    if not earlier or not later:
        return False
    earlier_end, later_start = earlier[1], later[0]
    return (earlier_end is not None and later_start is not None
            and later_start >= earlier_end)


def _event_instance(event: RawCaptureEvent) -> str | None:
    recorded = event.evidence.get("window") if isinstance(event.evidence, Mapping) else None
    instance_id = recorded.get("instanceId") if isinstance(recorded, Mapping) else None
    return instance_id if isinstance(instance_id, str) and instance_id else None


def _instance_handle(recording: RawRecording, instance_id: str) -> int | None:
    for window in recording.windows:
        if window.get("instanceId") == instance_id:
            handle = window.get("handle")
            if isinstance(handle, int) and not isinstance(handle, bool):
                return handle
    return None


def _frames_of_instance(
    recording: RawRecording,
    instance_id: str,
    handle: int | None,
) -> list[Mapping[str, Any]]:
    """The event-bound frames of one window instance (complete or not)."""
    frames: list[Mapping[str, Any]] = []
    for snapshot in recording.control_snapshots:
        window = snapshot.get("window")
        if not isinstance(window, Mapping):
            continue
        if window.get("instanceId"):
            if window.get("instanceId") != instance_id:
                continue
        elif handle is not None and window.get("handle") != handle:
            continue
        frames.append(snapshot)
    return frames


def _derive_structural_contract(
    step_event: RawCaptureEvent,
    proof_frames: list[Mapping[str, Any]],
) -> dict[str, Any] | None:
    """Derive ONE menu-layout contract that every proof frame confirms.

    The failing step's own frame is gone (its instance died before the
    inventory walked it), so derivation cannot use the compiler's
    exact-rect `from_recording` path. Instead, per proof frame: collect the
    ancestry-matched MenuItem peers, build the same scale/translation-
    invariant layout `menu_layout.layout` builds, and locate the slot that
    contains the failing target's rect CENTER (inclusive-exclusive bounds;
    slots never overlap by construction). Every frame must yield the same
    slot index AND the same normalized layout; a missing slot, an ambiguous
    point, or a disagreeing frame refuses the candidate.

    This mirrors what the replay locator does at run time
    (`resolve_index` over current peers), applied to recorded proof frames:
    the label may have changed between instances (Language/语言设置/Idioma),
    the position in the proven same-shaped menu is the fact.
    """
    target = step_event.observed_target
    if target is None or not target.rect:
        return None
    ancestry = [dict(a) for a in target.ancestry]
    if not ancestry or target.control_type != "MenuItem":
        return None
    rect = [int(v) for v in target.rect]
    if len(rect) != 4:
        return None
    point = ((rect[0] + rect[2]) / 2.0, (rect[1] + rect[3]) / 2.0)
    derived: dict[str, Any] | None = None
    for frame in proof_frames:
        if frame.get("complete") is not True:
            return None
        controls = frame.get("controls") or []
        peers = [n for n in controls if isinstance(n, Mapping)
                 and n.get("controlType") == "MenuItem"
                 and matches_ancestry(n.get("ancestry"), ancestry)]
        rects = [n.get("rect") for n in peers]
        result = _menu_layout(rects)
        if result is None:
            return None
        order, normalized = result
        slot = None
        for position, peer_index in enumerate(order):
            slot_rect = rects[peer_index]
            if not isinstance(slot_rect, (list, tuple)) or len(slot_rect) != 4:
                return None
            x0, y0, x1, y1 = (int(v) for v in slot_rect)
            if x0 <= point[0] < x1 and y0 <= point[1] < y1:
                if slot is not None:
                    return None
                slot = position
        if slot is None:
            return None
        contract = {"kind": "menu-layout/v1", "index": slot,
                    "layout": normalized}
        if derived is None:
            derived = contract
        elif contract != derived:
            return None
    return derived


def _structural_candidate(
    recording: RawRecording,
    golden: GoldenTrace,
    step_id: str,
    step_event: RawCaptureEvent,
) -> tuple[RepairCandidate | None, str | None]:
    """One candidate: attach the sibling-frames' structural identity to the
    failing step's selector. Returns (candidate, refusal_detail)."""
    instance_id = _event_instance(step_event)
    if not instance_id:
        return None, "step event carries no window instanceId"
    bounds = _lifecycle_bounds(recording)
    if instance_id not in bounds:
        return None, f"instance {instance_id} has no timeline facts"
    # Own-frame authority: the failing instance's own complete frames are
    # the authority for its identity. If any exists, the step's failure is
    # NOT "the tree is missing" and must not be papered over by borrowing
    # sibling frames — the recorded disagreement stands. Backfill applies
    # only when the own instance provably died before any walk.
    own_complete = [
        f for f in _frames_of_instance(recording, instance_id, None)
        if f.get("complete") is True]
    if own_complete:
        return None, (
            f"instance {instance_id} has its own complete frames; the "
            f"weak-identity failure of {step_id} stands on recorded "
            "evidence and must not be overridden by sibling frames")
    proof_frames: list[Mapping[str, Any]] = []
    proof_instances: list[str] = []
    for other_ri in bounds:
        if other_ri == instance_id:
            continue
        if not _sequentially_reopened(other_ri, instance_id, bounds):
            continue
        frames = [f for f in _frames_of_instance(recording, other_ri, None)
                  if f.get("complete") is True]
        if not frames:
            continue
        # EVERY complete frame of a provably sequential sibling must
        # confirm the contract. Skipping a frame that cannot derive it
        # (different menu shape, moved slot) would let one convenient
        # frame overrule recorded disagreement — exactly the evidence
        # weakening §4 forbids.
        for frame in frames:
            proof_frames.append(frame)
            proof_instances.append(other_ri)
    if not proof_frames:
        return None, (
            f"no complete frame of a provably sequential sibling instance "
            f"exists for {step_id}")
    contract = _derive_structural_contract(step_event, proof_frames)
    if contract is None:
        return None, "sibling frames disagree on the menu contract"
    steps = list(golden.steps)
    target_step = next((s for s in steps if s.step_id == step_id), None)
    if target_step is None or target_step.selector is None:
        return None, f"step {step_id} has no selector to repair"
    control = dict(target_step.selector.control)
    if "structural" in control:
        return None, f"step {step_id} already carries a structural contract"
    # The ONLY permitted mutation: selector.control gains `structural`.
    # Everything else — args, verifiers, actionId, window identity — must be
    # byte-identical between parent and candidate.
    control["structural"] = contract
    repaired = replace(
        target_step,
        selector=ReplaySelector(
            dict(target_step.selector.window), control,
            (dict(target_step.selector.visual)
             if target_step.selector.visual is not None else None)),
        status="ready",
        issues=tuple(i for i in target_step.issues
                     if i not in {"compile_weak_identity_unresolved",
                                  "compile_weak_identity_volatile_label"}),
    )
    steps = [repaired if s.step_id == step_id else s for s in steps]
    remaining_incomplete = any(s.status != "ready" for s in steps)
    candidate_golden = GoldenTrace(
        name=golden.name,
        source_recording=dict(golden.source_recording),
        catalog=dict(golden.catalog),
        environment=dict(golden.environment),
        steps=tuple(steps),
        status="incomplete" if remaining_incomplete else "ready",
        schema=golden.schema,
        entry_window=golden.entry_window,
        window_sequence=golden.window_sequence,
    )
    diff_summary = {
        "stepsChanged": [step_id],
        "fieldsChanged": ["selector.control.structural", "step.status"],
        "structural": contract,
    }
    source_evidence = {
        "failingStepId": step_id,
        "failingInstanceId": instance_id,
        "proofInstanceIds": proof_instances,
        "proofSnapshotIds": [f.get("snapshotId") for f in proof_frames],
        "timelineProvenSequential": [
            {"earlier": ri, "later": instance_id}
            for ri in proof_instances
        ],
    }
    return RepairCandidate(
        rule_id=_STRUCTURAL_RULE_ID,
        golden=candidate_golden,
        parent_sha256=_recording_sha256(recording),
        source_evidence=source_evidence,
        diff_summary=diff_summary,
        semantics_changed=False,
    ), None


def propose_repairs(
    recording: RawRecording,
    golden: GoldenTrace,
    diagnostics: Mapping[str, Any],
) -> RepairProposal:
    """Derive repair candidates for the trace-defect failures of one compile.

    `diagnostics` is the compile report's diagnostics (sourceEvents.byStep
    maps stepId -> raw sequences). Steps are located through that explicit
    mapping — never guessed from list positions. Returns every derivable
    candidate (bounded by CANDIDATE_LIMIT) plus explicit refusals for
    repair targets without provable support. The recording is never mutated.
    """
    by_step = (diagnostics.get("sourceEvents") or {}).get("byStep") or {}
    events_by_sequence = {e.sequence: e for e in recording.events}
    candidates: list[RepairCandidate] = []
    refusals: list[RepairRefusal] = []
    for step in golden.steps:
        if step.status == "ready" or not (set(step.issues) & REPAIRABLE_CODES):
            continue
        if len(candidates) >= CANDIDATE_LIMIT:
            refusals.append(RepairRefusal(
                "candidate_budget_exhausted",
                f"step {step.step_id} repairable but the candidate budget "
                f"({CANDIDATE_LIMIT}) is spent",
            ))
            continue
        sequences = by_step.get(step.step_id) or []
        step_event = next(
            (events_by_sequence[s] for s in sequences
             if (e := events_by_sequence.get(s)) is not None
             and e.type == "pointer_click" and e.observed_target is not None),
            None)
        if step_event is None:
            refusals.append(RepairRefusal(
                "step_source_event_unlocatable",
                f"step {step.step_id} maps to {sequences or 'no'} raw "
                "sequences; none is a pointer_click with an observed target",
            ))
            continue
        candidate, refusal = _structural_candidate(
            recording, golden, step.step_id, step_event)
        if candidate is None:
            refusals.append(RepairRefusal(
                "structural_identity_unprovable", refusal or ""))
            continue
        candidates.append(candidate)
    if not candidates and not refusals:
        refusals.append(RepairRefusal(
            "no_repairable_trace_defects",
            "no incomplete step carries a repairable weak-identity code",
        ))
    return RepairProposal(tuple(candidates[:CANDIDATE_LIMIT]), tuple(refusals))


__all__ = [
    "CANDIDATE_LIMIT",
    "REPAIRABLE_CODES",
    "RepairCandidate",
    "RepairError",
    "RepairProposal",
    "RepairRefusal",
    "propose_repairs",
]
