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
    # True only when an explicitly opted-in text confirmation selected among
    # the pixel candidates after the margin gate refused. The pure visual
    # fallback (visual_only replay) never sets it. False for every legacy
    # (pixel-margin-passed) resolution.
    text_confirmed: bool = False
    # Evidence provenance for the resolution: source, frame/window references
    # and the refused margin when text_confirmed is True; None otherwise.
    # Never carries the control text itself.
    evidence: Mapping[str, Any] | None = None


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

    @staticmethod
    def _same_position(rect_a: tuple[int, int, int, int],
                       rect_b: tuple[int, int, int, int]) -> bool:
        """True when each rect's centre lies inside the other. Tolerates a
        few pixels of matcher refinement offset; rejects side-by-side
        neighbours of repeated icons."""
        cx_a, cy_a = (rect_a[0] + rect_a[2]) // 2, (rect_a[1] + rect_a[3]) // 2
        cx_b, cy_b = (rect_b[0] + rect_b[2]) // 2, (rect_b[1] + rect_b[3]) // 2
        return (rect_b[0] <= cx_a < rect_b[2] and rect_b[1] <= cy_a < rect_b[3]
                and rect_a[0] <= cx_b < rect_a[2] and rect_a[1] <= cy_b < rect_a[3])

    def _text_confirmed_candidate(
        self,
        selector: ReplaySelector,
        observation: Any,
        candidates: list[VisualCandidate],
        window_rect: tuple[int, int, int, int],
        margin: float,
    ) -> tuple[VisualCandidate | None, dict[str, Any] | None]:
        """Opt-in fail-closed assist for a refused pixel margin.

        Enabled only by an explicit policy that already allows semantic
        sources (e.g. the semantic_first replay fallback); the pure
        ``visual_only`` mode never calls it, so its outcome cannot depend
        on UIA data. The recorded control text
        (``selector.control.name``) must appear on exactly one control of
        the fresh window-scoped observation, that control must lie inside
        the locked window, and it must confirm exactly one candidate at or
        above ``min_confidence``. Every missing, duplicated, or malformed
        signal (including a malformed control rectangle) returns
        ``(None, None)`` and the ambiguity stands. This never weakens a
        pixel gate: candidates still need ``min_confidence``; the text
        only selects among candidates the pixel path already produced.
        The returned evidence mapping carries the actual source, the
        frame/window references and the refused margin — never the
        control text itself.
        """
        control = selector.control if isinstance(selector.control, Mapping) else {}
        recorded = control.get("name")
        if not isinstance(recorded, str) or not recorded.strip():
            return None, None
        controls = observation.get("controls") if isinstance(observation, Mapping) else None
        if not isinstance(controls, (list, tuple)) or not controls:
            return None, None
        named = []
        for item in controls:
            if not isinstance(item, Mapping):
                return None, None
            if str(item.get("text") or "") != recorded:
                continue
            rect = item.get("rect") or item.get("rectangle")
            if (not isinstance(rect, (list, tuple)) or len(rect) != 4
                    or not all(isinstance(value, (int, float))
                               and not isinstance(value, bool)
                               and math.isfinite(value) for value in rect)):
                return None, None
            control_rect = tuple(int(value) for value in rect)
            if not (control_rect[0] < control_rect[2]
                    and control_rect[1] < control_rect[3]):
                return None, None
            named.append((control_rect, item))
        if len(named) != 1:
            return None, None
        wx0, wy0, wx1, wy1 = window_rect
        control_rect, control_item = named[0]
        if not (wx0 <= control_rect[0] < control_rect[2] <= wx1
                and wy0 <= control_rect[1] < control_rect[3] <= wy1):
            return None, None
        confirmed = [candidate for candidate in candidates
                     if self._valid_confidence(candidate)
                     and candidate.confidence >= self.min_confidence
                     and self._same_position(candidate.rect, control_rect)]
        if len(confirmed) != 1:
            return None, None
        active = observation.get("active_window")
        active = active if isinstance(active, Mapping) else {}
        identity = observation.get("window_identity")
        identity = identity if isinstance(identity, Mapping) else {}
        evidence = {
            "text_confirmed": True,
            "source": "uia_control_text",
            "margin": margin,
            "frame": {
                "snapshot_id": (observation.get("snapshot_id")
                                or observation.get("observation_id")),
                "sha256": observation.get("screenshot_sha256"),
                "origin": observation.get("screenshot_origin"),
                "scope": observation.get("screenshot_scope"),
            },
            "window": {
                "native_id": str(identity.get("native_id") or "legacy"),
                "process_name": active.get("process_name",
                                           active.get("processName")),
                "pid": active.get("pid"),
                "rect": active.get("rect") or active.get("rectangle"),
            },
            "control": {
                "automation_id": (control_item.get("automationId")
                                  or control_item.get("automation_id")),
                "rect": list(control_rect),
            },
        }
        return confirmed[0], evidence

    def resolve(
        self,
        action_id: str,
        selector: ReplaySelector,
        observation: Any,
        *,
        allow_text_confirmation: bool = False,
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
        text_confirmed = False
        evidence: dict[str, Any] | None = None
        if len(candidates) > 1 and margin < self.min_margin:
            # Strict pixel-margin refusal: the pure visual_only replay must
            # stay independent of any UIA/semantic signal, so the text
            # confirmation exists only behind an explicit opt-in from a
            # policy that already allows semantic sources.
            confirmed: VisualCandidate | None = None
            if allow_text_confirmation:
                confirmed, evidence = self._text_confirmed_candidate(
                    selector, observation, candidates, window_rect, margin)
            if confirmed is None:
                raise StepMaterializationError(
                    "visual_match_ambiguous",
                    "best and second visual candidates are too close",
                    details={"candidate_count": len(candidates), "margin": margin},
                )
            best = confirmed
            # The confirmed candidate is a different (lower-confidence) pixel
            # match; it must still satisfy the scale policy itself.
            if not self.min_scale <= best.scale <= self.max_scale:
                raise StepMaterializationError(
                    "visual_scale_unsupported",
                    f"visual match scale {best.scale} is outside policy",
                )
            text_confirmed = True
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
            text_confirmed=text_confirmed,
            evidence=evidence,
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
