"""Safety gates for protected visual fallback during golden replay."""

from __future__ import annotations

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
        min_scale: float = 0.75,
        max_scale: float = 1.5,
    ) -> None:
        self._matcher = matcher
        self._template_verifier = template_verifier
        self.min_confidence = min_confidence
        self.min_margin = min_margin
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
        if screenshot_scope != "window":
            raise StepMaterializationError(
                "visual_window_unverified",
                "visual replay requires a screenshot captured from the locked window",
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
        if self._template_verifier is not None and not self._template_verifier(template, visual):
            raise StepMaterializationError(
                "visual_template_integrity_failed",
                "visual template path or digest failed integrity validation",
            )
        window_rect = self._window_rect(observation)
        if window_rect is None:
            raise StepMaterializationError(
                "visual_window_unverified", "fresh observation has no verified window rectangle"
            )
        candidates = sorted(
            self._matcher(template, observation),
            key=lambda candidate: candidate.confidence,
            reverse=True,
        )
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
            or not all(isinstance(value, (int, float)) and 0 <= value <= 1 for value in relative)
        ):
            raise StepMaterializationError(
                "visual_relative_point_invalid", "relativePoint must contain two values in [0, 1]"
            )
        screen_x = round(x0 + (x1 - x0) * relative[0])
        screen_y = round(y0 + (y1 - y0) * relative[1])
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


__all__ = ["SafeVisualResolver", "VisualCandidate", "VisualResolution"]
