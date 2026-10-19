"""One candidate model for planner, live actions, and golden replay."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any, Mapping

from .evidence import align_evidence
from .policy import LocatorPolicy, choose_unique_candidate


class LocatorError(ValueError):
    def __init__(self, code: str, candidate_count: int = 0):
        super().__init__(code)
        self.code = code
        self.candidate_count = candidate_count


@dataclass(frozen=True)
class LocatorRequest:
    action_id: str
    window: Mapping[str, Any]
    control: Mapping[str, Any]
    ocr_text: str = ""


@dataclass(frozen=True)
class LocatorCandidate:
    target_id: str
    observation_id: str
    window_native_id: str
    rect: tuple[int, int, int, int] | None
    source: Mapping[str, Any]
    score: float = 1.0


@dataclass(frozen=True)
class LocatorResolution:
    candidate: LocatorCandidate | None
    method: str
    point: tuple[int, int] | None
    observation_id: str
    evidence_types: tuple[str, ...]


def _get(value: Mapping, *names: str):
    for name in names:
        if name in value:
            return value[name]
    return None


def _confidence(item: Mapping) -> float:
    value = item.get("confidence")
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or not 0 <= value <= 1):
        raise LocatorError("locator_evidence_invalid")
    return float(value)


class HybridLocator:
    def collect_candidates(self, request: LocatorRequest, bundle: Mapping) -> tuple[LocatorCandidate, ...]:
        observation_id = str(bundle.get("observation_id") or bundle.get("snapshot_id") or "")
        window = bundle.get("window_identity") or {}
        native_id = str(window.get("native_id") or "legacy")
        active = bundle.get("active_window") or {}
        process = request.window.get("processName")
        title_regex = request.window.get("titleRegex")
        out = []
        for item in bundle.get("controls") or bundle.get("targets") or ():
            if not isinstance(item, Mapping):
                continue
            actual_process = _get(item, "process_name", "processName") or active.get("process_name")
            if process and str(actual_process or "").lower().removesuffix(".exe") != str(process).lower().removesuffix(".exe"):
                continue
            title = _get(item, "window_title", "windowTitle") or active.get("title") or ""
            if title_regex:
                try:
                    if not re.search(str(title_regex), str(title)):
                        continue
                except re.error as exc:
                    raise LocatorError("replay_selector_invalid") from exc
            comparisons = {
                "automationId": ("automation_id", "automationId"),
                "identifier": ("identifier",),
                "controlType": ("control_type", "controlType", "role"),
                "name": ("text", "name", "title"),
            }
            # A stable ID owns the identity. Recorded labels (notably clocks,
            # log rows and status text) may legitimately change by replay.
            if request.control.get("automationId") or request.control.get("identifier"):
                comparisons.pop("name")
            if any(request.control.get(key) not in (None, "") and
                   _get(item, *keys) != request.control[key]
                   for key, keys in comparisons.items()):
                continue
            expected_ancestry = request.control.get("ancestry") or ()
            actual_ancestry = item.get("ancestry") or ()
            if expected_ancestry:
                if len(actual_ancestry) < len(expected_ancestry) or any(
                    not isinstance(actual_ancestry[index], Mapping) or
                    any(str(actual_ancestry[index].get(key) or
                            actual_ancestry[index].get(re.sub(r"(?<!^)(?=[A-Z])", "_", key).lower()) or "") != str(value)
                        for key, value in expected.items() if value not in (None, ""))
                    for index, expected in enumerate(expected_ancestry)
                    if isinstance(expected, Mapping)
                ):
                    continue
            target_id = _get(item, "target_id", "targetId")
            if not isinstance(target_id, str) or not target_id:
                continue
            rect = _get(item, "rect", "rectangle")
            if isinstance(rect, (list, tuple)) and len(rect) == 4:
                rect = tuple(map(int, rect))
            else:
                rect = None
            out.append(LocatorCandidate(target_id, observation_id, native_id, rect, item))
        return tuple(out)

    def resolve(self, request: LocatorRequest, bundle: Mapping,
                policy: LocatorPolicy, *, visual_candidates: tuple[Mapping, ...] = (),
                ocr_boxes: tuple[Mapping, ...] = ()) -> LocatorResolution:
        observation_id = str(bundle.get("observation_id") or bundle.get("snapshot_id") or "")
        if not observation_id:
            raise LocatorError("replay_observation_invalid")
        if any(not isinstance(item, Mapping) for item in (*visual_candidates, *ocr_boxes)):
            raise LocatorError("locator_evidence_invalid")
        for item in (*visual_candidates, *ocr_boxes):
            _confidence(item)
        if visual_candidates and (
            bundle.get("screenshot_scope") != "window" or
            bundle.get("screenshot_redacted") is not True
        ):
            raise LocatorError("visual_window_unverified")
        if ocr_boxes and (
            bundle.get("screenshot_scope") != "window" or
            bundle.get("screenshot_redacted") is not True
        ):
            raise LocatorError("ocr_window_unverified")
        candidates = self.collect_candidates(request, bundle)
        native_id = str((bundle.get("window_identity") or {}).get("native_id") or "legacy")
        window_rect = ((bundle.get("window_identity") or {}).get("rect")
                       or (bundle.get("active_window") or {}).get("rect"))
        if (visual_candidates or ocr_boxes) and (
            not isinstance(window_rect, (list, tuple)) or len(window_rect) != 4
        ):
            raise LocatorError("visual_window_unverified" if visual_candidates
                               else "ocr_window_unverified")
        if window_rect is not None:
            window_rect = tuple(map(int, window_rect))
        if (visual_candidates or ocr_boxes) and (
            window_rect[2] <= window_rect[0] or window_rect[3] <= window_rect[1]
        ):
            raise LocatorError("visual_window_unverified" if visual_candidates
                               else "ocr_window_unverified")
        for item in visual_candidates:
            rect = item.get("rect")
            if not isinstance(rect, (list, tuple)) or len(rect) != 4:
                raise LocatorError("locator_evidence_invalid")
            x0, y0, x1, y1 = map(int, rect)
            if not (window_rect[0] <= x0 < x1 <= window_rect[2]
                    and window_rect[1] <= y0 < y1 <= window_rect[3]):
                raise LocatorError("visual_match_outside_window")
        for item in ocr_boxes:
            rect = item.get("rect")
            if not isinstance(rect, (list, tuple)) or len(rect) != 4:
                raise LocatorError("locator_evidence_invalid")
            x0, y0, x1, y1 = map(int, rect)
            if not (window_rect[0] <= x0 < x1 <= window_rect[2]
                    and window_rect[1] <= y0 < y1 <= window_rect[3]):
                raise LocatorError("ocr_box_outside_window")
        aligned = align_evidence(candidates, visual_candidates, ocr_boxes,
                                 observation_id=observation_id, window_native_id=native_id,
                                 min_overlap=policy.min_overlap)
        valid_visual = tuple(item for item in visual_candidates
                             if item.get("observation_id") == observation_id
                             and item.get("window_native_id") == native_id
                             and _confidence(item) >= policy.min_visual_confidence)
        if policy.mode == "visual_only":
            candidates = ()
        if len(candidates) == 1:
            chosen = candidates[0]
            if valid_visual and not any(
                kind == "visual" and _confidence(evidence) >= policy.min_visual_confidence
                for kind, evidence in aligned[chosen.target_id]
            ):
                raise LocatorError("locator_evidence_conflict", 1)
            return LocatorResolution(chosen, "semantic", None, observation_id,
                                     ("semantic",) + (("visual",) if valid_visual else ()))
        if len(candidates) > 1:
            scored = []
            for item in candidates:
                # Repeated detections of one template/text are not independent
                # evidence. At most one strongest hit per modality contributes.
                visual_score = 0.0
                ocr_score = 0.0
                for kind, evidence in aligned[item.target_id]:
                    confidence = _confidence(evidence)
                    if kind == "visual" and confidence >= policy.min_visual_confidence:
                        visual_score = max(visual_score, confidence)
                    if kind == "ocr" and confidence >= policy.min_ocr_confidence and (
                        not request.ocr_text or evidence.get("text") == request.ocr_text
                    ):
                        ocr_score = max(ocr_score, confidence)
                scored.append(LocatorCandidate(item.target_id, item.observation_id,
                                               item.window_native_id, item.rect, item.source,
                                               visual_score + ocr_score))
            if not max(item.score for item in scored):
                raise LocatorError("locator_ambiguous", len(candidates))
            try:
                chosen = choose_unique_candidate(tuple(scored), min_margin=policy.min_margin)
            except ValueError as exc:
                raise LocatorError("locator_ambiguous", len(candidates)) from exc
            return LocatorResolution(chosen, "evidence_fused", None, observation_id,
                                     tuple(sorted({kind for kind, _ in aligned[chosen.target_id]})))
        if policy.mode == "semantic_only" or request.action_id not in {"gui.click", "gui.click_target"}:
            raise LocatorError("replay_target_not_found", 0)
        if not valid_visual:
            raise LocatorError("locator_low_confidence", 0)
        ranked = sorted(valid_visual, key=_confidence, reverse=True)
        if len(ranked) > 1 and _confidence(ranked[0]) - _confidence(ranked[1]) < policy.min_margin:
            raise LocatorError("locator_ambiguous", len(ranked))
        rect = ranked[0].get("rect")
        if not isinstance(rect, (list, tuple)) or len(rect) != 4:
            raise LocatorError("locator_evidence_invalid")
        point = ((int(rect[0]) + int(rect[2])) // 2, (int(rect[1]) + int(rect[3])) // 2)
        return LocatorResolution(None, "visual", point, observation_id, ("visual",))
