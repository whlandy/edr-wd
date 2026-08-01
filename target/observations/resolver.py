"""
resolver.py — 6-step target resolution pipeline (architecture §8.2).

Public API:

    resolve_target(ref, snapshot, catalog) -> Target

    raises TargetResolutionError with a stable `code` from enums:
        * CODE_TARGET_STALE         — snapshot_id not live, or
                                       tree_digest changed.
        * CODE_TARGET_NOT_FOUND     — no candidate matched.
        * CODE_TARGET_AMBIGUOUS      — multiple candidates matched.
        * CODE_OWNERSHIP_MISMATCH    — process_name mismatch.
        * CODE_FALLBACK_NOT_ALLOWED  — rectangle proximity was the
                                       only match but the action
                                       does not permit pointer
                                       fallback.

The resolver walks the steps in order:

    1. Verify snapshot is live (FR-P0.3-03, FR-P0.3-08).
    2. Ownership check (FR-P0.3-05).
    3. Exact native identity.
    4. Exact stable fingerprint (within the matching process/window).
    5. Backend-native selector from `ref.selector_hint`.
    6. Role/type + normalized text.
    7. (Step 6 in architecture: rectangle proximity, ONLY for
       actions whose catalog permits pointer fallback.)

If a step yields no candidate, the resolver advances to the next.
The first step that yields a single candidate wins. If a step yields
more than one candidate (after ownership filtering), the resolver
raises `target_ambiguous` (architecture §8.2 step 1 — refuse
silently, never pick one).

P0.3 implements the algorithm against in-memory snapshot data; it
never invokes a live backend method. The "native selector" step in
real deployment is the backend's `find_control` — but for P0.3 we
simulate it by matching against `selector_hint` keys (`text`,
`automation_id`, `control_type`, etc.) since we have no live backend.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from action_catalog import ActionSpec
from protocol_models.models import ProtocolModelError

from .enums import (
    ARCHITECTURE_P0_3_OBSERVATION_CODES,
    CODE_FALLBACK_NOT_ALLOWED,
    CODE_OWNERSHIP_MISMATCH,
    CODE_TARGET_AMBIGUOUS,
    CODE_TARGET_NOT_FOUND,
    CODE_TARGET_STALE,
)
from .ids import parse_target_id
from .invalidate import is_live
from .models import ObservationSnapshot, Target
from .ref import ObservationRef


@dataclass(frozen=True)
class TargetResolutionError(Exception):
    """Typed error raised by `resolve_target`.

    `code` is one of `ARCHITECTURE_P0_3_OBSERVATION_CODES`. `details`
    carries diagnostic data the resolver chose to include (e.g.
    candidate_count for target_ambiguous).
    """

    code: str
    message: str
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.code not in ARCHITECTURE_P0_3_OBSERVATION_CODES:
            raise ProtocolModelError(
                "type_error",
                f"unknown resolver code {self.code!r}",
                path="code",
                value=self.code,
            )


def _matches_process(
    target: Target, expected_process_name: str
) -> bool:
    return target.process_name == expected_process_name


def _matches_fingerprint(target: Target, ref_fingerprint: str) -> bool:
    return bool(target.fingerprint) and target.fingerprint == ref_fingerprint


def _matches_native_identity(target: Target, ref_target_id: str) -> bool:
    """P0.3 native-identity match is by target_id (within one
    snapshot, T0001 is stable). For cross-snapshot identity, the
    caller should use `ref.fingerprint` (step 3)."""
    return target.target_id == ref_target_id


def _matches_selector(target: Target, selector_hint: Mapping[str, Any]) -> bool:
    """Architecture §8.2 step 4: backend-native selector match.

    Step 4 is restricted to native-identity fields (`text`,
    `automation_id`, `control_type`, `title`). The extended keys
    `rect` and `text_contains` belong to steps 5/6 (text / rect
    proximity). Mixing them here would cause step 4 to swallow
    rect-only refs and never reach step 6.
    """
    if not selector_hint:
        return False
    step4_keys = {"text", "automation_id", "control_type", "title"}
    for key, expected in selector_hint.items():
        if key not in step4_keys:
            continue
        if key == "text" and target.text != expected:
            return False
        if key == "automation_id" and target.automation_id != expected:
            return False
        if key == "control_type" and target.control_type != expected:
            return False
        if key == "title" and target.title != expected:
            return False
    # Only consider step 4 successful if at least one step-4 key
    # was supplied AND matched. Otherwise the selector is empty for
    # step 4 and we fall through.
    return any(k in step4_keys for k in selector_hint)


def _matches_role_type_text(target: Target, selector_hint: Mapping[str, Any]) -> bool:
    """Architecture §8.2 step 5: role/type + normalized text. P0.3
    approximates with a substring match on `text` when the selector
    hint asks for it."""
    needle = selector_hint.get("text_contains")
    if not needle:
        return False
    if not target.text:
        return False
    return needle in target.text


def _query_center_inside_target(
    query: tuple[int, int, int, int],
    target: tuple[int, int, int, int],
) -> bool:
    """Architecture §8.2 step 6: rect proximity via *containment*.

    The semantic we want is "the user is clicking inside this
    control". Concretely: the query rect's center point must fall
    inside the target rect's bounding box. We do NOT use distance
    between centers (P0.3 review: a control may be much larger
    than the click area; distance comparisons can either be too
    lax or too strict depending on the control size).

    Args:
        query: (x0, y0, x1, y1) where the user is pointing.
        target: (x0, y0, x1, y1) of the candidate control.

    Returns:
        True iff query-center is inside target. Edge-touching
        counts (>=, <=) so a control with rect (100, 100, 200,
        150) matches query (100, 100, 200, 150) — i.e. exact
        equality.
    """
    if query is None or target is None:
        return False
    qx0, qy0, qx1, qy1 = query
    tx0, ty0, tx1, ty1 = target
    cx = (qx0 + qx1) // 2
    cy = (qy0 + qy1) // 2
    return tx0 <= cx <= tx1 and ty0 <= cy <= ty1


def _filter_by_ownership(
    snapshot: ObservationSnapshot,
    expected_process_name: str,
    targets: list[Target],
) -> list[Target]:
    if not expected_process_name:
        return targets
    return [t for t in targets if _matches_process(t, expected_process_name)]


def resolve_target(
    ref: ObservationRef,
    snapshot: ObservationSnapshot,
    catalog_action: ActionSpec,
) -> Target:
    """Resolve `ref` against `snapshot` for `catalog_action`.

    Raises `TargetResolutionError` on any of the documented failure
    modes (architecture §8.2). Returns the matched `Target` on
    success.
    """
    # Step 1: snapshot liveness (architecture §8.4 / FR-P0.3-08).
    if not is_live(snapshot.snapshot_id):
        raise TargetResolutionError(
            code=CODE_TARGET_STALE,
            message=(
                f"snapshot_id {snapshot.snapshot_id!r} is not live "
                f"(invalidate_snapshot was called or it was never "
                f"registered)"
            ),
            details={"snapshot_id": snapshot.snapshot_id},
        )

    # Pre-filter by ownership (architecture §8.2 step 1).
    ownership_filtered = _filter_by_ownership(
        snapshot,
        ref.expected_process_name,
        list(snapshot.targets),
    )
    if ref.expected_process_name and not ownership_filtered:
        actual = next(
            (t.process_name for t in snapshot.targets), "<no targets>"
        )
        raise TargetResolutionError(
            code=CODE_OWNERSHIP_MISMATCH,
            message=(
                f"expected process_name {ref.expected_process_name!r} "
                f"not present in snapshot; first observed process "
                f"is {actual!r}"
            ),
            details={
                "expected_process_name": ref.expected_process_name,
                "actual_process_name": actual,
            },
        )

    candidates = ownership_filtered

    # Helper: raise ambiguous if multiple match.
    def _ambiguous(matched: list[Target], strategy: str) -> None:
        if len(matched) > 1:
            raise TargetResolutionError(
                code=CODE_TARGET_AMBIGUOUS,
                message=(
                    f"strategy {strategy!r} matched {len(matched)} "
                    f"controls; refusing to pick one"
                ),
                details={
                    "candidate_count": len(matched),
                    "strategy": strategy,
                    "candidate_target_ids": [t.target_id for t in matched],
                },
            )

    # Step 2: exact native identity (target_id).
    if ref.target_id:
        try:
            parse_target_id(ref.target_id)
        except ValueError as exc:
            raise TargetResolutionError(
                code=CODE_TARGET_STALE,
                message=f"malformed ref.target_id: {exc}",
                details={"target_id": ref.target_id},
            )
        matched = [t for t in candidates
                   if _matches_native_identity(t, ref.target_id)]
        if len(matched) == 1:
            return matched[0]
        if len(matched) > 1:
            _ambiguous(matched, "native_identity")
        # Fall through to fingerprint.

    # Step 3: exact fingerprint (within matching process/window).
    if ref.fingerprint:
        matched = [t for t in candidates
                   if _matches_fingerprint(t, ref.fingerprint)]
        if len(matched) == 1:
            return matched[0]
        if len(matched) > 1:
            _ambiguous(matched, "fingerprint")

    # Step 4: backend-native selector.
    sel = ref.selector_hint or {}
    if sel:
        matched = [t for t in candidates if _matches_selector(t, sel)]
        if len(matched) == 1:
            return matched[0]
        if len(matched) > 1:
            _ambiguous(matched, "selector")

    # Step 5: role/type + normalized text.
    if sel.get("text_contains"):
        matched = [t for t in candidates if _matches_role_type_text(t, sel)]
        if len(matched) == 1:
            return matched[0]
        if len(matched) > 1:
            _ambiguous(matched, "text")

    # Step 6: rectangle proximity. Only for actions whose catalog
    # entry permits pointer fallback (architecture §8.2 step 6).
    if sel.get("rect"):
        # Permission gate: pointer-input actions only.
        if catalog_action.category not in {"pointer_input"}:
            raise TargetResolutionError(
                code=CODE_FALLBACK_NOT_ALLOWED,
                message=(
                    f"action {catalog_action.action_id!r} does not "
                    f"permit pointer fallback (category="
                    f"{catalog_action.category!r}); rect-proximity "
                    f"match refused"
                ),
                details={
                    "action_id": catalog_action.action_id,
                    "category": catalog_action.category,
                },
            )
        # Find every candidate whose rect contains the query
        # center, then pick the *smallest* (deepest) one.
        # This handles the case where a query point falls
        # inside both a parent window and a child control:
        # the child wins.
        matched_with_area: list[tuple[Target, int]] = []
        for t in candidates:
            if t.rect is None:
                continue
            if _query_center_inside_target(sel["rect"], t.rect):
                area = (t.rect[2] - t.rect[0]) * (
                    t.rect[3] - t.rect[1]
                )
                matched_with_area.append((t, area))
        if matched_with_area:
            # Smallest area first; tie-break by target_id for
            # determinism across runs.
            matched_with_area.sort(
                key=lambda pair: (pair[1], pair[0].target_id)
            )
            return matched_with_area[0][0]

    # Nothing matched.
    raise TargetResolutionError(
        code=CODE_TARGET_NOT_FOUND,
        message=(
            f"no target matched ref={ref!r} in snapshot "
            f"{snapshot.snapshot_id!r}"
        ),
        details={
            "snapshot_id": snapshot.snapshot_id,
            "target_id": ref.target_id,
            "fingerprint": ref.fingerprint,
            "selector_hint_keys": sorted(sel.keys()) if sel else [],
            "candidate_count": len(candidates),
        },
    )


__all__ = [
    "TargetResolutionError",
    "resolve_target",
]