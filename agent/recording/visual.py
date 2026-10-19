"""Safety gates for protected visual fallback during golden replay."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from agent.execution import StepMaterializationError

from .models import ReplaySelector


@dataclass(frozen=True)
class VisualCandidate:
    rect: tuple[int, int, int, int]
    confidence: float
    scale: float = 1.0


@dataclass(frozen=True)
class VisualResolution:
    action_id: str
    args: Mapping[str, Any]
    confidence: float
    margin: float


class SafeVisualResolver:
    """Turn matcher candidates into one guarded window-relative click."""

    def __init__(
        self,
        matcher: Callable[[str, Any], Sequence[VisualCandidate]],
        *,
        template_verifier: Callable[[str, Mapping[str, Any]], bool] | None = None,
        min_confidence: float = 0.92,
        min_margin: float = 0.05,
        min_context_margin: float = 0.03,
        min_scale: float = 0.75,
        max_scale: float = 1.5,
    ) -> None:
        self._matcher = matcher
        self._template_verifier = template_verifier
        self.min_confidence = min_confidence
        self.min_margin = min_margin
        self.min_context_margin = min_context_margin
        self.min_scale = min_scale
        self.max_scale = max_scale

    @staticmethod
    def _window_rect(observation: Any) -> tuple[int, int, int, int] | None:
        active = observation.get("active_window") if isinstance(observation, Mapping) else getattr(observation, "active_window", None)
        if isinstance(active, Mapping):
            rect = active.get("rect") or active.get("rectangle")
            if isinstance(rect, (list, tuple)) and len(rect) == 4:
                return tuple(int(value) for value in rect)
            if isinstance(rect, Mapping) and all(key in rect for key in ("x", "y", "w", "h")):
                x, y = int(rect["x"]), int(rect["y"])
                return x, y, x + int(rect["w"]), y + int(rect["h"])
        return None

    @staticmethod
    def _valid_confidence(candidate: VisualCandidate) -> bool:
        value = candidate.confidence
        return (isinstance(value, (int, float)) and not isinstance(value, bool)
                and math.isfinite(value) and 0 <= value <= 1)

    def _matched_candidates(
        self, visual: Mapping[str, Any], observation: Any,
        window_rect: tuple[int, int, int, int],
    ) -> list[VisualCandidate]:
        template = str(visual["template"])
        if self._template_verifier is not None and not self._template_verifier(template, visual):
            raise StepMaterializationError(
                "visual_template_integrity_failed", "visual template path or digest failed integrity validation"
            )
        context_path = visual.get("contextTemplate")
        offset = visual.get("contextOffset")
        if context_path:
            context_visual = {**visual, "elementSha256": visual.get("contextSha256")}
            if self._template_verifier is not None and not self._template_verifier(context_path, context_visual):
                raise StepMaterializationError(
                    "visual_template_integrity_failed", "context template path or digest failed integrity validation"
                )
        contexts: list[VisualCandidate] = []
        if context_path and isinstance(offset, (list, tuple)) and len(offset) == 2:
            wx0, wy0, wx1, wy1 = window_rect
            contexts = [candidate for candidate in self._matcher(context_path, observation)
                        if self._valid_confidence(candidate)
                        and candidate.confidence >= self.min_confidence
                        and self.min_scale <= candidate.scale <= self.max_scale
                        and wx0 <= candidate.rect[0] < candidate.rect[2] <= wx1
                        and wy0 <= candidate.rect[1] < candidate.rect[3] <= wy1]
            contexts.sort(key=lambda candidate: candidate.confidence, reverse=True)
            # A broad context patch can match many repeated rows with high
            # absolute confidence. It is useful for disambiguation only when
            # its best position is distinguishable from the runner-up.
            if len(contexts) > 1 and (contexts[0].confidence - contexts[1].confidence
                                       < self.min_context_margin):
                contexts = []
            else:
                contexts = contexts[:1]
        match_observation = observation
        if contexts and isinstance(observation, Mapping):
            match_observation = {**observation, "search_rect": contexts[0].rect}
        elements = [candidate for candidate in self._matcher(template, match_observation)
                    if self._valid_confidence(candidate)]
        if contexts:
            supported = []
            for element in elements:
                for context in contexts:
                    if abs(element.scale - context.scale) > 0.1:
                        continue
                    expected_x = context.rect[0] + round(int(offset[0]) * context.scale)
                    expected_y = context.rect[1] + round(int(offset[1]) * context.scale)
                    tolerance = max(3, round(min(element.rect[2] - element.rect[0],
                                                 element.rect[3] - element.rect[1]) * 0.05))
                    if (abs(element.rect[0] - expected_x) <= tolerance
                            and abs(element.rect[1] - expected_y) <= tolerance):
                        supported.append(element)
                        break
            # Context is supporting evidence. If the surrounding UI changed,
            # the element-only path remains available but must still pass the
            # ordinary confidence/margin gates; repeated icons stay ambiguous.
            if supported:
                elements = supported
            elif match_observation is not observation:
                elements = [candidate for candidate in self._matcher(template, observation)
                            if self._valid_confidence(candidate)]
        return sorted(elements, key=lambda candidate: candidate.confidence, reverse=True)

    def resolve(
        self,
        action_id: str,
        selector: ReplaySelector,
        observation: Any,
    ) -> VisualResolution:
        visual = selector.visual or {}
        screenshot_scope = (
            observation.get("screenshot_scope")
            if isinstance(observation, Mapping)
            else getattr(observation, "screenshot_scope", None)
        )
        screenshot_redacted = (
            observation.get("screenshot_redacted")
            if isinstance(observation, Mapping)
            else getattr(observation, "screenshot_redacted", None)
        )
        if screenshot_scope != "window" or screenshot_redacted is not True:
            raise StepMaterializationError(
                "visual_window_unverified",
                "visual replay requires a redacted screenshot captured from the locked window",
            )
        if action_id not in {"gui.click", "gui.click_target"}:
            raise StepMaterializationError(
                "visual_fallback_not_allowed",
                f"visual fallback is not allowed for action {action_id!r}",
            )
        if visual.get("redacted") is not True:
            raise StepMaterializationError(
                "visual_template_not_redacted",
                "visual template must be explicitly marked redacted",
            )
        template = visual.get("template")
        if not isinstance(template, str) or not template:
            raise StepMaterializationError("visual_template_missing", "visual template path is missing")
        window_rect = self._window_rect(observation)
        if window_rect is None:
            raise StepMaterializationError(
                "visual_window_unverified", "fresh observation has no verified window rectangle"
            )
        candidates = self._matched_candidates(visual, observation, window_rect)
        if not candidates or candidates[0].confidence < self.min_confidence:
            raise StepMaterializationError(
                "visual_match_low_confidence",
                "no visual candidate reached the minimum confidence",
                details={"candidate_count": len(candidates)},
            )
        best = candidates[0]
        if not self.min_scale <= best.scale <= self.max_scale:
            raise StepMaterializationError(
                "visual_scale_unsupported", f"visual match scale {best.scale} is outside policy"
            )
        second_confidence = candidates[1].confidence if len(candidates) > 1 else 0.0
        margin = best.confidence - second_confidence
        if len(candidates) > 1 and margin < self.min_margin:
            raise StepMaterializationError(
                "visual_match_ambiguous",
                "best and second visual candidates are too close",
                details={"candidate_count": len(candidates), "margin": margin},
            )
        x0, y0, x1, y1 = best.rect
        wx0, wy0, wx1, wy1 = window_rect
        if not (wx0 <= x0 < x1 <= wx1 and wy0 <= y0 < y1 <= wy1):
            raise StepMaterializationError(
                "visual_match_outside_window", "visual candidate is outside the locked window"
            )
        relative = visual.get("relativePoint", [0.5, 0.5])
        if (
            not isinstance(relative, (list, tuple)) or len(relative) != 2
            or not all(isinstance(value, (int, float)) and not isinstance(value, bool)
                       and 0 <= value <= 1 for value in relative)
        ):
            raise StepMaterializationError(
                "visual_relative_point_invalid", "relativePoint must contain two values in [0, 1]"
            )
        # Template rectangles use exclusive right/bottom edges. A recorded
        # anchor of 1.0 must still click the final pixel inside the match.
        screen_x = min(x1 - 1, max(x0, round(x0 + (x1 - x0) * relative[0])))
        screen_y = min(y1 - 1, max(y0, round(y0 + (y1 - y0) * relative[1])))
        return VisualResolution(
            action_id="pointer.click_window",
            args={
                "x": screen_x - wx0,
                "y": screen_y - wy0,
                "window_title_re": selector.window.get("titleRegex"),
                "expected_process_name": selector.window.get("processName"),
            },
            confidence=best.confidence,
            margin=margin,
        )

    def collect_evidence(self, selector: ReplaySelector, observation: Any) -> tuple[dict, ...]:
        """Expose verified template matches to the shared locator decision."""
        visual = selector.visual or {}
        scope = observation.get("screenshot_scope") if isinstance(observation, Mapping) else getattr(observation, "screenshot_scope", None)
        redacted = observation.get("screenshot_redacted") if isinstance(observation, Mapping) else getattr(observation, "screenshot_redacted", None)
        if scope != "window" or redacted is not True:
            raise StepMaterializationError("visual_window_unverified",
                                           "visual evidence requires a redacted locked-window frame")
        template = visual.get("template")
        if visual.get("redacted") is not True or not isinstance(template, str) or not template:
            raise StepMaterializationError("visual_template_missing", "redacted visual template is missing")
        window_rect = self._window_rect(observation)
        if window_rect is None:
            raise StepMaterializationError("visual_window_unverified", "window rectangle is unavailable")
        observation_id = (observation.get("observation_id") or observation.get("snapshot_id")) if isinstance(observation, Mapping) else (getattr(observation, "observation_id", None) or getattr(observation, "snapshot_id", None))
        identity = observation.get("window_identity") if isinstance(observation, Mapping) else getattr(observation, "window_identity", None)
        native_id = str((identity or {}).get("native_id") or "legacy") if isinstance(identity, Mapping) or identity is None else str(getattr(identity, "native_id", "legacy"))
        out = []
        for candidate in self._matched_candidates(visual, observation, window_rect):
            if not self.min_scale <= candidate.scale <= self.max_scale:
                continue
            x0, y0, x1, y1 = candidate.rect
            wx0, wy0, wx1, wy1 = window_rect
            if not (wx0 <= x0 < x1 <= wx1 and wy0 <= y0 < y1 <= wy1):
                continue
            out.append({"rect": candidate.rect, "confidence": candidate.confidence,
                        "observation_id": observation_id, "window_native_id": native_id})
        return tuple(out)


__all__ = ["SafeVisualResolver", "VisualCandidate", "VisualResolution"]
