"""Hard gates precede ranking; scores never override window ownership."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class LocatorPolicy:
    mode: str = "semantic_only"
    min_visual_confidence: float = 0.92
    min_ocr_confidence: float = 0.85
    min_margin: float = 0.05
    min_overlap: float = 0.4


def mode_to_locator_policy(replay_mode: str) -> LocatorPolicy:
    if replay_mode not in {"semantic_only", "semantic_first", "visual_only"}:
        raise ValueError("locator_policy_invalid")
    return LocatorPolicy(mode=replay_mode)


def choose_unique_candidate(candidates: tuple, *, min_margin: float = 0.05):
    """Choose only one ranked candidate separated from the runner-up."""
    if not candidates:
        raise ValueError("locator_not_found")
    ranked = sorted(candidates, key=lambda c: c.score, reverse=True)
    if len(ranked) > 1 and ranked[0].score - ranked[1].score < min_margin:
        raise ValueError("locator_ambiguous")
    return ranked[0]
